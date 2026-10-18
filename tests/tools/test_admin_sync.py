"""admin_clear_anomaly (§7 step 4, §11.6): clearing re-trips, an approval tombstones once.

The breaker is tripped by the real worker against the fake cloud: nine synthetic documents,
six of them removed, two complete listings. No real document names or UUIDs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from scriptorium.auth.oidc import OIDCValidator
from scriptorium.users.store import global_db_path
from scriptorium.worker import AccountLoop
from tests.cloud.fake_cloud import PAGES, FakeCloud
from tests.cloud.scenarios import nine
from tests.conftest import FakeIssuer, make_settings
from tests.tools.helpers import Served, serve
from tests.worker.helpers import Linked, link

TOOL = "admin_clear_anomaly"
REASON = "Synthetic reason: checked the tablet by hand"
REMOVED = PAGES[:6]


@pytest.fixture
async def linked(tmp_path: Path) -> AsyncIterator[Linked]:
    """Nine documents, synced once (renders are not run: nothing here needs them)."""
    cloud = FakeCloud()
    nine(cloud)
    account = link(tmp_path, cloud=cloud)
    try:
        yield account
    finally:
        account.close()


@pytest.fixture
async def app(
    linked: Linked, issuer: FakeIssuer, make_validator: Callable[..., OIDCValidator]
) -> AsyncIterator[Served]:
    async with serve(linked.settings, issuer, make_validator, clock=linked.clock) as served:
        yield served


async def _poll(linked: Linked, loop: AccountLoop) -> None:
    await loop.poll_once(linked.context())


async def _trip(linked: Linked) -> AccountLoop:
    """Sync, remove six of nine, list twice: the breaker trips on the second listing."""
    loop = linked.loop()
    await _poll(linked, loop)
    linked.cloud.remove_document(*REMOVED)
    await _poll(linked, loop)
    await _poll(linked, loop)
    assert _state(linked, "anomaly") == "mass_delete"
    return loop


def _state(linked: Linked, key: str) -> str | None:
    return linked.index().get_state(key)


def _tombstoned(linked: Linked) -> frozenset[str]:
    return linked.index().document_uuids(deleted=True)


def _args(**overrides: Any) -> dict[str, Any]:
    return {"reason": REASON, **overrides}


async def _admin(app: Served, linked: Linked, **overrides: Any) -> dict[str, Any]:
    return await app.ok(TOOL, _args(user=linked.uid, **overrides), token=app.issuer.admin_token())


async def _admin_error(app: Served, args: dict[str, Any]) -> dict[str, Any]:
    return await app.error(TOOL, args, token=app.issuer.admin_token())


def _audited(app: Served) -> list[tuple[str, dict[str, Any]]]:
    conn = sqlite3.connect(global_db_path(app.settings.data_dir))
    try:
        rows = conn.execute(
            "SELECT outcome, identifiers FROM audit WHERE tool = ? ORDER BY rowid", (TOOL,)
        ).fetchall()
    finally:
        conn.close()
    return [(outcome, json.loads(identifiers)) for outcome, identifiers in rows]


def _server_logs(caplog: pytest.LogCaptureFixture) -> str:
    """Everything logged outside the MCP client (which logs the arguments it sends)."""
    return "\n".join(
        f"{r.getMessage()} {getattr(r, 'scriptorium_fields', {})}"
        for r in caplog.records
        if not r.name.startswith("mcp.client")
    )


async def test_clearing_re_trips_and_an_approval_tombstones_once(
    app: Served, linked: Linked, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    loop = await _trip(linked)
    stored = _state(linked, "anomaly_candidates")
    assert stored is not None and sorted(json.loads(stored)) == sorted(REMOVED)

    # Cleared without approval: the next poll finds the same six missing and trips again.
    cleared = await _admin(app, linked)
    assert cleared["cleared"] == {
        "anomaly": "mass_delete",
        "reason": "6 of 9 documents missing from the root",
    }
    assert cleared["approved_documents"] == 0 and cleared["approval_expires_at"] is None
    assert "allow_mass_delete=true" in cleared["message"]
    for key in ("anomaly", "anomaly_reason", "anomaly_candidates", "allow_mass_delete"):
        assert _state(linked, key) is None
    await _poll(linked, loop)
    assert _state(linked, "anomaly") == "mass_delete"
    assert _tombstoned(linked) == frozenset()

    # Cleared with approval: the next poll moves the six to tombstones/, once.
    approved = await _admin(app, linked, allow_mass_delete=True)
    assert approved["approved_documents"] == 6 and approved["allow_mass_delete"] is True
    assert approved["approval_expires_at"] is not None
    assert "nothing is deleted" in approved["message"]
    assert _state(linked, "allow_mass_delete") is not None
    assert _state(linked, "anomaly") is None
    await _poll(linked, loop)

    assert _state(linked, "anomaly") is None
    assert _state(linked, "allow_mass_delete") is None  # spent
    assert _tombstoned(linked) == frozenset(REMOVED)
    for uuid in REMOVED:
        assert (linked.paths.tombstones / uuid).is_dir()
        assert not (linked.paths.library / uuid).exists()
    assert len(linked.index().document_uuids(deleted=False)) == 3

    # Nothing left to clear.
    refused = await _admin_error(app, _args(user=linked.uid))
    assert refused["code"] == "refused" and refused["reason"] == "no_anomaly"

    # Neither the candidates nor the reason reach a log line, the audit log or a result.
    logs = _server_logs(caplog)
    audit = json.dumps(app.audit_rows())
    for text in (logs, audit, json.dumps([cleared, approved])):
        assert "Synthetic reason" not in text
        assert not any(uuid in text for uuid in REMOVED)
    assert _audited(app) == [
        (
            "ok",
            {
                "acct": linked.acct,
                "allowed": False,
                "count": 0,
                "reason_chars": len(REASON),
                "user": linked.uid,
            },
        ),
        (
            "ok",
            {
                "acct": linked.acct,
                "allowed": True,
                "count": 6,
                "reason_chars": len(REASON),
                "user": linked.uid,
            },
        ),
        ("refused", {"allowed": False, "reason_chars": len(REASON), "user": linked.uid}),
    ]


async def test_hidden_from_users_and_refused_to_them(app: Served, linked: Linked) -> None:
    await _trip(linked)  # which tools a user is offered is checked in test_acceptance
    refused = await app.error(TOOL, _args(user=linked.uid))
    assert refused["code"] == "refused" and "administrators" in refused["message"]
    assert _state(linked, "anomaly") == "mass_delete"


@pytest.mark.parametrize(
    "args",
    [
        {"user": "0123456789ABCDEF"},
        {"user": "user@example.invalid"},
        {"user": "0123456789abcdef", "reason": ""},
        {"user": "0123456789abcdef", "reason": "   "},
        {"user": "0123456789abcdef", "reason": "x" * 201},
        {"user": "0123456789abcdef", "allow_mass_delete": "true"},
        {"user": "0123456789abcdef", "account": "01J0000000000000000000000A"},
    ],
)
async def test_bad_arguments_are_refused(app: Served, linked: Linked, args: dict[str, Any]) -> None:
    error = await _admin_error(app, {**_args(), **args})
    assert error["code"] == "invalid_argument"


async def test_an_unknown_user_is_not_found(app: Served) -> None:
    error = await _admin_error(app, _args(user="0123456789abcdef"))
    assert error["code"] == "not_found"


async def test_a_user_with_no_linked_account_is_not_found(
    tmp_path: Path, issuer: FakeIssuer, make_validator: Callable[..., OIDCValidator]
) -> None:
    data = tmp_path / "fresh"
    data.mkdir()
    async with serve(make_settings(data_dir=data), issuer, make_validator) as served:
        status = await served.ok("account_status")
        uid = status["user"]
        error = await served.error(TOOL, _args(user=uid), token=issuer.admin_token())
    assert error["code"] == "not_found"


async def test_without_an_anomaly_nothing_changes(app: Served, linked: Linked) -> None:
    loop = linked.loop()
    await _poll(linked, loop)
    linked.index().set_state("allow_mass_delete", "synthetic-unspent")
    refused = await _admin_error(app, _args(user=linked.uid, allow_mass_delete=True))
    assert refused["code"] == "refused" and refused["reason"] == "no_anomaly"
    assert _state(linked, "allow_mass_delete") == "synthetic-unspent"


async def test_approval_applies_only_to_a_recorded_mass_delete(app: Served, linked: Linked) -> None:
    index = linked.index()
    with index.transaction():
        index.set_state("anomaly", "generation_regressed")
        index.set_state("anomaly_reason", "root generation 5 is lower than 6")
    refused = await _admin_error(app, _args(user=linked.uid, allow_mass_delete=True))
    assert refused["code"] == "refused" and refused["reason"] == "not_mass_delete"
    assert _state(linked, "anomaly") == "generation_regressed"
    assert _state(linked, "allow_mass_delete") is None

    with index.transaction():  # tripped before candidates were stored, or damaged
        index.set_state("anomaly", "mass_delete")
        index.set_state("anomaly_candidates", "[not json")
    refused = await _admin_error(app, _args(user=linked.uid, allow_mass_delete=True))
    assert refused["code"] == "refused" and refused["reason"] == "no_candidates"
    assert _state(linked, "anomaly") == "mass_delete"
    assert _state(linked, "allow_mass_delete") is None

    cleared = await _admin(app, linked)
    assert cleared["cleared"]["anomaly"] == "mass_delete"
    assert _state(linked, "anomaly") is None and _state(linked, "anomaly_candidates") is None


async def test_clearing_without_approval_withdraws_an_unspent_one(
    app: Served, linked: Linked
) -> None:
    loop = await _trip(linked)
    await _admin(app, linked, allow_mass_delete=True)
    with linked.index().transaction():  # a later anomaly stopped a poll before planning
        linked.index().set_state("anomaly", "generation_regressed")
        linked.index().set_state("anomaly_reason", "root generation 5 is lower than 6")

    cleared = await _admin(app, linked)

    assert cleared["earlier_approval_withdrawn"] is True
    assert "withdrawn" in cleared["message"]
    assert _state(linked, "allow_mass_delete") is None
    await _poll(linked, loop)
    assert _state(linked, "anomaly") == "mass_delete"  # the six are not let through
    assert _tombstoned(linked) == frozenset()
