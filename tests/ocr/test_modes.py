"""Mode resolution (§8.1): user key → server key (allowed, caps with room) → lazy."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from scriptorium.auth.oidc import User, uid_hash_for
from scriptorium.config import Settings
from scriptorium.errors import NotFoundError, ScriptoriumError
from scriptorium.ocr.ledger import SpendLedger
from scriptorium.ocr.modes import (
    REASONS,
    USER_KEY_SECRET,
    Resolution,
    email_digest,
    listed_email_digest,
    resolve,
    server_key_listed,
)
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import GlobalStore, global_db_path
from tests.conftest import ISSUER, make_settings

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
SERVER_KEY = "sk-ant-server-" + "s" * 40
USER_KEY = "sk-ant-user-" + "u" * 40


def make_user(sub: str = "alice", email: str | None = None, verified: bool = True) -> User:
    return User(
        iss=ISSUER,
        sub=sub,
        uid_hash=uid_hash_for(ISSUER, sub),
        display_name=sub,
        email=email,
        email_verified=verified,
        is_admin=False,
        scopes=frozenset(),
    )


class World:
    def __init__(self, tmp_path: Path, **overrides: Any) -> None:
        self.tmp_path = tmp_path
        self.store = GlobalStore.open(global_db_path(tmp_path), migrate=True)
        self.ledger = SpendLedger(global_db_path(tmp_path))
        self.configure(**overrides)
        self.uid = self.store.ensure_user(make_user()).uid

    def configure(self, **overrides: Any) -> None:
        self.settings: Settings = make_settings(data_dir=self.tmp_path, **overrides)

    @property
    def box(self) -> SecretBox:
        return SecretBox(self.settings, self.uid)

    def resolve(self) -> Resolution:
        return resolve(self.settings, self.store, self.box, self.ledger, self.uid, NOW)

    def spend(self, usd: float, mode: str = "server", uid: str | None = None) -> None:
        self.ledger.record(
            uid=uid or self.uid,
            mode=mode,  # type: ignore[arg-type]
            model="claude-sonnet-5-5",
            input_tokens=1,
            output_tokens=1,
            cost_usd=usd,
            at=NOW,
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_no_key_anywhere(world: World) -> None:
    assert world.resolve() == Resolution("lazy", "no_key")
    assert world.resolve().api_key is None


def test_user_key_wins(world: World) -> None:
    world.configure(anthropic_api_key=SERVER_KEY, server_key_users="*")
    world.box.seal(USER_KEY_SECRET, USER_KEY.encode())
    resolution = world.resolve()
    assert (resolution.mode, resolution.reason) == ("user", "user_key")
    assert resolution.api_key is not None
    assert resolution.api_key.get_secret_value() == USER_KEY
    assert USER_KEY not in repr(resolution) and "sk-ant" not in repr(resolution)


def test_server_key_needs_permission(world: World) -> None:
    world.configure(anthropic_api_key=SERVER_KEY)
    assert world.resolve() == Resolution("lazy", "server_key_not_allowed")
    world.store.set_server_key_allowed(world.uid, True)
    resolution = world.resolve()
    assert (resolution.mode, resolution.reason) == ("server", "server_key")
    assert resolution.api_key is not None
    assert resolution.api_key.get_secret_value() == SERVER_KEY


def test_permission_without_a_server_key_is_no_key(world: World) -> None:
    world.store.set_server_key_allowed(world.uid, True)
    assert world.resolve() == Resolution("lazy", "no_key")


@pytest.mark.parametrize("entry", ["*", "UID"])
def test_listed_by_uid_or_wildcard(world: World, entry: str) -> None:
    world.configure(
        anthropic_api_key=SERVER_KEY,
        server_key_users=f"someone-else,{world.uid if entry == 'UID' else entry}",
    )
    assert world.resolve().mode == "server"


def test_a_sub_entry_does_not_match(world: World) -> None:
    """Only uids and verified emails: a claim value a user might choose never matches."""
    world.configure(anthropic_api_key=SERVER_KEY, server_key_users="alice")
    assert world.resolve().reason == "server_key_not_allowed"


def test_listed_by_verified_email(world: World) -> None:
    world.configure(anthropic_api_key=SERVER_KEY, server_key_users="Alice@Example.org")
    assert world.resolve().reason == "server_key_not_allowed"  # not recorded yet
    user = make_user(email="alice@EXAMPLE.org")
    digest = listed_email_digest(world.settings, user)
    assert (
        digest
        == hashlib.sha256(b"alice@example.org").hexdigest()
        == email_digest("ALICE@example.org")
    )
    world.store.set_server_key_email(world.uid, digest)
    assert world.resolve().mode == "server"
    # Removing the entry revokes at once, although the digest is still stored.
    world.configure(anthropic_api_key=SERVER_KEY, server_key_users="bob@example.org")
    assert world.resolve().reason == "server_key_not_allowed"


def test_unverified_or_unlisted_email_records_nothing(world: World) -> None:
    world.configure(server_key_users="alice@example.org")
    assert (
        listed_email_digest(world.settings, make_user(email="alice@example.org", verified=False))
        is None
    )
    assert listed_email_digest(world.settings, make_user(email="carol@example.org")) is None
    assert listed_email_digest(world.settings, make_user()) is None
    # Non-ASCII letters are not folded (ascii_lower), as in the allowlist.
    assert listed_email_digest(world.settings, make_user(email="alİce@example.org")) is None


def test_server_key_listed_ignores_email_entries_without_a_digest(world: World) -> None:
    world.configure(server_key_users="alice@example.org")
    row = world.store.user(world.uid)
    assert row is not None and not server_key_listed(world.settings, row)


def test_global_cap(world: World) -> None:
    world.configure(anthropic_api_key=SERVER_KEY, server_key_users="*")
    other = world.store.ensure_user(make_user("bob")).uid
    world.spend(20.0, uid=other)
    assert world.resolve() == Resolution("lazy", "cap_reached_global")


def test_user_cap(world: World) -> None:
    world.configure(anthropic_api_key=SERVER_KEY, server_key_users="*")
    world.spend(9.99)
    assert world.resolve().mode == "server"
    world.spend(0.01)
    assert world.resolve() == Resolution("lazy", "cap_reached_user")


def test_own_cap_falls_through_to_the_server_key(world: World) -> None:
    world.box.seal(USER_KEY_SECRET, USER_KEY.encode())
    world.store.set_user_ocr_cap(world.uid, 1.0)
    world.spend(1.0, mode="user")
    assert world.resolve() == Resolution("lazy", "user_cap_reached")  # no server key
    world.configure(anthropic_api_key=SERVER_KEY)
    assert world.resolve() == Resolution("lazy", "user_cap_reached")  # not allowed
    world.configure(anthropic_api_key=SERVER_KEY, server_key_users="*")
    resolution = world.resolve()
    assert (resolution.mode, resolution.reason) == ("server", "server_key")
    world.spend(10.0)
    assert world.resolve() == Resolution("lazy", "user_cap_reached")  # server cap too


def test_own_key_without_a_cap_is_unlimited(world: World) -> None:
    world.box.seal(USER_KEY_SECRET, USER_KEY.encode())
    world.spend(1000.0, mode="user")
    assert world.resolve().reason == "user_key"


def test_unreadable_user_key_never_falls_back_to_the_server_key(world: World) -> None:
    world.configure(anthropic_api_key=SERVER_KEY, server_key_users="*")
    world.box.seal(USER_KEY_SECRET, b"not a key")
    with pytest.raises(ScriptoriumError) as info:
        world.resolve()
    assert info.value.code == "internal"
    assert "not a key" not in str(info.value)


def test_unknown_user(world: World) -> None:
    with pytest.raises(NotFoundError):
        resolve(world.settings, world.store, world.box, world.ledger, "f" * 16, NOW)


def test_reasons_are_the_contract() -> None:
    assert REASONS == {
        "user_key",
        "server_key",
        "no_key",
        "server_key_not_allowed",
        "cap_reached_global",
        "cap_reached_user",
        "user_cap_reached",
    }


def test_resolution_equality_ignores_the_key() -> None:
    assert Resolution("server", "server_key", SecretStr("a" * 20)) == Resolution(
        "server", "server_key"
    )
