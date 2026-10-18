"""set_claude_api_key, clear_claude_api_key and account_status.ocr (§8.1, §11.1; M2 #3, #6).

The Claude API is never called: ``Services.ocr_client`` is a :class:`FakeOcrFactory`. The key
is a synthetic string; the tests grep every result, audit row, log line and file for it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scriptorium.auth.oidc import OIDCValidator, uid_hash_for
from scriptorium.logging import JsonFormatter
from scriptorium.ocr.errors import OcrRefused, OcrUnavailable
from scriptorium.ocr.modes import USER_KEY_SECRET
from scriptorium.tools._services import KEY_CHECK_WINDOW_S, KEY_CHECKS_PER_WINDOW
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import uid_for
from tests.conftest import ALLOWED_EMAIL, ALLOWED_SUB, ISSUER, FakeIssuer, make_settings
from tests.fakes.ocr import FakeOcrFactory
from tests.test_server import mcp_session
from tests.tools.helpers import Served, serve

KEY = "sk-ant-api03-" + "Zq7" * 12 + "-keymarker"  # synthetic; never a real key
DISTINCT = "Zq7Zq7Zq7"  # any 9 characters of the key's body
OTHER_KEY = "sk-ant-api03-" + "Yp4" * 12
SERVER_KEY = "sk-ant-api03-server-" + "Xw2" * 10
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
USER_UID = uid_for(uid_hash_for(ISSUER, ALLOWED_SUB))


@dataclass
class Tick:
    """``Services.monotonic`` under the test's control."""

    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


Make = Callable[..., AbstractAsyncContextManager[Served]]


@pytest.fixture
def factory() -> FakeOcrFactory:
    return FakeOcrFactory()


@pytest.fixture
def tick() -> Tick:
    return Tick()


@pytest.fixture
def make(
    tmp_path: Path,
    issuer: FakeIssuer,
    make_validator: Callable[..., OIDCValidator],
    factory: FakeOcrFactory,
    tick: Tick,
) -> Make:
    """The app over an empty ``/data`` with these settings, the fake API and a fixed clock."""

    @asynccontextmanager
    async def build(**overrides: Any) -> AsyncIterator[Served]:
        data = tmp_path / "data"
        data.mkdir(exist_ok=True)
        settings = make_settings(data_dir=data, **overrides)
        async with serve(
            settings,
            issuer,
            make_validator,
            ocr_client=factory,
            monotonic=tick,
            clock=lambda: NOW,
        ) as served:
            yield served

    return build


@pytest.fixture
async def app(make: Make) -> AsyncIterator[Served]:
    """No server key: the user's own key or lazy."""
    async with make() as served:
        yield served


@pytest.fixture
async def shared(make: Make) -> AsyncIterator[Served]:
    """A server key every user may spend."""
    async with make(anthropic_api_key=SERVER_KEY, server_key_users="*") as served:
        yield served


async def _ocr(served: Served, token: str | None = None) -> dict[str, Any]:
    ocr = (await served.ok("account_status", token=token))["ocr"]
    assert isinstance(ocr, dict)
    return ocr


def _spend(served: Served, usd: float, *, mode: str = "server", uid: str = USER_UID) -> None:
    served.services.ledger.record(
        uid=uid,
        mode=mode,  # type: ignore[arg-type]
        model="claude-sonnet-5-5",
        input_tokens=2300,
        output_tokens=150,
        cost_usd=usd,
        at=NOW,
    )


def _stored_key(served: Served) -> bytes | None:
    return SecretBox(served.settings, USER_UID).open(USER_KEY_SECRET)


def _files_containing(root: Path, needle: bytes) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and needle in p.read_bytes()]


# ---- account_status.ocr ----------------------------------------------------------------------


async def test_status_before_any_key(app: Served) -> None:
    status = await app.ok("account_status")
    assert status["user"] == USER_UID and re.fullmatch(r"[0-9a-f]{16}", status["user"])
    ocr = status["ocr"]
    assert (ocr["mode"], ocr["reason"], ocr["own_key_stored"]) == ("lazy", "no_key", False)
    assert ocr["month"] == "2026-10"
    assert ocr["resets_at"] == "2026-11-01T00:00:00Z"
    assert ocr["spend_this_month"] == {
        "usd": 0.0,
        "pages": 0,
        "own_key": {"usd": 0.0, "pages": 0},
        "server_key": {"usd": 0.0, "pages": 0},
    }
    assert ocr["caps"] == {"own_key": None, "server_key": None}
    assert "read_page" in ocr["note"] and "set_claude_api_key" in ocr["note"]


async def test_status_of_a_linked_account_carries_the_ocr_summary(library: Served) -> None:
    status = await library.ok("account_status")
    assert status["linked"] is True and status["user"] == USER_UID
    assert (status["ocr"]["mode"], status["ocr"]["reason"]) == ("lazy", "no_key")
    assert set(status["ocr"]) >= {"spend_this_month", "caps", "month", "resets_at"}


async def test_server_key_mode_shows_the_allowance(shared: Served) -> None:
    ocr = await _ocr(shared)
    assert (ocr["mode"], ocr["reason"]) == ("server", "server_key")
    assert ocr["caps"] == {
        "own_key": None,
        "server_key": {
            "monthly_cap_usd": 10.0,
            "remaining_usd": 10.0,
            "server_budget_spent": False,
        },
    }
    _spend(shared, 4.0)
    ocr = await _ocr(shared)
    assert ocr["spend_this_month"]["usd"] == 4.0
    assert ocr["spend_this_month"]["server_key"] == {"usd": 4.0, "pages": 1}
    assert ocr["caps"]["server_key"]["remaining_usd"] == 6.0
    _spend(shared, 6.0)  # the per-user cap is reached: lazy until next month (M2 #3)
    ocr = await _ocr(shared)
    assert (ocr["mode"], ocr["reason"]) == ("lazy", "cap_reached_user")
    assert ocr["caps"]["server_key"]["remaining_usd"] == 0.0
    assert "2026-11-01" in ocr["note"]
    assert SERVER_KEY not in json.dumps(ocr) and "Xw2Xw2" not in json.dumps(ocr)


async def test_the_global_cap_stops_everyone(make: Make) -> None:
    async with make(
        anthropic_api_key=SERVER_KEY, server_key_users="*", ocr_monthly_cap_usd=5
    ) as served:
        admin_uid = (await served.ok("account_status", token=served.issuer.admin_token()))["user"]
        await served.ok("account_status")  # the user signs in
        _spend(served, 5.0, uid=admin_uid)
        ocr = await _ocr(served)
        assert (ocr["mode"], ocr["reason"]) == ("lazy", "cap_reached_global")
        assert ocr["caps"]["server_key"]["server_budget_spent"] is True
        assert ocr["caps"]["server_key"]["remaining_usd"] == 10.0  # this user spent nothing


async def test_a_server_key_the_user_may_not_spend(make: Make) -> None:
    async with make(anthropic_api_key=SERVER_KEY) as served:
        ocr = await _ocr(served)
        assert (ocr["mode"], ocr["reason"]) == ("lazy", "server_key_not_allowed")
        assert ocr["caps"]["server_key"] is None
        assert "administrator" in ocr["note"]


async def test_an_email_entry_needs_a_verified_email(make: Make) -> None:
    async with make(anthropic_api_key=SERVER_KEY, server_key_users=ALLOWED_EMAIL) as served:
        verified = served.issuer.token(email=ALLOWED_EMAIL, email_verified=True)
        unverified = served.issuer.token(email=ALLOWED_EMAIL, email_verified=False)
        assert (await _ocr(served, verified))["reason"] == "server_key"
        # The same user signing in without a verified email loses it at once.
        assert (await _ocr(served, unverified))["reason"] == "server_key_not_allowed"
        assert (await _ocr(served, verified))["reason"] == "server_key"


# ---- set_claude_api_key -----------------------------------------------------------------------


async def test_set_key_checks_then_seals_and_never_echoes(
    app: Served, factory: FakeOcrFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    result = await app.ok("set_claude_api_key", {"key": KEY, "monthly_cap_usd": 5})
    assert result["stored"] is True and result["monthly_cap_usd"] == 5.0
    assert (result["ocr"]["mode"], result["ocr"]["reason"]) == ("user", "user_key")
    assert result["ocr"]["own_key_stored"] is True
    assert result["ocr"]["caps"]["own_key"] == {"monthly_cap_usd": 5.0, "remaining_usd": 5.0}
    assert "$5 a month" in result["ocr"]["note"]
    # Checked once, with this key, before it was sealed; the client was closed.
    assert factory.keys == [KEY]
    assert factory.client.calls == [("validate_key",)]
    assert factory.client.closed
    assert _stored_key(app) == KEY.encode()
    status = await app.ok("account_status")
    await app.ok("clear_claude_api_key")
    # Nowhere else: results, audit rows, log lines, files in clear. Every record the server
    # side emits counts, at DEBUG and from any library (production holds mcp, httpx and
    # anthropic at WARNING); only the test's own MCP client logs the request it sends.
    seen = json.dumps([result, status]) + json.dumps(app.audit_rows(), default=str)
    formatter = JsonFormatter()
    server_side = [r for r in caplog.records if not r.name.startswith("mcp.client")]
    assert any(r.getMessage() == "tool_call" for r in server_side)
    seen += "\n".join(formatter.format(r) + repr(r.__dict__) for r in server_side)
    assert KEY not in seen and DISTINCT not in seen
    assert _files_containing(app.settings.data_dir, DISTINCT.encode()) == []


async def test_the_audit_row_carries_no_identifiers(app: Served) -> None:
    await app.ok("set_claude_api_key", {"key": KEY})
    rows = [r for r in app.audit_rows() if "set_claude_api_key" in r]
    assert len(rows) == 1
    assert "{}" in rows[0]  # no identifiers at all: the key is not one


async def test_whitespace_around_a_pasted_key_is_dropped(
    app: Served, factory: FakeOcrFactory
) -> None:
    await app.ok("set_claude_api_key", {"key": f"  {KEY}\n"})
    assert factory.keys == [KEY]
    assert _stored_key(app) == KEY.encode()


@pytest.mark.parametrize(
    "args",
    [
        {"key": "not a key, keymarker-0123456789"},
        {"key": "keymarker"},
        {"key": "keymarker-" + "x" * 600},
        {"key": ["keymarker-" + "x" * 30]},
        {"key": "sk-ant-api03-keymarker-Zq7Zq7Zq7\x00"},
        {"key": 12345678901234567890123},
        {"key": KEY, "monthly_cap_usd": -1},
        {"key": KEY, "monthly_cap_usd": 1e9},
        {"key": KEY, "monthly_cap_usd": True},
        {"key": KEY, "monthly_cap_usd": "5"},
        {"key": KEY, "user": "0123456789abcdef"},
        {},
    ],
)
async def test_bad_arguments_are_invalid_and_not_echoed(
    app: Served, factory: FakeOcrFactory, args: dict[str, Any]
) -> None:
    error = await app.error("set_claude_api_key", args)
    assert error["code"] == "invalid_argument"
    text = json.dumps(error)
    assert "keymarker" not in text and DISTINCT not in text
    assert factory.keys == []  # nothing was sent to the API
    assert _stored_key(app) is None


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        ("bad_key", "rejected this key"),
        ("billing", "billing problem"),
        ("forbidden", "cannot use the OCR model claude-sonnet-5-5"),
        ("model_unavailable", "cannot use the OCR model"),
        ("invalid_request", "could not be checked"),
    ],
)
async def test_a_rejected_key_leaves_the_stored_one_alone(
    app: Served, factory: FakeOcrFactory, reason: str, words: str
) -> None:
    await app.ok("set_claude_api_key", {"key": OTHER_KEY, "monthly_cap_usd": 3})
    factory.client.key_error = OcrRefused(reason)
    error = await app.error("set_claude_api_key", {"key": KEY, "monthly_cap_usd": 50})
    assert error["code"] == "refused"
    assert words in error["message"] and "Nothing was stored." in error["message"]
    assert KEY not in json.dumps(error) and DISTINCT not in json.dumps(error)
    assert _stored_key(app) == OTHER_KEY.encode()
    row = app.services.store.user(USER_UID)
    assert row is not None and row.ocr_cap_usd == 3.0


async def test_an_unreachable_api_stores_nothing(app: Served, factory: FakeOcrFactory) -> None:
    factory.client.key_error = OcrUnavailable("overloaded", retry_after_s=5)
    error = await app.error("set_claude_api_key", {"key": KEY})
    assert error["code"] == "refused"
    assert "Nothing was stored." in error["message"] and "Try again" in error["hint"]
    assert _stored_key(app) is None


async def test_key_checks_are_rate_limited_per_user(
    app: Served, factory: FakeOcrFactory, tick: Tick
) -> None:
    factory.client.key_error = OcrRefused("bad_key")
    for _ in range(KEY_CHECKS_PER_WINDOW):
        assert "rejected" in (await app.error("set_claude_api_key", {"key": KEY}))["message"]
    limited = await app.error("set_claude_api_key", {"key": KEY})
    assert limited["code"] == "refused" and "Too many" in limited["message"]
    assert len(factory.keys) == KEY_CHECKS_PER_WINDOW  # the limited call reached no API
    # Another user is not held up by this one.
    factory.client.key_error = None
    await app.ok("set_claude_api_key", {"key": OTHER_KEY}, token=app.issuer.admin_token())
    assert "Too many" in (await app.error("set_claude_api_key", {"key": KEY}))["message"]
    tick.now += KEY_CHECK_WINDOW_S + 1
    assert (await app.ok("set_claude_api_key", {"key": KEY}))["stored"] is True


async def test_own_key_is_preferred_over_the_server_key(shared: Served) -> None:
    """M2 #6: with both, the user's own key transcribes; at its cap the server key takes over."""
    result = await shared.ok("set_claude_api_key", {"key": KEY, "monthly_cap_usd": 1})
    assert (result["ocr"]["mode"], result["ocr"]["reason"]) == ("user", "user_key")
    caps = result["ocr"]["caps"]
    assert caps["own_key"] == {"monthly_cap_usd": 1.0, "remaining_usd": 1.0}
    assert caps["server_key"]["remaining_usd"] == 10.0
    _spend(shared, 1.0, mode="user")
    ocr = await _ocr(shared)
    assert (ocr["mode"], ocr["reason"]) == ("server", "server_key")
    assert ocr["spend_this_month"]["own_key"] == {"usd": 1.0, "pages": 1}


async def test_own_cap_reached_without_a_server_key(app: Served) -> None:
    await app.ok("set_claude_api_key", {"key": KEY, "monthly_cap_usd": 1})
    _spend(app, 0.75, mode="user")
    assert (await _ocr(app))["caps"]["own_key"]["remaining_usd"] == 0.25
    _spend(app, 0.25, mode="user")
    ocr = await _ocr(app)
    assert (ocr["mode"], ocr["reason"]) == ("lazy", "user_cap_reached")
    assert "monthly_cap_usd" in ocr["note"]
    # Raising the cap is a new set_claude_api_key call, checked like the first.
    raised = await app.ok("set_claude_api_key", {"key": KEY, "monthly_cap_usd": 2})
    assert raised["ocr"]["reason"] == "user_key"


async def test_a_key_without_a_cap(app: Served) -> None:
    await app.ok("set_claude_api_key", {"key": KEY, "monthly_cap_usd": 3})
    result = await app.ok("set_claude_api_key", {"key": KEY})  # replaces key and cap
    assert result["ocr"]["caps"]["own_key"] == {"monthly_cap_usd": None, "remaining_usd": None}
    _spend(app, 500.0, mode="user")
    assert (await _ocr(app))["reason"] == "user_key"


# ---- clear_claude_api_key ---------------------------------------------------------------------


async def test_clear_forgets_key_and_cap_and_keeps_spend(app: Served) -> None:
    await app.ok("set_claude_api_key", {"key": KEY, "monthly_cap_usd": 5})
    _spend(app, 1.25, mode="user")
    cleared = await app.ok("clear_claude_api_key")
    assert cleared["cleared"] is True
    assert (cleared["ocr"]["reason"], cleared["ocr"]["own_key_stored"]) == ("no_key", False)
    assert cleared["ocr"]["spend_this_month"]["own_key"] == {"usd": 1.25, "pages": 1}
    assert _stored_key(app) is None
    row = app.services.store.user(USER_UID)
    assert row is not None and row.ocr_cap_usd is None
    assert (await app.ok("clear_claude_api_key"))["cleared"] is True  # idempotent


async def test_an_unreadable_key_fails_closed_and_can_be_cleared(shared: Served) -> None:
    await shared.ok("account_status")
    SecretBox(shared.settings, USER_UID).seal(USER_KEY_SECRET, b"\xff not a key")
    ocr = await _ocr(shared)
    # Not the server key: the user stored a key of their own and must fix or clear it.
    assert (ocr["mode"], ocr["reason"], ocr["own_key_stored"]) == ("lazy", "key_unreadable", True)
    assert "clear_claude_api_key" in ocr["note"]
    cleared = await shared.ok("clear_claude_api_key")
    assert (cleared["ocr"]["mode"], cleared["ocr"]["reason"]) == ("server", "server_key")


# ---- the tool list ----------------------------------------------------------------------------


async def test_the_key_tools_as_listed(app: Served) -> None:
    async with mcp_session(app.app, app.issuer.token()) as (client, _):
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    set_key, clear = tools["set_claude_api_key"], tools["clear_claude_api_key"]
    key = set_key.input_schema["properties"]["key"]
    assert key["writeOnly"] is True and key["format"] == "password"
    assert set_key.input_schema["required"] == ["key"]
    assert set(set_key.input_schema["properties"]) == {"key", "monthly_cap_usd"}
    assert clear.input_schema.get("properties") == {}
    assert set_key.annotations is not None and clear.annotations is not None
    assert set_key.annotations.read_only_hint is False and set_key.annotations.open_world_hint
    assert clear.annotations.destructive_hint is True and clear.annotations.idempotent_hint
    for tool in (set_key, clear):
        assert tool.description is not None and len(tool.description) > 80
