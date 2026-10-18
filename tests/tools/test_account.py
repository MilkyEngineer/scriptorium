"""connect_remarkable, disconnect_remarkable and account_status (M1 acceptance 5, 6 and 8)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from scriptorium.accounts import STATE_NEEDS_RELINK, device_token_name
from scriptorium.auth.oidc import OIDCValidator
from scriptorium.jobs.kinds import render_key
from scriptorium.library import pages as pg
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import global_db_path, worker_heartbeat_path
from tests.cloud.fake_cloud import NOTEBOOK, PAGES, PDF, TOKEN_KEY, FakeCloud
from tests.cloud.scenarios import _partial, _remove_six, nine
from tests.conftest import FakeIssuer, make_settings
from tests.tools.helpers import BAD_CODE, IDENTITY, Served, serve
from tests.worker.helpers import Linked, link

CODE = "abcd1234"
ACCT = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
SECOND_TOKEN = "second-device-token"
SECOND_IDENTITY = "b" * 64


@pytest.fixture
async def fresh(
    tmp_path: Path, issuer: FakeIssuer, make_validator: Callable[..., OIDCValidator]
) -> AsyncIterator[Served]:
    """The app over an empty ``/data``: nobody has linked an account yet."""
    data = tmp_path / "data"
    data.mkdir()
    async with serve(make_settings(data_dir=data), issuer, make_validator) as served:
        yield served


def _codes(status: dict[str, Any]) -> list[str]:
    return [warning["code"] for warning in status["warnings"]]


def _uid(served: Served) -> str:
    (row,) = {row.uid: row for row in served.services.store.all_accounts()}.values()
    return row.uid


def _files_containing(root: Path, needle: bytes) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and needle in p.read_bytes()]


def _archive_files(root: Path) -> set[Path]:
    """Every file under an account root, but SQLite's transient journal files."""
    return {
        p.relative_to(root)
        for p in root.glob("*/accounts/**/*")
        if p.is_file() and not p.name.endswith(("-wal", "-shm", "-journal"))
    }


def _fresh_heartbeat(linked: Linked) -> None:
    linked.worker.write_heartbeat()


# ---- connect_remarkable ------------------------------------------------------------------------


async def test_before_linking_status_explains_how_to_connect(fresh: Served) -> None:
    status = await fresh.ok("account_status")
    assert status["linked"] is False
    assert status["status"] == "not_linked"
    assert "connect_remarkable" in status["message"]
    assert status["ocr"]["mode"] == "lazy"
    for tool, args in (
        ("list", {}),
        ("trash", {}),
        ("read_page", {"document": NOTEBOOK, "page": 1}),
        ("disconnect_remarkable", {}),
    ):
        assert (await fresh.error(tool, args))["code"] == "not_linked", tool


async def test_connect_links_seals_the_token_and_waits_for_the_first_sync(
    fresh: Served,
) -> None:
    linked = await fresh.ok("connect_remarkable", {"code": CODE})
    assert linked["linked"] is True
    assert ACCT.match(linked["account"])
    assert linked["replaced"] == []
    assert "desktop-linux" in linked["message"]
    assert "my.remarkable.com" in linked["message"]
    assert fresh.registrar.calls == ["desktop-linux"]
    # The token is sealed with the master keys and never returned or stored in clear.
    uid = _uid(fresh)
    box = SecretBox(fresh.settings, uid)
    assert box.open(device_token_name(linked["account"])) == fresh.registrar.token.encode()
    token = fresh.registrar.token.encode()
    assert _files_containing(fresh.settings.data_dir, token) == []
    status = await fresh.ok("account_status")
    assert token.decode() not in json.dumps(status)
    assert status["linked"] is True
    assert status["account"] == linked["account"]
    assert status["status"] == "waiting_for_first_sync"
    assert "worker_stale" in _codes(status)  # no worker has written a heartbeat here
    assert status["other_accounts"] == []
    assert status["sync"]["last_full_listing_ok"] is None
    assert status["quota"] is None


async def test_a_second_connect_is_refused_before_registering(fresh: Served) -> None:
    first = await fresh.ok("connect_remarkable", {"code": CODE})
    refused = await fresh.error("connect_remarkable", {"code": CODE})
    assert refused["code"] == "refused"
    assert "replace" in refused["hint"]
    assert len(fresh.registrar.calls) == 1  # no orphan device at reMarkable
    # replace=true with another reMarkable account: a new acct; the old one is kept, inactive.
    fresh.registrar.token = SECOND_TOKEN
    fresh.identities[SECOND_TOKEN] = SECOND_IDENTITY
    second = await fresh.ok("connect_remarkable", {"code": CODE, "replace": True})
    assert second["account"] != first["account"]
    assert second["replaced"] == [first["account"]]
    assert "remove that device" in second["message"]
    # The replaced account's token is deleted with it; only the new one stays sealed.
    box = SecretBox(fresh.settings, _uid(fresh))
    assert box.open(device_token_name(first["account"])) is None
    assert box.open(device_token_name(second["account"])) == SECOND_TOKEN.encode()
    status = await fresh.ok("account_status")
    assert status["account"] == second["account"]
    assert [(a["account"], a["active"]) for a in status["other_accounts"]] == [
        (first["account"], False)
    ]
    old = await fresh.ok("account_status", {"account": first["account"]})
    assert old["status"] == "disconnected"
    assert old["linked"] is False


async def test_relinking_the_same_remarkable_account_reuses_its_archive(fresh: Served) -> None:
    first = await fresh.ok("connect_remarkable", {"code": CODE})
    again = await fresh.ok("connect_remarkable", {"code": CODE, "replace": True})
    assert again["account"] == first["account"]
    assert again["replaced"] == []
    assert "remove that device" in again["message"]  # the first device is orphaned
    assert len(fresh.services.store.all_accounts()) == 1
    box = SecretBox(fresh.settings, _uid(fresh))
    assert box.open(device_token_name(first["account"])) == fresh.registrar.token.encode()


async def test_a_rejected_code_links_nothing(fresh: Served) -> None:
    refused = await fresh.error("connect_remarkable", {"code": BAD_CODE})
    assert refused["code"] == "refused"
    assert (await fresh.ok("account_status"))["status"] == "not_linked"
    invalid = await fresh.error("connect_remarkable", {"code": "no spaces; allowed"})
    assert invalid["code"] == "invalid_argument"
    assert fresh.registrar.calls == ["desktop-linux"]  # only the rejected code reached it


async def test_an_account_linked_to_another_user_is_refused(fresh: Served) -> None:
    await fresh.ok("connect_remarkable", {"code": CODE})
    other = fresh.issuer.admin_token()
    refused = await fresh.error("connect_remarkable", {"code": CODE}, token=other)
    assert refused["code"] == "refused"
    assert "another user" in refused["message"]
    assert "my.remarkable.com" in refused["hint"]  # the attempt left a device to remove
    assert len(fresh.registrar.calls) == 2
    assert (await fresh.ok("account_status", token=other))["status"] == "not_linked"
    # Nothing was sealed for the refused user: the only secrets file is the first user's.
    uid = _uid(fresh)
    secrets = list(fresh.settings.data_dir.rglob("secrets.enc"))
    assert [path.parent.name for path in secrets] == [uid]


async def test_another_users_account_is_not_found(fresh: Served) -> None:
    mine = await fresh.ok("connect_remarkable", {"code": CODE})
    other = fresh.issuer.admin_token()
    fresh.registrar.token = SECOND_TOKEN
    fresh.identities[SECOND_TOKEN] = SECOND_IDENTITY
    await fresh.ok("connect_remarkable", {"code": CODE}, token=other)
    for tool, args in (
        ("account_status", {}),
        ("list", {}),
        ("trash", {}),
        ("recent", {"since": "2026-01-01T00:00:00Z"}),
        ("read_page", {"document": NOTEBOOK, "page": 1}),
    ):
        error = await fresh.error(tool, {**args, "account": mine["account"]}, token=other)
        assert error["code"] == "not_found", (tool, error)


# ---- disconnect_remarkable ---------------------------------------------------------------------


async def test_disconnect_keeps_the_archive_and_forgets_the_token(fresh: Served) -> None:
    linked = await fresh.ok("connect_remarkable", {"code": CODE})
    refused = await fresh.error("disconnect_remarkable", {"keep_archive": False})
    assert refused["code"] == "refused"
    confirm = await fresh.error("disconnect_remarkable", {"confirmation_token": "x"})
    assert confirm["code"] == "invalid_argument"
    assert (await fresh.ok("account_status"))["linked"] is True
    root = fresh.settings.data_dir / "users"
    before = _archive_files(root)
    gone = await fresh.ok("disconnect_remarkable")
    assert gone["disconnected"] == [linked["account"]]
    assert gone["archive_kept"] is True
    assert "my.remarkable.com" in gone["message"]
    box = SecretBox(fresh.settings, _uid(fresh))
    assert box.open(device_token_name(linked["account"])) is None
    assert before <= _archive_files(root)  # nothing of the archive was removed
    status = await fresh.ok("account_status")
    assert status["status"] == "disconnected"
    assert status["linked"] is False
    assert "worker_stale" not in _codes(status)  # nothing is meant to sync
    assert (await fresh.error("disconnect_remarkable"))["code"] == "not_linked"
    # The archive stays readable by its account id.
    assert (await fresh.ok("list", {"account": linked["account"]}))["items"] == []


# ---- account_status after real syncs -----------------------------------------------------------


async def test_status_of_a_synced_account(linked: Linked, library: Served) -> None:
    _fresh_heartbeat(linked)
    status = await library.ok("account_status")
    assert status["status"] == "ok", status["warnings"]
    assert status["warnings"] == []
    assert status["account"] == linked.acct
    assert status["sync"]["last_full_listing_ok"] is True
    assert status["sync"]["last_poll_outcome"] == "ok"
    assert status["sync"]["last_sync_at"] is not None
    assert status["sync"]["token_rejections"] == 0
    assert status["library"]["documents"] == 2
    assert status["jobs"]["dead"] == 0
    assert all(n == 0 for n in status["jobs"]["pending"].values())
    assert 0 <= status["worker"]["heartbeat_age_s"] < 60
    assert status["worker"]["render_pool_rebuilds"] == 0
    linked.worker.render_pool_rebuilds = 3
    _fresh_heartbeat(linked)
    status = await library.ok("account_status")
    assert status["worker"]["render_pool_rebuilds"] == 3
    worker_heartbeat_path(linked.settings.data_dir).write_text("2026-10-01T12:00:00Z\n")
    status = await library.ok("account_status")  # an old-style heartbeat body
    assert status["worker"]["render_pool_rebuilds"] is None
    assert status["warnings"] == []


async def test_status_warns_of_a_stale_worker_and_failed_renders(
    linked: Linked, library: Served
) -> None:
    path = worker_heartbeat_path(linked.settings.data_dir)
    _fresh_heartbeat(linked)
    old = time.time() - 600
    os.utime(path, (old, old))
    version = pg.current_version(linked.paths, linked.index(), NOTEBOOK)
    assert version is not None
    sources = pg.page_sources(linked.paths, version)
    conn = sqlite3.connect(global_db_path(linked.settings.data_dir))
    try:
        with conn:
            # One verdict on the page, one run that failed (a broken render pool).
            for idx, error in ((1, "invalid_argument"), (2, "internal")):
                conn.execute(
                    "UPDATE jobs SET state = 'dead', error = ? WHERE dedupe_key = ?",
                    (error, render_key(linked.acct, PAGES[idx], sources[idx].content_hash)),
                )
    finally:
        conn.close()
    status = await library.ok("account_status")
    assert set(_codes(status)) == {"worker_stale", "render_failures"}
    assert status["worker"]["heartbeat_age_s"] >= 600
    assert status["worker"]["render_pool_rebuilds"] == 0
    assert status["jobs"]["dead"] == 2
    (failures,) = [w for w in status["warnings"] if w["code"] == "render_failures"]
    assert (failures["count"], failures["rerunnable"]) == (2, 1)
    assert status["status"] == "ok"  # warnings, but the archive is served and syncing


async def test_partial_listing_tombstones_nothing(
    tmp_path: Path, issuer: FakeIssuer, make_validator: Callable[..., OIDCValidator]
) -> None:
    """Acceptance #5."""
    linked = link(tmp_path)
    try:
        loop = await linked.sync()
        _partial(linked.cloud)
        for _ in range(2):
            await loop.poll_once(linked.context())
        async with serve(linked.settings, issuer, make_validator, clock=linked.clock) as library:
            status = await library.ok("account_status")
            assert status["sync"]["last_full_listing_ok"] is False
            assert status["sync"]["last_poll_outcome"] == "partial"
            assert (await library.ok("trash"))["items"] == []
            listed = {item["uuid"] for item in (await library.ok("list"))["items"]}
            assert listed == {NOTEBOOK, PDF}
        assert not any(linked.paths.tombstones.rglob("*"))
    finally:
        linked.close()


async def test_mass_delete_halts_and_status_says_so(
    tmp_path: Path, issuer: FakeIssuer, make_validator: Callable[..., OIDCValidator]
) -> None:
    """Acceptance #6: six of nine documents (67 %) disappear at once."""
    cloud = FakeCloud()
    nine(cloud)
    linked = link(tmp_path, cloud=cloud)
    try:
        loop = await linked.sync()
        _remove_six(cloud)
        for _ in range(3):
            await loop.poll_once(linked.context())
        async with serve(linked.settings, issuer, make_validator, clock=linked.clock) as library:
            _fresh_heartbeat(linked)
            status = await library.ok("account_status")
            assert status["status"] == "anomaly"
            (warning,) = status["warnings"]
            assert warning["code"] == "sync_anomaly"
            assert warning["anomaly"] == "mass_delete"
            assert (await library.ok("trash"))["items"] == []
            assert len((await library.ok("list", {"limit": 100}))["items"]) == 9
        assert not any(linked.paths.tombstones.rglob("*"))
    finally:
        linked.close()


async def test_a_token_outage_backs_off_without_needing_a_relink(
    linked: Linked, library: Served
) -> None:
    """Acceptance #8, first half, as account_status reports it."""
    linked.cloud.status[TOKEN_KEY] = 503
    loop = linked.loop()
    for _ in range(3):
        await loop.poll_once(linked.context())
    _fresh_heartbeat(linked)
    status = await library.ok("account_status")
    assert status["status"] == "ok"
    assert status["sync"]["last_poll_outcome"] == "backoff"
    assert status["sync"]["token_rejections"] == 0


async def test_a_rejected_token_needs_a_relink_and_connect_clears_it(
    linked: Linked, library: Served
) -> None:
    """Acceptance #8, second half: three rejections, then connect_remarkable(replace=true)."""
    linked.cloud.device_token = "a-new-device-token"  # the stored token is refused
    loop = linked.loop()
    for _ in range(3):
        await loop.poll_once(linked.context())
    _fresh_heartbeat(linked)
    status = await library.ok("account_status")
    assert status["status"] == "needs_relink"
    assert _codes(status) == ["needs_relink"]
    assert "connect_remarkable" in status["warnings"][0]["hint"]
    assert status["sync"]["token_rejections"] == 3
    # Relinking the same reMarkable account keeps its acct, archive and history.
    library.registrar.token = "a-new-device-token"
    library.identities["a-new-device-token"] = IDENTITY
    again = await library.ok("connect_remarkable", {"code": CODE, "replace": True})
    assert again["account"] == linked.acct
    assert linked.index().get_state(STATE_NEEDS_RELINK) is None
    assert (await library.ok("account_status"))["status"] == "ok"
    seen = len(linked.cloud.requests)
    await loop.poll_once(linked.context())
    assert len(linked.cloud.requests) > seen  # polling resumed with the new token
    assert linked.index().get_state(STATE_NEEDS_RELINK) is None
