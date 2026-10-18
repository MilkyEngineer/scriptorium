"""The worker's M2 duties: render priority, removed accounts, the poll lock, poke, reaping."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scriptorium.accounts import STATE_LAST_POLL_OUTCOME, poll_lock
from scriptorium.cloud.client import CloudClient
from scriptorium.cloud.sync import SyncReport
from scriptorium.jobs.kinds import GONE_RESULT, RenderSpec
from scriptorium.jobs.queue import PRIORITY_LOW, PRIORITY_NORMAL, JobQueue
from scriptorium.library import pages as pg
from scriptorium.library.index import DocumentRow
from scriptorium.worker import AccountContext, AccountLoop, render_priority
from tests.cloud.fake_cloud import (
    DEVICE_TOKEN,
    NOTEBOOK,
    PAGES,
    PDF,
    FakeCloud,
    fixture_documents,
)
from tests.worker.helpers import FAST, T0, Linked, link

POLL = 600


@pytest.fixture
def linked(tmp_path: Path) -> Any:
    linked = link(tmp_path)
    yield linked
    linked.close()


def report(outcome: str, **kw: Any) -> SyncReport:
    return SyncReport(outcome=outcome, started_at=T0, finished_at=T0, **kw)  # type: ignore[arg-type]


def priorities(queue: JobQueue) -> dict[str, set[int]]:
    """Document UUID → the priorities of its render jobs."""
    with queue._lock:
        rows = queue._conn.execute("SELECT payload, priority FROM jobs").fetchall()
    out: dict[str, set[int]] = {}
    for row in rows:
        out.setdefault(json.loads(row["payload"])["document"], set()).add(row["priority"])
    return out


def fields(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    return [getattr(r, "scriptorium_fields", {}) for r in caplog.records if r.getMessage() == event]


# ---- render priority --------------------------------------------------------------------------


def test_render_priority() -> None:
    live = DocumentRow(
        uuid=NOTEBOOK,
        type="DocumentType",
        parent=None,
        name="Synthetic",
        last_modified=T0,
        last_opened=None,
        deleted_at=None,
        deletion_cause=None,
        source_kind="notebook",
        page_count=1,
        hash_root="a" * 64,
        generation=1,
    )
    when = datetime(2026, 10, 2, tzinfo=UTC)
    assert render_priority(live) == PRIORITY_NORMAL
    for low in (
        None,
        dataclasses.replace(live, parent="trash"),
        dataclasses.replace(live, deleted_at=when, deletion_cause="unknown"),
        dataclasses.replace(live, excluded_at=when),
    ):
        assert render_priority(low) == PRIORITY_LOW


async def test_a_trashed_documents_pages_render_after_live_ones(tmp_path: Path) -> None:
    docs = fixture_documents()
    meta = json.loads(docs[PDF][f"{PDF}.metadata"])
    meta["parent"] = "trash"
    docs[PDF][f"{PDF}.metadata"] = json.dumps(meta).encode()
    cloud = FakeCloud()
    cloud.put_documents(docs)
    linked = link(tmp_path, cloud=cloud)
    try:
        await linked.loop().poll_once(linked.context())
        assert priorities(linked.queue) == {NOTEBOOK: {PRIORITY_NORMAL}, PDF: {PRIORITY_LOW}}
        first = linked.queue.lease("w", kinds=["render"])
        assert first is not None and first.payload["document"] == NOTEBOOK
    finally:
        linked.close()


async def test_tombstoning_a_document_lowers_its_waiting_renders(
    linked: Linked, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    loop = linked.loop()
    await loop.poll_once(linked.context())  # queues every page, runs none
    assert priorities(linked.queue) == {NOTEBOOK: {PRIORITY_NORMAL}, PDF: {PRIORITY_NORMAL}}

    loop.after_poll(linked.context(), report("ok", tombstoned=(NOTEBOOK,)))

    assert priorities(linked.queue) == {NOTEBOOK: {PRIORITY_LOW}, PDF: {PRIORITY_NORMAL}}
    lowered = fields(caplog, "renders_deprioritised")
    assert lowered and lowered[0]["count"] >= 1 and NOTEBOOK not in repr(lowered)


# ---- removed accounts and the poll lock -------------------------------------------------------


async def test_render_jobs_of_a_removed_account_are_gone(linked: Linked) -> None:
    spec = RenderSpec(NOTEBOOK, PAGES[0], "a" * 64, 1)
    job = linked.queue.enqueue(
        uid=linked.uid,
        acct=linked.acct,
        kind=spec.kind,
        dedupe_key="render:removed",
        payload=spec.payload(),
    )
    linked.store.mark_removed(linked.uid, linked.acct, at=T0)

    assert await linked.worker.drain_jobs() == 1

    done = linked.queue.get(job.id, linked.uid)
    assert done is not None and (done.state, done.result) == ("done", dict(GONE_RESULT))
    assert linked.queue.rerun(done) is None
    assert not linked.paths.pages.exists()


async def test_a_page_image_whose_account_goes_while_rendering_writes_nothing(
    linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base PNG a page_image render needs is not written once the account has gone: a
    write would recreate the moved account's directory."""
    await linked.loop().poll_once(linked.context())  # no renders run yet
    page = linked.index().page(NOTEBOOK, 0)
    version = pg.current_version(linked.paths, linked.index(), NOTEBOOK)
    assert page is not None and version is not None
    spec = RenderSpec(NOTEBOOK, page.page_id, page.content_hash, version.generation, 2048)
    job = linked.queue.enqueue(
        uid=linked.uid,
        acct=linked.acct,
        kind=spec.kind,
        dedupe_key=spec.key(linked.acct),
        payload=spec.payload(),
    )
    asked: list[int] = []

    def gone(row: Any) -> bool:
        # The archive renders the poll queued see a removed account; the page_image job
        # sees it live when it starts and removed once its base render is done.
        if row.id != job.id:
            return True
        asked.append(row.id)
        return len(asked) > 1

    monkeypatch.setattr(linked.worker, "_account_gone", gone)

    await linked.worker.drain_jobs()

    done = linked.queue.get(job.id, linked.uid)
    assert done is not None and (done.state, done.result) == ("done", dict(GONE_RESULT))
    assert len(asked) == 2
    pages = linked.paths.pages
    assert not pages.exists() or not any(pages.rglob("*.png"))


async def test_a_render_that_loses_the_race_with_a_removal_recreates_nothing(
    linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory moves after the worker's last check and before the PNG is written:
    write_png refuses to recreate it and the job completes as gone."""
    await linked.loop().poll_once(linked.context())  # queues the archive renders
    root = linked.paths.root
    moved = root.with_name(root.name + "-moved")
    calls: dict[int, int] = {}
    mover: list[int] = []

    def gone(row: Any) -> bool:
        n = calls[row.id] = calls.get(row.id, 0) + 1
        if n == 2 and not mover:
            # The check right before the write: the removal is recorded and the directory
            # moved just after it answers.
            mover.append(row.id)
            root.rename(moved)
            return False
        return n > 2 or bool(mover and row.id != mover[0])

    monkeypatch.setattr(linked.worker, "_account_gone", gone)

    await linked.worker.drain_jobs()

    assert mover and not root.exists()
    done = linked.queue.get(mover[0], linked.uid)
    assert done is not None and (done.state, done.result) == ("done", dict(GONE_RESULT))
    assert not any(moved.glob("pages/*/*.png"))


async def test_a_poll_does_nothing_once_the_directory_has_moved(
    linked: Linked, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    loop = linked.loop()
    ctx = linked.context()
    moved = linked.paths.root.with_name(linked.paths.root.name + "-moved")
    linked.paths.root.rename(moved)
    requests = len(linked.cloud.requests)

    assert await loop.poll_once(ctx) == FAST.recheck_s

    assert len(linked.cloud.requests) == requests
    assert not linked.paths.root.exists()  # nothing recreated it
    assert {"acct": linked.acct, "reason": "account_moved"} in fields(caplog, "poll_skipped")


async def test_a_poll_waits_for_a_held_lock(linked: Linked) -> None:
    loop = linked.loop()
    ctx = linked.context()
    with poll_lock(linked.paths) as held:
        assert held == "held"
        task = asyncio.create_task(loop.poll_once(ctx))
        await asyncio.sleep(0.2)
        assert not task.done()  # the poll thread waits on the lock
    assert await asyncio.wait_for(task, 10) == POLL


# ---- poke ---------------------------------------------------------------------------------------


async def test_a_poke_brings_the_next_poll_forward_after_a_good_poll(linked: Linked) -> None:
    loop = linked.loop()
    ctx = linked.context()
    stop = asyncio.Event()
    loop.after_poll(ctx, report("ok"))
    loop._ended_at = asyncio.get_running_loop().time() - 3600  # the gap has passed
    loop.poke()

    await asyncio.wait_for(loop.pause(stop, POLL), 5)  # well before POLL seconds


async def test_a_poke_waits_for_the_minimum_gap(linked: Linked) -> None:
    loop = linked.loop()
    ctx = linked.context()
    stop = asyncio.Event()
    loop.after_poll(ctx, report("ok"))
    now = asyncio.get_running_loop().time()
    gap = linked.settings.notify_min_gap_seconds
    loop._ended_at = now - gap + 0.3  # the gap ends in 0.3 s

    pausing = asyncio.create_task(loop.pause(stop, POLL))
    loop.poke()
    await asyncio.sleep(0.1)
    assert not pausing.done()
    await asyncio.wait_for(pausing, 5)
    assert asyncio.get_running_loop().time() - now >= 0.3


async def test_a_poke_does_not_cut_a_backoff_short(linked: Linked) -> None:
    loop = linked.loop()
    ctx = linked.context()
    stop = asyncio.Event()
    loop.after_poll(ctx, report("backoff", reason="http_503"))
    loop._ended_at = asyncio.get_running_loop().time() - 3600
    loop.poke()

    pausing = asyncio.create_task(loop.pause(stop, 0.5))
    await asyncio.sleep(0.2)
    assert not pausing.done()
    await asyncio.wait_for(pausing, 5)
    stop.set()


# ---- reaping a poll the watchdog gave up on ---------------------------------------------------


async def test_an_abandoned_poll_that_fails_is_logged_by_class_only(
    linked: Linked, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    linked.worker.settings = linked.settings.model_copy(update={"poll_timeout_seconds": 0.1})
    release = threading.Event()

    def stuck(self: AccountLoop, ctx: AccountContext, client: CloudClient) -> SyncReport:
        release.wait(10)
        raise RuntimeError("synthetic detail that must not be logged")

    monkeypatch.setattr(AccountLoop, "_sync", stuck)
    loop = linked.loop()
    ctx = linked.context()
    assert await loop.poll_once(ctx) == POLL
    assert ctx.index.get_state(STATE_LAST_POLL_OUTCOME) == "watchdog"
    release.set()
    for _ in range(200):
        if fields(caplog, "poll_thread_error"):
            break
        await asyncio.sleep(0.01)

    assert fields(caplog, "poll_thread_error") == [{"acct": linked.acct, "reason": "RuntimeError"}]
    assert "synthetic detail" not in caplog.text


async def test_an_abandoned_poll_that_commits_still_gets_its_pages(
    linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked.worker.settings = linked.settings.model_copy(update={"poll_timeout_seconds": 0.1})
    release = threading.Event()
    real = AccountLoop._sync
    factory = linked.worker.client_factory
    assert factory is not None

    def slow(self: AccountLoop, ctx: AccountContext, client: CloudClient) -> SyncReport | None:
        # Stuck in local work rather than on the (now closed) client, then finishes.
        release.wait(10)
        fresh = factory(DEVICE_TOKEN)
        try:
            return real(self, ctx, fresh)
        finally:
            fresh.close()

    monkeypatch.setattr(AccountLoop, "_sync", slow)
    loop = linked.loop()
    ctx = linked.context()
    assert await loop.poll_once(ctx) == POLL
    assert priorities(linked.queue) == {}
    release.set()
    for _ in range(500):
        if set(priorities(linked.queue)) == {NOTEBOOK, PDF}:
            break
        await asyncio.sleep(0.01)

    assert set(priorities(linked.queue)) == {NOTEBOOK, PDF}
