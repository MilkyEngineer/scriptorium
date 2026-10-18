"""admin_allow_server_key (§11.6): role-gated, takes a uid, never an email (M2 breakdown §3.1)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from scriptorium.auth.oidc import OIDCValidator, uid_hash_for
from scriptorium.users.store import uid_for
from tests.conftest import ALLOWED_EMAIL, ALLOWED_SUB, ISSUER, FakeIssuer, make_settings
from tests.fakes.ocr import FakeOcrFactory
from tests.test_server import mcp_session
from tests.tools.helpers import Served, serve

SERVER_KEY = "sk-ant-api03-server-" + "Xw2" * 10  # synthetic
USER_UID = uid_for(uid_hash_for(ISSUER, ALLOWED_SUB))
TOOL = "admin_allow_server_key"

Make = Callable[..., AbstractAsyncContextManager[Served]]


@pytest.fixture
def make(tmp_path: Path, issuer: FakeIssuer, make_validator: Callable[..., OIDCValidator]) -> Make:
    @asynccontextmanager
    async def build(**overrides: Any) -> AsyncIterator[Served]:
        data = tmp_path / "data"
        data.mkdir(exist_ok=True)
        settings = make_settings(data_dir=data, **overrides)
        async with serve(settings, issuer, make_validator, ocr_client=FakeOcrFactory()) as served:
            yield served

    return build


@pytest.fixture
async def app(make: Make) -> AsyncIterator[Served]:
    """A server key nobody is listed for; the user has signed in once."""
    async with make(anthropic_api_key=SERVER_KEY) as served:
        await served.ok("account_status")
        yield served


async def _admin(served: Served, args: dict[str, Any]) -> dict[str, Any]:
    return await served.ok(TOOL, args, token=served.issuer.admin_token())


async def _reason(served: Served) -> str:
    reason = (await served.ok("account_status"))["ocr"]["reason"]
    assert isinstance(reason, str)
    return reason


def _audited(served: Served) -> list[tuple[str, dict[str, Any]]]:
    import sqlite3

    from scriptorium.users.store import global_db_path

    conn = sqlite3.connect(global_db_path(served.settings.data_dir))
    try:
        rows = conn.execute(
            "SELECT outcome, identifiers FROM audit WHERE tool = ? ORDER BY rowid", (TOOL,)
        ).fetchall()
    finally:
        conn.close()
    return [(outcome, json.loads(identifiers)) for outcome, identifiers in rows]


async def test_hidden_from_users_and_refused_to_them(app: Served) -> None:
    async with mcp_session(app.app, app.issuer.token()) as (client, _):
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert TOOL not in names
    refused = await app.error(TOOL, {"user": USER_UID, "allowed": True})
    assert refused["code"] == "refused" and "administrators" in refused["message"]
    row = app.services.store.user(USER_UID)
    assert row is not None and row.server_key_allowed is False
    assert await _reason(app) == "server_key_not_allowed"


async def test_listed_for_admins_with_a_uid_argument(app: Served) -> None:
    async with mcp_session(app.app, app.issuer.admin_token()) as (client, _):
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    tool = tools[TOOL]
    assert tool.description is not None and "never by an email" in tool.description
    assert tool.input_schema["properties"]["user"]["pattern"] == "^[0-9a-f]{16}$"
    assert set(tool.input_schema["required"]) == {"user", "allowed"}
    assert tool.annotations is not None and tool.annotations.read_only_hint is False


async def test_grant_and_withdraw(app: Served) -> None:
    granted = await _admin(app, {"user": USER_UID, "allowed": True})
    assert granted == {
        "user": USER_UID,
        "server_key_allowed": True,
        "listed_in_server_key_users": False,
        "may_use_server_key": True,
        "server_key_configured": True,
        "monthly_cap_usd": 10.0,
        "note": None,
    }
    assert await _reason(app) == "server_key"
    assert (await _admin(app, {"user": USER_UID, "allowed": True}))["server_key_allowed"]
    withdrawn = await _admin(app, {"user": USER_UID, "allowed": False})
    assert withdrawn["may_use_server_key"] is False
    assert await _reason(app) == "server_key_not_allowed"
    assert _audited(app) == [
        ("ok", {"user": USER_UID, "allowed": True}),
        ("ok", {"user": USER_UID, "allowed": True}),
        ("ok", {"user": USER_UID, "allowed": False}),
    ]
    assert SERVER_KEY not in json.dumps([granted, withdrawn])


@pytest.mark.parametrize(
    "user",
    [ALLOWED_EMAIL, ALLOWED_SUB, USER_UID.upper(), USER_UID + "0", USER_UID[:-1], f"{USER_UID}\n"],
)
async def test_takes_a_uid_never_an_email_or_sub(app: Served, user: str) -> None:
    error = await app.error(TOOL, {"user": user, "allowed": True}, token=app.issuer.admin_token())
    assert error["code"] == "invalid_argument"
    row = app.services.store.user(USER_UID)
    assert row is not None and row.server_key_allowed is False


@pytest.mark.parametrize("allowed", ["true", 1, None])
async def test_allowed_is_a_boolean(app: Served, allowed: object) -> None:
    error = await app.error(
        TOOL, {"user": USER_UID, "allowed": allowed}, token=app.issuer.admin_token()
    )
    assert error["code"] == "invalid_argument"


async def test_an_unknown_uid_is_not_found(app: Served) -> None:
    error = await app.error(
        TOOL, {"user": "0123456789abcdef", "allowed": True}, token=app.issuer.admin_token()
    )
    assert error["code"] == "not_found"
    assert _audited(app) == [("not_found", {"user": "0123456789abcdef", "allowed": True})]


async def test_withdrawing_from_a_listed_user_says_the_list_still_admits_them(
    make: Make,
) -> None:
    async with make(anthropic_api_key=SERVER_KEY, server_key_users=USER_UID) as served:
        await served.ok("account_status")
        result = await _admin(served, {"user": USER_UID, "allowed": False})
        assert result["listed_in_server_key_users"] is True
        assert result["may_use_server_key"] is True
        assert "SERVER_KEY_USERS" in result["note"]
        assert await _reason(served) == "server_key"


async def test_without_a_server_key_the_grant_waits_for_one(make: Make) -> None:
    async with make() as served:
        await served.ok("account_status")
        result = await _admin(served, {"user": USER_UID, "allowed": True})
        assert result["server_key_configured"] is False
        assert result["may_use_server_key"] is False
        assert "SCRIPTORIUM_ANTHROPIC_API_KEY" in result["note"]
        assert await _reason(served) == "no_key"
