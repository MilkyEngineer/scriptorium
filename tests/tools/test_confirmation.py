"""Confirmation tokens (§11.0): bound to the user, the tool and the exact arguments; 10
minutes; single use through the ``confirmations`` table."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from scriptorium.config import Settings
from scriptorium.tools._conventions import (
    CONFIRMATION_TTL_SECONDS,
    ErrorCode,
    ToolError,
    confirmation_digest,
    issue_confirmation_token,
    spend_confirmation_token,
    verify_confirmation_token,
)
from scriptorium.users.store import GlobalStore
from tests.conftest import MASTER_KEY, make_settings

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
UID = "0123456789abcdef"
TOOL = "disconnect_remarkable"
ARGS: dict[str, Any] = {"keep_archive": False, "acct": "01J0000000000000000000000A"}


@pytest.fixture
def settings() -> Settings:
    return make_settings()


def issue(settings: Settings, **kw: Any) -> str:
    fields: dict[str, Any] = {"uid": UID, "tool": TOOL, "args": ARGS, "now": T0, **kw}
    return issue_confirmation_token(settings, **fields)


def refusal(settings: Settings, token: str, **kw: Any) -> str:
    fields: dict[str, Any] = {"uid": UID, "tool": TOOL, "args": ARGS, "now": T0, **kw}
    with pytest.raises(ToolError) as caught:
        verify_confirmation_token(settings, token, **fields)
    assert caught.value.code is ErrorCode.REFUSED
    reason = caught.value.extra["reason"]
    assert isinstance(reason, str)
    return reason


def test_a_token_verifies_for_the_same_user_tool_and_arguments(settings: Settings) -> None:
    token = issue(settings)
    verify_confirmation_token(settings, token, uid=UID, tool=TOOL, args=ARGS, now=T0)
    # Key order does not matter: the arguments are compared as canonical JSON.
    reordered = dict(reversed(list(ARGS.items())))
    verify_confirmation_token(settings, token, uid=UID, tool=TOOL, args=reordered, now=T0)


@pytest.mark.parametrize(
    "change",
    [
        {"uid": "fedcba9876543210"},
        {"tool": "admin_purge_tombstones"},
        {"args": {**ARGS, "keep_archive": True}},
        {"args": {**ARGS, "acct": "01J0000000000000000000000B"}},
        {"args": {"keep_archive": False}},
        {"args": {**ARGS, "extra": None}},
    ],
)
def test_a_token_is_bound_to_the_user_tool_and_arguments(
    settings: Settings, change: dict[str, Any]
) -> None:
    assert refusal(settings, issue(settings), **change) == "not_bound"


def test_a_token_expires_after_ten_minutes(settings: Settings) -> None:
    token = issue(settings)
    ttl = timedelta(seconds=CONFIRMATION_TTL_SECONDS)
    verify_confirmation_token(settings, token, uid=UID, tool=TOOL, args=ARGS, now=T0 + ttl)
    assert refusal(settings, token, now=T0 + ttl + timedelta(seconds=1)) == "expired"
    # Issued "in the future" (the clock stepped back by more than the skew): not accepted.
    assert refusal(settings, token, now=T0 - timedelta(seconds=61)) == "expired"
    verify_confirmation_token(
        settings, token, uid=UID, tool=TOOL, args=ARGS, now=T0 - timedelta(seconds=60)
    )


def test_a_rotated_master_key_invalidates_outstanding_tokens(settings: Settings) -> None:
    token = issue(settings)
    rotated = make_settings(master_keys=f"{Fernet.generate_key().decode()},{MASTER_KEY}")
    assert refusal(rotated, token) == "not_bound"
    # The old key is still in the list, so data it sealed stays readable; tokens do not.
    verify_confirmation_token(rotated, issue(rotated), uid=UID, tool=TOOL, args=ARGS, now=T0)


def test_another_deployments_token_is_not_bound(settings: Settings) -> None:
    other = make_settings(master_keys=Fernet.generate_key().decode())
    assert refusal(settings, issue(other)) == "not_bound"


@pytest.mark.parametrize(
    "token",
    ["", "x", "A" * 53, "A" * 55, "A" * 53 + "=", "A" * 53 + "+", "A" * 53 + "/", "é" * 54],
)
def test_malformed_tokens_are_refused(settings: Settings, token: str) -> None:
    assert refusal(settings, token) == "malformed"


def test_a_non_canonical_encoding_is_malformed(settings: Settings) -> None:
    token = issue(settings)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(token[-1])
    # 40 bytes leave four spare bits in the last character; setting one decodes to the same
    # bytes but is not the token the server issued.
    twin = token[:-1] + alphabet[last | 1]
    assert twin != token
    assert refusal(settings, twin) == "malformed"


def test_the_token_carries_no_argument_in_clear(settings: Settings) -> None:
    secret_like = "visible-only-in-the-arguments"
    token = issue(settings, args={"note": secret_like})
    raw = base64.urlsafe_b64decode(token + "==")
    assert len(raw) == 40
    assert secret_like.encode() not in raw
    assert secret_like not in token
    assert int.from_bytes(raw[:8], "big") == int(T0.timestamp())


def test_arguments_must_be_plain_json(settings: Settings) -> None:
    with pytest.raises(ValueError):
        issue(settings, args={"when": T0})
    with pytest.raises(ValueError):
        issue(settings, args={"x": float("nan")})


def test_a_token_is_spent_once(settings: Settings, tmp_path: Path) -> None:
    store = GlobalStore.open(tmp_path / "scriptorium.sqlite", migrate=True)
    token = issue(settings)
    spend_confirmation_token(store, token, now=T0)
    with pytest.raises(ToolError) as caught:
        spend_confirmation_token(store, token, now=T0 + timedelta(seconds=1))
    assert caught.value.extra["reason"] == "used"
    # Another token of the same dry run (issued a second later) is a different token.
    spend_confirmation_token(store, issue(settings, now=T0 + timedelta(seconds=1)), now=T0)


def test_the_ledger_stores_a_hash_and_prunes_old_rows(settings: Settings, tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "scriptorium.sqlite"
    store = GlobalStore.open(path, migrate=True)
    token = issue(settings)
    spend_confirmation_token(store, token, now=T0)
    later = issue(settings, now=T0 + timedelta(hours=1))
    spend_confirmation_token(store, later, now=T0 + timedelta(hours=1))
    conn = sqlite3.connect(path)
    try:
        rows = [r[0] for r in conn.execute("SELECT sha256 FROM confirmations")]
    finally:
        conn.close()
    assert rows == [confirmation_digest(later)]  # the first was pruned
    assert token not in rows and later not in rows


def test_refusal_messages_carry_no_token_or_argument(settings: Settings) -> None:
    token = issue(settings)
    with pytest.raises(ToolError) as caught:
        verify_confirmation_token(
            settings, token, uid=UID, tool=TOOL, args={"acct": "X" * 26}, now=T0
        )
    text = f"{caught.value.message} {caught.value.hint} {caught.value.extra}"
    assert token not in text and "X" * 26 not in text and UID not in text
