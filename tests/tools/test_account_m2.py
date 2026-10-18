"""The M2 account tools: set_sync_scope and disconnect_remarkable(keep_archive=false)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from scriptorium.accounts import device_token_name, poll_lock
from scriptorium.auth.oidc import OIDCValidator
from scriptorium.cloud.scope import SETTING_SYNC_SCOPE
from scriptorium.errors import NotFoundError
from scriptorium.tools import account as account_tools
from scriptorium.users.secrets import SecretBox
from tests.cloud.fake_cloud import NOTEBOOK, PDF
from tests.conftest import FakeIssuer, make_settings
from tests.tools.helpers import Served, serve, text_of
from tests.worker.helpers import Linked

WORK = "Synthetic Work"
"""A tag the tests put on the fixture notebook (no real tag names)."""
PERSONAL = "Synthetic Personal"


@pytest.fixture
async def fresh(
    tmp_path: Path, issuer: FakeIssuer, make_validator: Callable[..., OIDCValidator]
) -> AsyncIterator[Served]:
    data = tmp_path / "data"
    data.mkdir()
    async with serve(make_settings(data_dir=data), issuer, make_validator) as served:
        yield served


def _tag(linked: Linked, uuid: str, *tags: str) -> None:
    files = linked.cloud.docs[uuid]
    content = json.loads(files[f"{uuid}.content"])
    content["tags"] = [{"name": tag, "timestamp": 1} for tag in tags]
    linked.cloud.set_file(uuid, f"{uuid}.content", json.dumps(content).encode())


def _excluded(linked: Linked, uuid: str) -> bool:
    row = linked.index().document(uuid)
    assert row is not None
    return row.excluded_at is not None


# ---- set_sync_scope ------------------------------------------------------------------------------


async def test_set_sync_scope_before_linking_is_not_linked(fresh: Served) -> None:
    assert (await fresh.error("set_sync_scope", {"mode": "all"}))["code"] == "not_linked"


@pytest.mark.parametrize(
    "args",
    [
        {"mode": "all", "tags": [WORK]},
        {"mode": "allow"},
        {"mode": "deny", "tags": []},
        {"mode": "allow", "tags": ["   "]},
        {"mode": "allow", "tags": ["bell\x07"]},
        {"mode": "allow", "tags": [""]},
        {"mode": "everything"},
        {"mode": "allow", "tags": [f"t{n}" for n in range(101)]},
        {"mode": "allow", "tags": ["x" * 257]},
        {"mode": "allow", "tags": [WORK], "user": "0123456789abcdef"},
    ],
)
async def test_set_sync_scope_refuses_a_bad_scope(fresh: Served, args: dict[str, Any]) -> None:
    # Refused before any account is looked at (nobody is linked here).
    error = await fresh.error("set_sync_scope", args)
    assert error["code"] == "invalid_argument"
    assert "bell" not in json.dumps(error)


async def test_set_sync_scope_stores_the_scope_and_the_next_poll_applies_it(
    library: Served, linked: Linked, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _tag(linked, NOTEBOOK, WORK)
    await linked.sync()  # the tag is indexed

    allowed = await library.ok("set_sync_scope", {"mode": "allow", "tags": [f"  {WORK} "]})

    assert allowed["account"] == linked.acct
    assert allowed["scope"] == {"mode": "allow", "tags": [WORK]}
    assert allowed["previous"] == {"mode": "all", "tags": []}
    assert allowed["changed"] is True
    assert allowed["unmatched_tags"] == []
    assert "Nothing archived here is deleted" in allowed["message"]
    stored = linked.index().get_setting(SETTING_SYNC_SCOPE)
    assert stored is not None and json.loads(stored) == {"mode": "allow", "tags": [WORK]}
    before = sorted(p for p in linked.paths.root.rglob("*") if p.is_file())

    await linked.sync()

    assert not _excluded(linked, NOTEBOOK) and _excluded(linked, PDF)
    after = {p for p in linked.paths.root.rglob("*") if p.is_file()}
    assert set(before) - after <= {p for p in before if p.name.endswith(("-wal", "-shm"))}

    again = await library.ok("set_sync_scope", {"mode": "allow", "tags": [WORK]})
    assert again["changed"] is False

    denied = await library.ok("set_sync_scope", {"mode": "deny", "tags": [WORK, PERSONAL]})
    assert denied["scope"] == {"mode": "deny", "tags": sorted([WORK, PERSONAL])}
    assert denied["unmatched_tags"] == [PERSONAL]
    assert "spelling" in denied["message"]
    await linked.sync()
    assert _excluded(linked, NOTEBOOK) and not _excluded(linked, PDF)

    everything = await library.ok("set_sync_scope", {"mode": "all"})
    assert everything["scope"] == {"mode": "all", "tags": []}
    assert linked.index().get_setting(SETTING_SYNC_SCOPE) is None  # the default: no row
    await linked.sync()
    assert not _excluded(linked, NOTEBOOK) and not _excluded(linked, PDF)

    # Tag names are the user's: returned to them, never logged or audited. (The test's own
    # MCP client logs what it sends; only the server side counts.)
    server_side = "\n".join(
        f"{r.getMessage()} {getattr(r, 'scriptorium_fields', '')}"
        for r in caplog.records
        if not r.name.startswith("mcp.client")
    )
    assert WORK not in server_side and PERSONAL not in server_side
    audit = [row for row in library.audit_rows() if "set_sync_scope" in repr(row)]
    assert audit and all(WORK not in repr(row) and PERSONAL not in repr(row) for row in audit)
    assert any(
        '"count":2' in repr(row).replace(" ", "") and linked.acct in repr(row) for row in audit
    )


async def test_a_damaged_stored_scope_is_replaced(library: Served, linked: Linked) -> None:
    linked.index().set_setting(SETTING_SYNC_SCOPE, "{not json")
    result = await library.ok("set_sync_scope", {"mode": "all"})
    assert result["previous"] is None and result["changed"] is True
    assert linked.index().get_setting(SETTING_SYNC_SCOPE) is None


# ---- disconnect_remarkable(keep_archive=false) ---------------------------------------------------


def _files(root: Path) -> set[Path]:
    """Every file under ``root`` but SQLite's transient journal files, relative to it."""
    return {
        p.relative_to(root)
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(("-wal", "-shm", "-journal"))
    }


def _moved_to(linked: Linked) -> Path:
    (moved,) = (linked.settings.data_dir / "users" / linked.uid / "removed").iterdir()
    return moved


async def _dry_run(library: Served) -> dict[str, Any]:
    return await library.ok("disconnect_remarkable", {"keep_archive": False})


async def _execute(library: Served, confirmation: str, *, as_user: str | None = None) -> Any:
    """The execute call's structured result, or its error payload."""
    args = {"keep_archive": False, "confirmation_token": confirmation}
    result = await library.call("disconnect_remarkable", args, token=as_user)
    return json.loads(text_of(result))


async def test_the_dry_run_changes_nothing_and_reports_counts(
    library: Served, linked: Linked, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    before = _files(linked.paths.root)

    dry = await _dry_run(library)

    assert dry["dry_run"] is True and dry["account"] == linked.acct and dry["linked"] is True
    assert set(dry["summary"]) == {"documents", "pages", "blobs", "bytes"}
    assert dry["summary"]["documents"] == 2
    assert dry["summary"]["pages"] > 0
    assert dry["summary"]["blobs"] == sum(1 for p in before if p.parts[0] == "blobs")
    assert dry["summary"]["bytes"] >= sum((linked.paths.root / p).stat().st_size for p in before)
    assert re.fullmatch(r"[A-Za-z0-9_-]{54}", dry["confirmation_token"])
    assert dry["expires_at"].endswith("Z")
    assert _files(linked.paths.root) == before
    row = linked.store.account(linked.uid, linked.acct)
    assert row is not None and row.active and row.removed_at is None
    assert SecretBox(linked.settings, linked.uid).open(device_token_name(linked.acct))
    # The token is the caller's to hold: it is neither logged nor audited.
    server_side = "\n".join(
        f"{r.getMessage()} {getattr(r, 'scriptorium_fields', '')}"
        for r in caplog.records
        if not r.name.startswith("mcp.client")
    )
    assert dry["confirmation_token"] not in server_side
    assert all(dry["confirmation_token"] not in repr(row) for row in library.audit_rows())


async def test_executing_moves_the_account_aside_and_deletes_nothing(
    library: Served, linked: Linked
) -> None:
    before = _files(linked.paths.root)
    dry = await _dry_run(library)

    done = await _execute(library, dry["confirmation_token"])

    assert done["removed"] is True and done["account"] == linked.acct
    assert done["archive_kept"] is False and "my.remarkable.com" in done["message"]
    assert not linked.paths.root.exists()
    moved = _moved_to(linked)
    assert moved.name.startswith(f"{linked.acct}-") and moved.name.endswith("Z")
    assert before <= _files(moved)  # one rename: every file is there, nothing deleted
    row = linked.store.account(linked.uid, linked.acct)
    assert row is not None and not row.active and row.removed_at is not None
    assert SecretBox(linked.settings, linked.uid).open(device_token_name(linked.acct)) is None
    assert linked.store.active_accounts() == []

    status = await library.ok("account_status")
    assert status["status"] == "not_linked"
    (other,) = status["other_accounts"]
    assert other["account"] == linked.acct and other["removed_at"].endswith("Z")
    assert (await library.error("list", {"account": linked.acct}))["code"] == "not_found"
    assert (await library.error("list"))["code"] == "not_linked"
    with pytest.raises(NotFoundError):
        library.services.index(linked.uid, linked.acct)
    assert not linked.paths.root.exists()  # nothing above recreated it

    again = await _execute(library, dry["confirmation_token"])
    assert (again["code"], again["reason"]) == ("refused", "used")
    # A new link is a new account: the removed one is never reused.
    relinked = await library.ok("connect_remarkable", {"code": "abcd1234"})
    assert relinked["account"] != linked.acct


async def test_a_removal_token_is_bound_to_its_user_account_and_time(
    library: Served, linked: Linked
) -> None:
    token = (await _dry_run(library))["confirmation_token"]
    keep = await library.error(
        "disconnect_remarkable", {"keep_archive": True, "confirmation_token": token}
    )
    assert keep["code"] == "invalid_argument"
    malformed = await _execute(library, "not-a-token")
    assert (malformed["code"], malformed["reason"]) == ("refused", "malformed")
    # Another user with an account of their own cannot use it.
    other = library.issuer.admin_token()
    library.registrar.token = "second-device-token"
    library.identities["second-device-token"] = "b" * 64
    await library.ok("connect_remarkable", {"code": "abcd1234"}, token=other)
    stolen = await _execute(library, token, as_user=other)
    assert (stolen["code"], stolen["reason"]) == ("refused", "not_bound")
    # Ten minutes later it has expired.
    linked.clock.now += timedelta(minutes=11)
    late = await _execute(library, token)
    assert (late["code"], late["reason"]) == ("refused", "expired")
    assert linked.paths.root.is_dir()
    # A token from before a relink names the old account.
    fresh = (await _dry_run(library))["confirmation_token"]
    await library.ok("disconnect_remarkable")
    library.registrar.token = "third-device-token"
    library.identities["third-device-token"] = "c" * 64
    await library.ok("connect_remarkable", {"code": "abcd1234"})
    moved = await _execute(library, fresh)
    assert (moved["code"], moved["reason"]) == ("refused", "not_bound")
    assert linked.paths.root.is_dir()


async def test_a_running_poll_makes_the_removal_wait_then_refuse(
    library: Served, linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(account_tools, "REMOVE_LOCK_WAIT_S", 0.3)
    token = (await _dry_run(library))["confirmation_token"]
    with poll_lock(linked.paths) as held:
        assert held == "held"
        busy = await _execute(library, token)
    assert (busy["code"], busy["reason"]) == ("refused", "busy")
    assert linked.paths.root.is_dir()
    row = linked.store.account(linked.uid, linked.acct)
    assert row is not None and row.active
    # The token was not spent: once the poll is over it works.
    done = await _execute(library, token)
    assert done["removed"] is True
    assert not linked.paths.root.exists()


async def test_a_failed_move_leaves_the_archive_readable(
    library: Served, linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(paths: object, destination: object) -> None:
        raise PermissionError("synthetic")

    monkeypatch.setattr(account_tools, "move_aside", fail)
    token = (await _dry_run(library))["confirmation_token"]

    failed = await _execute(library, token)

    assert (failed["code"], failed["reason"]) == ("refused", "move_failed")
    row = linked.store.account(linked.uid, linked.acct)
    assert row is not None and not row.active and row.removed_at is None
    assert linked.paths.root.is_dir()
    listed = await library.ok("list", {"account": linked.acct})
    assert {item["uuid"] for item in listed["items"]} == {NOTEBOOK, PDF}
    # Once the cause is fixed, a new dry run and execute finish the job.
    monkeypatch.undo()
    done = await _execute(library, (await _dry_run(library))["confirmation_token"])
    assert done["removed"] is True and not linked.paths.root.exists()


async def test_a_failure_before_the_move_puts_the_account_back(
    library: Served, linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(box: object, name: str) -> None:
        raise OSError("synthetic")

    monkeypatch.setattr(SecretBox, "forget", fail)
    failed = await _execute(library, (await _dry_run(library))["confirmation_token"])

    assert (failed["code"], failed["reason"]) == ("refused", "move_failed")
    row = linked.store.account(linked.uid, linked.acct)
    assert row is not None and row.removed_at is None  # never removed with its directory here
    assert linked.paths.root.is_dir()
    monkeypatch.undo()
    done = await _execute(library, (await _dry_run(library))["confirmation_token"])
    assert done["removed"] is True
    assert SecretBox(linked.settings, linked.uid).open(device_token_name(linked.acct)) is None


async def test_a_removal_waits_for_a_connect_of_the_same_user(
    library: Served, linked: Linked
) -> None:
    token = (await _dry_run(library))["confirmation_token"]
    async with library.services.connect_lock(linked.uid):
        pending = asyncio.create_task(_execute(library, token))
        await asyncio.sleep(0.3)
        assert not pending.done()
        assert linked.paths.root.is_dir()
    done = await pending
    assert done["removed"] is True


async def test_an_archive_kept_at_disconnect_can_be_removed_later(
    library: Served, linked: Linked
) -> None:
    await library.ok("disconnect_remarkable")
    dry = await _dry_run(library)
    assert dry["account"] == linked.acct and dry["linked"] is False
    done = await _execute(library, dry["confirmation_token"])
    assert done["removed"] is True
    refused = await library.error("disconnect_remarkable", {"keep_archive": False})
    assert refused["code"] == "not_linked"
