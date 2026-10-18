"""The sync worker (scriptorium/worker.py): schedule, pages, renders, failure classes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

import pytest

from scriptorium.accounts import (
    STATE_LAST_POLL_OUTCOME,
    STATE_LAST_SYNC_AT,
    STATE_NEEDS_RELINK,
    STATE_TOKEN_REJECTIONS,
)
from scriptorium.cloud.client import CloudClient
from scriptorium.cloud.sync import STATE_ANOMALY, SyncReport
from scriptorium.jobs.kinds import RenderSpec
from scriptorium.library import pages as pg
from scriptorium.library.render import render_pdf_page, render_rm_page
from scriptorium.users.store import global_db_path, worker_heartbeat_path
from scriptorium.worker import (
    AccountContext,
    AccountLoop,
    Worker,
    backoff_delay,
    heartbeat_age,
    jittered,
    read_heartbeat,
)
from tests.cloud.fake_cloud import NOTEBOOK, PAGES, PDF, TOKEN_KEY, fixture_documents, renamed
from tests.cloud.scenarios import PAGE_101
from tests.worker.helpers import FAST, T0, Linked, link

POLL = 600


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pngs(linked: Linked) -> dict[str, bytes]:
    """``pages/<page>/<file>`` → bytes for every PNG on disk."""
    root = linked.paths.pages
    if not root.is_dir():
        return {}
    return {
        p.relative_to(linked.paths.root).as_posix(): p.read_bytes()
        for p in sorted(root.glob("*/*.png"))
    }


def report(outcome: str, **kw: Any) -> SyncReport:
    return SyncReport(outcome=outcome, started_at=T0, finished_at=T0, **kw)  # type: ignore[arg-type]


@pytest.fixture
def linked(tmp_path: Path) -> Any:
    linked = link(tmp_path)
    yield linked
    linked.close()


# ---- the schedule ------------------------------------------------------------------------------


def test_jitter_stays_within_ten_percent() -> None:
    assert jittered(600, 0) == 600
    assert jittered(600, 1) == pytest.approx(660)
    assert jittered(600, -1) == pytest.approx(540)
    assert jittered(600, 7) == pytest.approx(660)  # u is clamped
    assert jittered(5, 1, spread=0.5) == pytest.approx(7.5)


def test_backoff_doubles_to_an_hour_and_honours_retry_after() -> None:
    assert [backoff_delay(POLL, n, None) for n in (1, 2, 3, 4, 5, 50)] == [
        600,
        1200,
        2400,
        3600,
        3600,
        3600,
    ]
    assert backoff_delay(POLL, 1, 900) == 900
    assert backoff_delay(POLL, 1, 10) == 600
    assert backoff_delay(POLL, 1, 10**9) == 3600  # a hostile Retry-After is capped
    assert backoff_delay(7200, 3, None) == 7200  # a long POLL_SECONDS is never shortened


def test_after_poll_schedules_each_outcome(linked: Linked) -> None:
    loop = linked.loop()
    ctx = linked.context()
    assert loop.after_poll(ctx, report("ok")) == POLL
    assert ctx.index.get_state(STATE_LAST_SYNC_AT) is not None
    # A poll that stopped on its budget has more to do: soon, not POLL_SECONDS.
    for reason in ("byte_budget", "time_budget"):
        assert loop.after_poll(ctx, report("partial", reason=reason)) == FAST.continue_s
    assert loop.after_poll(ctx, report("partial", reason="listing_incomplete")) == POLL
    # Backoff grows with each consecutive backoff poll and resets after a success.
    assert [loop.after_poll(ctx, report("backoff", reason="http_503")) for _ in range(3)] == [
        600,
        1200,
        2400,
    ]
    assert loop.after_poll(ctx, report("backoff", retry_after_s=3000.0)) == 3600
    assert loop.after_poll(ctx, report("ok")) == POLL
    assert loop.after_poll(ctx, report("backoff")) == 600
    # An anomaly keeps the normal interval: SyncOnce itself makes no request while it is set.
    assert loop.after_poll(ctx, report("anomaly", anomaly="mass_delete")) == POLL
    assert ctx.index.get_state(STATE_LAST_POLL_OUTCOME) == "anomaly"


def test_three_consecutive_rejections_need_a_relink(linked: Linked) -> None:
    loop = linked.loop()
    ctx = linked.context()
    rejected = report("token_rejected", reason="token_rejected")
    assert loop.after_poll(ctx, rejected) == POLL
    assert loop.after_poll(ctx, report("backoff")) == 600  # an outage breaks the run
    assert ctx.index.get_state(STATE_TOKEN_REJECTIONS) is None
    loop.after_poll(ctx, rejected)
    loop.after_poll(ctx, rejected)
    assert ctx.index.get_state(STATE_NEEDS_RELINK) is None
    assert loop.after_poll(ctx, rejected) == FAST.recheck_s
    assert ctx.index.get_state(STATE_NEEDS_RELINK) == "true"


# ---- polling against the fake cloud ---------------------------------------------------------


async def test_first_poll_archives_and_renders_every_page(linked: Linked) -> None:
    loop = linked.loop()
    ctx = linked.context()
    assert await loop.poll_once(ctx) == POLL
    docs = fixture_documents()
    index = ctx.index
    pages = index.pages(NOTEBOOK)
    assert [p.page_id for p in pages] == list(PAGES)
    assert [p.content_hash for p in pages] == [
        sha(docs[NOTEBOOK][f"{NOTEBOOK}/{page}.rm"]) for page in PAGES
    ]
    assert all(p.rendered_at is None for p in pages)
    # The PDF's only page reuses the notebook's first page UUID (open question 8): it is left
    # out of `pages` while the notebook is live, but it is still rendered.
    assert index.pages(PDF) == []
    assert await linked.worker.drain_jobs() == 8
    assert all(p.rendered_at is not None for p in index.pages(NOTEBOOK))

    files = pngs(linked)
    assert len(files) == 8
    for page in index.pages(NOTEBOOK):
        assert page.png_path == pg.png_relpath(page.page_id, page.content_hash)
        rm = docs[NOTEBOOK][f"{NOTEBOOK}/{page.page_id}.rm"]
        assert files[page.png_path] == render_rm_page(rm).png
    pdf_source = pg.page_sources(linked.paths, pg.current_version(linked.paths, index, PDF))[0]  # type: ignore[arg-type]
    pdf_png = files[pg.png_relpath(PAGES[0], pdf_source.content_hash)]
    assert pg.png_size(pdf_png) == pg.png_size(render_pdf_page(docs[PDF][f"{PDF}.pdf"], 0).png)
    # A second poll of an unchanged cloud queues nothing new.
    assert await loop.poll_once(ctx) == POLL
    assert await linked.worker.drain_jobs() == 0


async def test_an_edited_page_gets_a_new_png_and_keeps_the_old(linked: Linked) -> None:
    """Acceptance #3: new PNG and history row within one poll; the old blob stays."""
    loop = await linked.sync()
    ctx = linked.context()
    old = ctx.index.page(NOTEBOOK, 0)
    assert old is not None and old.png_path is not None
    old_png = pngs(linked)[old.png_path]
    new_rm = fixture_documents()[NOTEBOOK][f"{NOTEBOOK}/{PAGES[3]}.rm"]
    linked.cloud.set_file(NOTEBOOK, PAGE_101, new_rm)

    assert await loop.poll_once(ctx) == POLL
    assert len(ctx.index.history(NOTEBOOK)) == 2
    changed = ctx.index.page(NOTEBOOK, 0)
    assert changed is not None and changed.content_hash == sha(new_rm)
    assert changed.rendered_at is None and changed.png_path == old.png_path  # last good image
    assert await linked.worker.drain_jobs() == 1
    changed = ctx.index.page(NOTEBOOK, 0)
    assert changed is not None and changed.png_path == pg.png_relpath(PAGES[0], sha(new_rm))
    files = pngs(linked)
    assert files[changed.png_path] == render_rm_page(new_rm).png
    assert files[old.png_path] == old_png  # the old version's image is still there
    assert (linked.paths.blobs / old.content_hash).is_file()  # and so is its blob
    # Other pages did not change, so they were not re-rendered.
    assert [p.rendered_at is not None for p in ctx.index.pages(NOTEBOOK)[1:]] == [True] * 6


async def test_a_copied_document_needs_no_render_jobs(linked: Linked) -> None:
    """Renders are content-addressed: a copy's pages already have their PNGs."""
    loop = await linked.sync()
    copy = "00000000-0000-4000-8000-0000000000c1"
    files = renamed(fixture_documents()[NOTEBOOK], NOTEBOOK, copy)
    metadata = json.loads(files[f"{copy}.metadata"])
    metadata["visibleName"] = "Copy"
    files[f"{copy}.metadata"] = json.dumps(metadata).encode()
    linked.cloud.put_document(copy, files)
    before = pngs(linked)
    assert await loop.poll_once(linked.context()) == POLL
    assert linked.context().index.document(copy) is not None
    assert await linked.worker.drain_jobs() == 0
    assert pngs(linked) == before


async def test_token_rejected_three_times_then_polling_stops(linked: Linked) -> None:
    """Acceptance #8, second half."""
    linked.cloud.device_token = "a-new-device-token"  # the stored one is no longer accepted
    loop = linked.loop()
    ctx = linked.context()
    assert await loop.poll_once(ctx) == POLL
    assert await loop.poll_once(ctx) == POLL
    assert ctx.index.get_state(STATE_NEEDS_RELINK) is None
    assert await loop.poll_once(ctx) == FAST.recheck_s
    assert ctx.index.get_state(STATE_NEEDS_RELINK) == "true"
    seen = len(linked.cloud.requests)
    assert await loop.poll_once(ctx) == FAST.recheck_s
    assert len(linked.cloud.requests) == seen  # no request at all while it needs a relink
    # connect_remarkable clears the state; polling resumes.
    linked.cloud.device_token = "test-device-token"
    ctx.index.set_state(STATE_NEEDS_RELINK, None)
    ctx.index.set_state(STATE_TOKEN_REJECTIONS, None)
    assert await loop.poll_once(ctx) == POLL
    assert ctx.index.document(NOTEBOOK) is not None


async def test_a_token_endpoint_outage_backs_off_without_relink(linked: Linked) -> None:
    """Acceptance #8, first half."""
    linked.cloud.status[TOKEN_KEY] = 503
    loop = linked.loop()
    ctx = linked.context()
    delays = [await loop.poll_once(ctx) for _ in range(4)]
    assert delays == [600, 1200, 2400, 3600]
    assert ctx.index.get_state(STATE_NEEDS_RELINK) is None
    assert ctx.index.get_state(STATE_LAST_POLL_OUTCOME) == "backoff"
    del linked.cloud.status[TOKEN_KEY]
    assert await loop.poll_once(ctx) == POLL
    assert loop.backoffs == 0


async def test_an_anomaly_stops_cloud_requests(linked: Linked) -> None:
    loop = await linked.sync()
    ctx = linked.context()
    ctx.index.set_state(STATE_ANOMALY, "mass_delete")
    seen = len(linked.cloud.requests)
    assert await loop.poll_once(ctx) == POLL
    assert len(linked.cloud.requests) == seen


async def test_a_budget_stop_polls_again_soon(tmp_path: Path) -> None:
    linked = link(tmp_path)
    ticks = iter(range(0, 10**9, 10_000))
    linked.worker.monotonic = lambda: float(next(ticks))  # every reading spends the budget
    try:
        loop = linked.loop()
        assert await loop.poll_once(linked.context()) == FAST.continue_s
        assert linked.context().index.get_state(STATE_LAST_POLL_OUTCOME) == "partial"
    finally:
        linked.close()


async def test_the_watchdog_abandons_a_stuck_poll_and_never_overlaps(
    linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked.worker.settings = linked.settings.model_copy(update={"poll_timeout_seconds": 0.1})
    release = threading.Event()
    calls: list[int] = []

    def stuck(self: AccountLoop, ctx: AccountContext, client: CloudClient) -> SyncReport:
        calls.append(1)
        release.wait(10)
        return report("ok")

    monkeypatch.setattr(AccountLoop, "_sync", stuck)
    loop = linked.loop()
    ctx = linked.context()
    assert await loop.poll_once(ctx) == 600  # treated like a backoff
    assert ctx.index.get_state(STATE_LAST_POLL_OUTCOME) == "watchdog"
    assert await loop.poll_once(ctx) == FAST.recheck_s  # the old poll still runs: wait
    assert calls == [1]
    release.set()
    for _ in range(100):
        if loop._inflight is None or loop._inflight.done():
            break
        await asyncio.sleep(0.01)
    assert await loop.poll_once(ctx) == POLL
    assert calls == [1, 1]


async def test_start_sweeps_staging(linked: Linked) -> None:
    """Acceptance #7: whatever an interrupted download left in staging/ is gone at start."""
    stale = linked.paths.staging / f"{NOTEBOOK}.01J0000000000000000000000B"
    stale.mkdir(parents=True)
    (stale / "partial").write_bytes(b"half a blob")
    linked.loop()
    assert not stale.exists()


# ---- jobs ---------------------------------------------------------------------------------------


async def test_a_page_that_will_not_render_fails_its_job(tmp_path: Path) -> None:
    linked = link(tmp_path)
    try:
        linked.cloud.set_file(NOTEBOOK, PAGE_101, b"not a page")
        loop = linked.loop()
        await loop.poll_once(linked.context())
        assert await linked.worker.drain_jobs() == 8
        page = linked.index().page(NOTEBOOK, 0)
        assert page is not None and page.rendered_at is None
        failed = [
            j
            for j in (linked.queue.get(n, linked.uid) for n in range(1, 20))
            if j is not None and j.state == "failed"
        ]
        assert [(j.payload["page"], j.error) for j in failed] == [(PAGES[0], "invalid_argument")]
    finally:
        linked.close()


async def test_a_broken_render_pool_is_rebuilt_and_the_next_job_renders(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A child that exits abruptly breaks a ProcessPoolExecutor for good; the worker heals."""
    caplog.set_level(logging.INFO, logger="scriptorium")
    linked = link(tmp_path, settings={"render_workers": 1})
    try:
        injected = linked.worker.render_executor
        assert injected is not None
        injected.shutdown()
        linked.worker.render_executor = None  # the worker builds its own spawn pool
        pool = linked.worker.executor()
        assert isinstance(pool, ProcessPoolExecutor)
        killed = pool.submit(os._exit, 1)  # not via render_in_pool: a test hook only
        with pytest.raises(BrokenProcessPool):
            killed.result(timeout=60)
        await linked.loop().poll_once(linked.context())

        job = await asyncio.to_thread(linked.queue.lease, linked.worker.owner, kinds=["render"])
        assert job is not None
        await linked.worker.run_job(job)
        failed = linked.queue.get(job.id, linked.uid)
        assert failed is not None and (failed.state, failed.error) == ("failed", "internal")
        assert linked.worker.render_pool_rebuilds == 1
        assert linked.worker.render_executor is None  # dropped; the next job builds a new one
        assert any(r.getMessage() == "render_pool_rebuilt" for r in caplog.records)

        nxt = await asyncio.to_thread(linked.queue.lease, linked.worker.owner, kinds=["render"])
        assert nxt is not None
        await linked.worker.run_job(nxt)
        done = linked.queue.get(nxt.id, linked.uid)
        assert done is not None and done.state == "done", done
        assert linked.worker.executor() is not pool
        assert linked.worker.render_pool_rebuilds == 1

        linked.worker.write_heartbeat()
        beat = read_heartbeat(worker_heartbeat_path(linked.settings.data_dir))
        assert beat["render_pool_rebuilds"] == 1
    finally:
        linked.close()


async def test_page_image_jobs_render_larger_than_the_archive(linked: Linked) -> None:
    await linked.sync()
    page = linked.index().page(NOTEBOOK, 0)
    assert page is not None
    version = pg.current_version(linked.paths, linked.index(), NOTEBOOK)
    assert version is not None
    spec = RenderSpec(NOTEBOOK, page.page_id, page.content_hash, version.generation, 2048)
    job = linked.queue.enqueue(
        uid=linked.uid,
        acct=linked.acct,
        kind=spec.kind,
        dedupe_key=spec.key(linked.acct),
        payload=spec.payload(),
    )
    assert await linked.worker.drain_jobs() == 1
    done = linked.queue.get(job.id, linked.uid)
    assert done is not None and done.state == "done"
    rel = pg.png_relpath(page.page_id, page.content_hash, max_side=2048)
    assert done.result == {"png": rel, "bytes": len(pngs(linked)[rel])}
    assert max(pg.png_size(pngs(linked)[rel])) == 2048


async def test_a_job_whose_lease_was_lost_is_not_reported(linked: Linked) -> None:
    await linked.sync()
    page = linked.index().page(NOTEBOOK, 2)
    assert page is not None
    spec = RenderSpec(NOTEBOOK, page.page_id, page.content_hash, 1, 512)
    linked.queue.enqueue(
        uid=linked.uid,
        acct=linked.acct,
        kind=spec.kind,
        dedupe_key=spec.key(linked.acct),
        payload=spec.payload(),
    )
    job = linked.queue.lease("someone-else", kinds=["page_image"])
    assert job is not None
    await linked.worker.run_job(job)  # the worker does not hold this lease
    after = linked.queue.get(job.id, linked.uid)
    assert after is not None and (after.state, after.lease_owner) == ("leased", "someone-else")


async def test_jobs_for_an_unlinked_account_are_dropped(linked: Linked) -> None:
    spec = RenderSpec(NOTEBOOK, PAGES[0], "a" * 64, 1)
    job = linked.queue.enqueue(
        uid=linked.uid,
        acct="01J0000000000000000000000Z",
        kind=spec.kind,
        dedupe_key="render:other",
        payload=spec.payload(),
    )
    assert await linked.worker.drain_jobs() == 1
    done = linked.queue.get(job.id, linked.uid)
    assert done is not None and (done.state, done.result) == ("done", {"state": "gone"})


# ---- the process ------------------------------------------------------------------------------


async def test_run_syncs_renders_beats_and_stops(linked: Linked) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(linked.worker.run(stop))
    try:
        for _ in range(500):
            if len(pngs(linked)) == 8:
                break
            await asyncio.sleep(0.02)
        assert len(pngs(linked)) == 8
        age = heartbeat_age(worker_heartbeat_path(linked.settings.data_dir))
        assert age is not None and age < 5
        assert set(linked.worker._loops) == {linked.acct}
        # Deactivating the account stops its loop at the next supervisor pass.
        linked.store.deactivate_account(linked.uid, linked.acct)
        for _ in range(200):
            if not linked.worker._loops:
                break
            await asyncio.sleep(0.02)
        assert linked.worker._loops == {}
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def test_the_worker_waits_for_the_server_to_migrate(tmp_path: Path) -> None:
    linked = link(tmp_path)
    linked.close()
    stop = asyncio.Event()
    empty = linked.settings.model_copy(update={"data_dir": tmp_path / "elsewhere"})
    (tmp_path / "elsewhere").mkdir()
    waiting = asyncio.create_task(Worker.open(empty, stop, timing=FAST))
    await asyncio.sleep(0.1)
    assert not waiting.done()
    stop.set()
    assert await waiting is None
    assert not global_db_path(tmp_path / "elsewhere").exists()  # it never creates the file
    opened = await Worker.open(linked.settings, asyncio.Event(), timing=FAST)
    assert opened is not None
    opened.close()


def test_heartbeat_age(tmp_path: Path) -> None:
    path = tmp_path / "worker.heartbeat"
    assert heartbeat_age(path) is None
    path.write_text("x")
    age = heartbeat_age(path)
    assert age is not None and age < 5
    assert heartbeat_age(path, now=path.stat().st_mtime + 100) == pytest.approx(100)


def test_read_heartbeat_tolerates_any_body(tmp_path: Path) -> None:
    path = tmp_path / "worker.heartbeat"
    assert read_heartbeat(path) == {}
    for body in ("2026-10-01T12:00:00.000000Z\n", "[1]", "x" * 5000):
        path.write_text(body)
        assert read_heartbeat(path) == {}
    path.write_text('{"at":"t","render_pool_rebuilds":2}\n')
    assert read_heartbeat(path) == {"at": "t", "render_pool_rebuilds": 2}
