"""The sync worker, ``scriptorium worker`` (Architecture §4.1, §7, §8.3).

One process, one asyncio loop, four kinds of task:

- **Supervisor.** Every :attr:`Timing.supervise_s` it reads the active accounts
  (``GlobalStore.active_accounts``: active, user not disabled) and starts one
  :class:`AccountLoop` per account, stopping the loops of accounts that left the list.
- **Account loop.** At start: ``Archive.sweep_staging`` (finish or discard interrupted
  commits, §7 step 3) and a reconciliation of every live document's pages. Then ``SyncOnce``
  on the §7 schedule: ``POLL_SECONDS`` ±10 %; soon again (a few seconds, jittered) after a
  poll that stopped on its byte or time budget; exponential backoff to an hour after
  ``backoff``; ``needs_relink`` and no more polling after :data:`~scriptorium.accounts.RELINK_AFTER`
  consecutive ``token_rejected`` polls (``connect_remarkable`` clears it); while an anomaly
  is set ``SyncOnce`` itself makes no request (read-only safe mode), so the loop just keeps
  its normal interval. At most ``SYNC_CONCURRENCY`` polls run at once. Each poll runs in a
  thread under a watchdog (``POLL_TIMEOUT_SECONDS``): when it fires, the cloud client is
  closed (failing whatever request is in flight) and no new poll of that account starts
  until the old one has returned. After a poll, the pages of every committed or
  un-tombstoned document are refreshed from its new version (:func:`refresh_document`) and
  ``render`` jobs are queued for pages without a PNG.
- **Job runner.** Leases ``render`` and ``page_image`` jobs, at most ``RENDER_WORKERS`` at a
  time, renders in the process pool (heartbeating the lease while it waits), writes the PNG
  and completes or fails the job. A job whose lease was lost is abandoned, not reported. A
  ``ProcessPoolExecutor`` is broken for good once a child exits abruptly, so a job that
  finds the pool broken (at submit or while waiting) shuts it down, drops it so the next
  job gets a fresh one (``render_pool_rebuilt``), and fails normally to be retried.
- **Housekeeping.** ``requeue_expired`` at start and every minute; at start, dead render
  jobs that died of their run rather than their page are revived once
  (``JobQueue.revive_dead_renders``); the liveness file
  ``<data>/worker.heartbeat`` (read by ``scriptorium healthcheck --worker``, which looks
  only at its mtime; its body is a small JSON object with the time and the number of render
  pool rebuilds since start, which ``account_status`` reports).

The worker never migrates: it opens ``scriptorium.sqlite`` and each ``index.sqlite`` with
``migrate=False`` and waits while the server has not migrated them yet.

The worker is the only cloud writer (CLAUDE.md), and in M1 it writes nothing: ``SyncOnce``
only reads. Nothing here deletes under ``blobs/``, ``library/`` or ``tombstones/``; the PNG
cache under ``pages/`` is only ever added to.

Logs carry the account id, job ids, counts, byte counts and outcome codes; never a name, a
token, a page's content or an exception message.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import BrokenExecutor, Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from scriptorium.accounts import (
    RELINK_AFTER,
    STATE_LAST_POLL_AT,
    STATE_LAST_POLL_OUTCOME,
    STATE_LAST_SYNC_AT,
    STATE_NEEDS_RELINK,
    STATE_TOKEN_REJECTIONS,
    device_token_name,
)
from scriptorium.cloud.client import CloudClient
from scriptorium.cloud.sync import SyncOnce, SyncReport, SyncSettings
from scriptorium.config import Settings
from scriptorium.db.connect import to_db_time, utc_now
from scriptorium.db.migrations import SchemaMismatch
from scriptorium.errors import ScriptoriumError
from scriptorium.jobs.kinds import RENDER_KINDS, RenderSpec, parse_render_payload
from scriptorium.jobs.queue import JobQueue, JobRow, LeaseLostError
from scriptorium.library import pages as pg
from scriptorium.library import render
from scriptorium.library.archive import Archive
from scriptorium.library.index import Index, PageRow
from scriptorium.logging import log_event
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import (
    AccountPaths,
    AccountRow,
    GlobalStore,
    account_paths,
    global_db_path,
    worker_heartbeat_path,
)

__all__ = [
    "BUDGET_REASONS",
    "AccountLoop",
    "Timing",
    "Worker",
    "backoff_delay",
    "heartbeat_age",
    "jittered",
    "read_heartbeat",
    "refresh_document",
]

logger = logging.getLogger(__name__)

JITTER: Final = 0.10
"""The poll interval's jitter (±10 %, §7)."""
BACKOFF_CAP_SECONDS: Final = 3600
BUDGET_REASONS: Final = frozenset({"byte_budget", "time_budget"})
"""A ``partial`` poll with one of these reasons stopped early with more to do."""
HEARTBEAT_STALE_SECONDS: Final = 180
HEARTBEAT_MAX_BYTES: Final = 4096
DOCUMENT_TYPE: Final = "DocumentType"


@dataclass(frozen=True)
class Timing:
    """The worker's housekeeping intervals, in seconds; tests shrink them."""

    supervise_s: float = 30.0
    requeue_s: float = 60.0
    heartbeat_s: float = 30.0
    job_idle_s: float = 1.0
    job_heartbeat_s: float = 20.0
    lease_ttl_s: int = 60
    recheck_s: float = 60.0
    """How often an account that cannot poll (needs relink, no token) is looked at again."""
    schema_wait_s: float = 5.0
    continue_s: float = 5.0
    """Delay before the next poll after one that stopped on its budget (jittered ±50 %)."""
    watchdog_grace_s: float = 60.0


# ---- the schedule (pure) ---------------------------------------------------------------------


def jittered(seconds: float, u: float, spread: float = JITTER) -> float:
    """``seconds`` moved by ``spread * u`` of itself; ``u`` is uniform in [-1, 1]."""
    return max(0.0, seconds * (1.0 + spread * max(-1.0, min(1.0, u))))


def backoff_delay(poll_seconds: int, failures: int, retry_after: float | None) -> float:
    """The wait after ``failures`` consecutive ``backoff`` polls (§7 step 7).

    Doubles from ``POLL_SECONDS`` up to an hour (or ``POLL_SECONDS`` if that is longer), and
    honours the cloud's ``Retry-After`` up to the same cap.
    """
    cap = float(max(BACKOFF_CAP_SECONDS, poll_seconds))
    delay = min(float(poll_seconds) * 2.0 ** min(max(failures, 1) - 1, 16), cap)
    if retry_after is not None and retry_after > 0:
        delay = max(delay, min(float(retry_after), cap))
    return delay


def heartbeat_age(path: Path, now: float | None = None) -> float | None:
    """Seconds since the worker last wrote its heartbeat file, or None if there is none."""
    try:
        written = path.stat().st_mtime
    except OSError:
        return None
    return max(0.0, (time.time() if now is None else now) - written)


def read_heartbeat(path: Path) -> dict[str, Any]:
    """The heartbeat file's body (``{"at": ..., "render_pool_rebuilds": n}``), or ``{}``.

    Anything unreadable, oversized or not a JSON object reads as ``{}``: the body is
    informational, the file's mtime is the liveness signal.
    """
    try:
        with path.open("rb") as fh:
            raw = fh.read(HEARTBEAT_MAX_BYTES + 1)
    except OSError:
        return {}
    if len(raw) > HEARTBEAT_MAX_BYTES:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


# ---- pages ---------------------------------------------------------------------------------


def _live(index: Index, uuid: str) -> bool:
    doc = index.document(uuid)
    return doc is not None and doc.deleted_at is None


def refresh_document(
    paths: AccountPaths, index: Index, uuid: str, now: datetime
) -> tuple[list[RenderSpec], int]:
    """Make ``pages`` match the document's current version; returns what needs rendering.

    Returns the render specs of pages with no PNG on disk, and how many page ids were left
    out of ``pages`` because another live document already lists them (open question 8:
    ``pages.uuid`` is a primary key, and a duplicated document can reuse page UUIDs). A page
    left out still gets its PNG and is still readable: tools read page lists from the
    version's blobs, not from ``pages``. A page whose PNG is already on disk (an unchanged
    page of a copied document, or a render that finished before a restart) is marked
    rendered without a job.
    """
    doc = index.document(uuid)
    if doc is None or doc.type != DOCUMENT_TYPE or doc.hash_root is None:
        return [], 0
    version = pg.current_version(paths, index, uuid)
    if version is None:
        return [], 0
    sources = pg.page_sources(paths, version)
    rows: list[PageRow] = []
    collisions = 0
    for source in sources:
        owner = index.page_by_id(source.page_id)
        if owner is not None and owner.document != uuid and _live(index, owner.document):
            collisions += 1
            continue
        rows.append(PageRow(source.page_id, uuid, source.idx, source.content_hash, now))
    index.replace_pages(uuid, rows)
    listed = {row.page_id: row for row in index.pages(uuid)}
    todo: list[RenderSpec] = []
    for source in sources:
        rel = pg.png_relpath(source.page_id, source.content_hash)
        row = listed.get(source.page_id)
        if pg.resolve_png(paths, rel).is_file():
            if row is not None and row.rendered_at is None:
                index.set_rendered(source.page_id, rel, now, content_hash=source.content_hash)
            continue
        todo.append(RenderSpec(uuid, source.page_id, source.content_hash, version.generation))
    return todo, collisions


def find_source(paths: AccountPaths, index: Index, spec: RenderSpec) -> pg.PageSource | None:
    """The page version a render job names, read from that generation's blobs."""
    try:
        version = pg.version_at(paths, index, spec.document, spec.generation)
    except ScriptoriumError:
        return None
    for source in pg.page_sources(paths, version):
        if source.page_id == spec.page and source.content_hash == spec.hash:
            return source
    return None


# ---- per-account state ---------------------------------------------------------------------


@dataclass
class AccountContext:
    """An account's paths and its open index (worker side: never migrated here)."""

    uid: str
    acct: str
    paths: AccountPaths
    index: Index


ClientFactory = Callable[[str], CloudClient]


def _default_client_factory(settings: Settings) -> ClientFactory:
    cache = Path(tempfile.gettempdir()) / "scriptorium-cloud"

    def make(token: str) -> CloudClient:
        return CloudClient.from_device_token(token, cache_dir=cache)

    return make


@dataclass
class Worker:
    """The worker process's state; :meth:`run` until ``stop`` is set."""

    settings: Settings
    store: GlobalStore
    queue: JobQueue
    client_factory: ClientFactory | None = None
    render_executor: Executor | None = None
    clock: Callable[[], datetime] = utc_now
    monotonic: Callable[[], float] = time.monotonic
    timing: Timing = field(default_factory=Timing)
    rng: random.Random = field(default_factory=random.SystemRandom)
    owner: str = field(default_factory=lambda: f"worker-{os.getpid()}-{secrets.token_hex(4)}")

    def __post_init__(self) -> None:
        if self.client_factory is None:
            self.client_factory = _default_client_factory(self.settings)
        self._owns_executor = self.render_executor is None
        self._executor_lock = threading.Lock()
        self.render_pool_rebuilds = 0
        """Render pools replaced since start because the last one broke (heartbeat body)."""
        self._contexts: dict[tuple[str, str], AccountContext] = {}
        self._contexts_lock = threading.Lock()
        self._loops: dict[str, tuple[AccountLoop, asyncio.Task[None], asyncio.Event]] = {}
        self._poll_executor = ThreadPoolExecutor(
            max_workers=self.settings.sync_concurrency + 2, thread_name_prefix="poll"
        )
        self._slots: asyncio.Semaphore | None = None
        self.sync_settings = SyncSettings(
            tombstone_max_per_poll=self.settings.tombstone_max_per_poll,
            sync_workers=self.settings.sync_workers,
            sync_max_poll_bytes=self.settings.sync_max_poll_bytes,
            sync_max_poll_seconds=self.settings.sync_max_poll_seconds,
        )

    # ---- start-up and shutdown ----------------------------------------------------------

    @classmethod
    async def open(cls, settings: Settings, stop: asyncio.Event, **kwargs: Any) -> Worker | None:
        """Open the global stores once the server has migrated them; None if stopped first."""
        path = global_db_path(settings.data_dir)
        waiting = False
        while not stop.is_set():
            try:
                store = await asyncio.to_thread(GlobalStore.open, path, migrate=False)
                queue = await asyncio.to_thread(JobQueue.open, path, migrate=False)
            except SchemaMismatch:
                if not waiting:
                    log_event(logger, "worker_waiting_for_schema")
                    waiting = True
                await _wait(stop, kwargs.get("timing", Timing()).schema_wait_s)
                continue
            return cls(settings, store, queue, **kwargs)
        return None

    def close(self) -> None:
        self._poll_executor.shutdown(wait=False, cancel_futures=True)
        if self._owns_executor and self.render_executor is not None:
            self.render_executor.shutdown(wait=False, cancel_futures=True)
        with self._contexts_lock:
            for ctx in self._contexts.values():
                ctx.index.close()
            self._contexts.clear()
        self.queue.close()

    def sync_slots(self) -> asyncio.Semaphore:
        """The ``SYNC_CONCURRENCY`` semaphore every poll holds while it runs."""
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.settings.sync_concurrency)
        return self._slots

    def executor(self) -> Executor:
        """The render pool, built on first use and again after :meth:`drop_executor`."""
        with self._executor_lock:
            if self.render_executor is None:
                self.render_executor = render.render_pool(
                    self.settings.render_workers, log_level=self.settings.log_level
                )
                self._owns_executor = True
            return self.render_executor

    def drop_executor(self, broken: Executor) -> bool:
        """Shut down ``broken`` and forget it, so :meth:`executor` builds a fresh pool.

        Several jobs can find the same pool broken; only the first replaces it (returns
        True), the others see that ``broken`` is no longer the current pool.
        """
        with self._executor_lock:
            if self.render_executor is not broken:
                return False
            self.render_executor = None
            self.render_pool_rebuilds += 1
            count = self.render_pool_rebuilds
        broken.shutdown(wait=False, cancel_futures=True)
        log_event(logger, "render_pool_rebuilt", level=logging.WARNING, count=count)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        """Run every task until ``stop`` is set, then wind down."""
        await asyncio.to_thread(self.requeue_expired)
        revived = await asyncio.to_thread(self.queue.revive_dead_renders)
        log_event(logger, "worker_start", count=revived)
        tasks = [
            asyncio.create_task(self._housekeeping(stop), name="housekeeping"),
            asyncio.create_task(self._supervise(stop), name="supervisor"),
            asyncio.create_task(self.run_jobs(stop), name="jobs"),
        ]
        try:
            await stop.wait()
        finally:
            for loop, _task, account_stop in self._loops.values():
                account_stop.set()
                loop.abort()
            pending = [task for _l, task, _s in self._loops.values()] + tasks
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._loops.clear()
            log_event(logger, "worker_stop")

    # ---- housekeeping ------------------------------------------------------------------

    def requeue_expired(self) -> int:
        count = self.queue.requeue_expired()
        if count:
            log_event(logger, "jobs_requeued", count=count)
        return count

    def write_heartbeat(self) -> None:
        """Atomically replace ``<data>/worker.heartbeat`` (its mtime is the signal)."""
        path = worker_heartbeat_path(self.settings.data_dir)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        body = {"at": to_db_time(self.clock()), "render_pool_rebuilds": self.render_pool_rebuilds}
        tmp.write_text(json.dumps(body, separators=(",", ":")) + "\n", encoding="ascii")
        os.replace(tmp, path)

    async def _housekeeping(self, stop: asyncio.Event) -> None:
        last_requeue = self.monotonic()
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.write_heartbeat)
                if self.monotonic() - last_requeue >= self.timing.requeue_s:
                    last_requeue = self.monotonic()
                    await asyncio.to_thread(self.requeue_expired)
            except Exception:
                log_event(logger, "worker_housekeeping_failed", level=logging.ERROR, exc_info=True)
            await _wait(stop, min(self.timing.heartbeat_s, self.timing.requeue_s))

    # ---- accounts ------------------------------------------------------------------------

    async def _supervise(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                rows = await asyncio.to_thread(self.store.active_accounts)
                self.reconcile_loops(rows)
            except Exception:
                log_event(logger, "worker_supervise_failed", level=logging.ERROR, exc_info=True)
            await _wait(stop, self.timing.supervise_s)

    def reconcile_loops(self, rows: Sequence[AccountRow]) -> None:
        """Start a loop for each new active account; stop those no longer listed."""
        wanted = {row.acct: row for row in rows}
        for acct in list(self._loops):
            if acct not in wanted:
                _loop, _task, account_stop = self._loops.pop(acct)
                account_stop.set()
                log_event(logger, "account_loop_stop", acct=acct)
        for acct, row in wanted.items():
            if acct in self._loops and not self._loops[acct][1].done():
                continue
            loop = AccountLoop(self, row)
            account_stop = asyncio.Event()
            task = asyncio.create_task(loop.run(account_stop), name=f"account-{acct}")
            self._loops[acct] = (loop, task, account_stop)
            log_event(logger, "account_loop_start", acct=acct)

    def context(self, uid: str, acct: str) -> AccountContext:
        """The account's paths and index; raises ``SchemaMismatch`` until it is migrated."""
        key = (uid, acct)
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            if ctx is None:
                paths = account_paths(self.settings.data_dir, uid, acct)
                index = Index.open(paths.index, migrate=False, clock=self.clock)
                ctx = AccountContext(uid, acct, paths, index)
                self._contexts[key] = ctx
            return ctx

    def device_token(self, uid: str, acct: str) -> str | None:
        value = SecretBox(self.settings, uid).open(device_token_name(acct))
        if value is None:
            return None
        try:
            return value.decode("ascii")
        except UnicodeDecodeError:
            return None

    def refresh(self, ctx: AccountContext, uuids: Iterable[str]) -> int:
        """Refresh the pages of ``uuids`` and queue their renders; returns jobs queued."""
        queued = collisions = 0
        now = self.clock()
        for uuid in uuids:
            try:
                todo, skipped = refresh_document(ctx.paths, ctx.index, uuid, now)
            except ScriptoriumError:
                log_event(logger, "pages_refresh_failed", level=logging.WARNING, acct=ctx.acct)
                continue
            collisions += skipped
            for spec in todo:
                self.queue.enqueue(
                    uid=ctx.uid,
                    acct=ctx.acct,
                    kind=spec.kind,
                    dedupe_key=spec.key(ctx.acct),
                    payload=spec.payload(),
                )
                queued += 1
        if collisions:
            log_event(
                logger, "pages_id_collision", level=logging.WARNING, acct=ctx.acct, count=collisions
            )
        return queued

    # ---- jobs ------------------------------------------------------------------------------

    async def run_jobs(self, stop: asyncio.Event) -> None:
        """Lease and run render jobs until ``stop``; at most ``RENDER_WORKERS`` at once."""
        slots = asyncio.Semaphore(self.settings.render_workers)
        running: set[asyncio.Task[None]] = set()
        try:
            while not stop.is_set():
                await slots.acquire()
                try:
                    job = await asyncio.to_thread(
                        self.queue.lease,
                        self.owner,
                        kinds=RENDER_KINDS,
                        ttl_s=self.timing.lease_ttl_s,
                    )
                except Exception:
                    slots.release()
                    log_event(logger, "job_lease_failed", level=logging.ERROR, exc_info=True)
                    await _wait(stop, self.timing.job_idle_s)
                    continue
                if job is None:
                    slots.release()
                    await _wait(stop, self.timing.job_idle_s)
                    continue
                task = asyncio.create_task(self.run_job(job))
                running.add(task)

                def done(t: asyncio.Task[None]) -> None:
                    running.discard(t)
                    slots.release()

                task.add_done_callback(done)
        finally:
            for task in list(running):
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)

    async def drain_jobs(self, *, limit: int = 1000) -> int:
        """Run leasable jobs one at a time until none is left (tests and tooling)."""
        ran = 0
        while ran < limit:
            job = await asyncio.to_thread(
                self.queue.lease, self.owner, kinds=RENDER_KINDS, ttl_s=self.timing.lease_ttl_s
            )
            if job is None:
                break
            await self.run_job(job)
            ran += 1
        return ran

    async def run_job(self, job: JobRow) -> None:
        """Run one leased job and report it; never raises (except cancellation)."""
        started = self.monotonic()
        outcome = "done"
        try:
            result = await self._render_job(job)
            await asyncio.to_thread(self.queue.complete, job.id, self.owner, result)
        except LeaseLostError:
            outcome = "lease_lost"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, ScriptoriumError) else "internal"
            outcome = code
            level = logging.WARNING if isinstance(exc, ScriptoriumError) else logging.ERROR
            log_event(
                logger,
                "job_failed",
                level=level,
                exc_info=not isinstance(exc, ScriptoriumError),
                job=job.id,
                acct=job.acct,
                reason=code,
            )
            with contextlib.suppress(LeaseLostError):
                await asyncio.to_thread(self.queue.fail, job.id, self.owner, code)
        log_event(
            logger,
            "job",
            job=job.id,
            acct=job.acct,
            outcome=outcome,
            duration_ms=round((self.monotonic() - started) * 1000, 1),
        )

    async def _render_job(self, job: JobRow) -> Mapping[str, Any]:
        spec = parse_render_payload(job.kind, job.payload)
        if (
            job.acct is None
            or await asyncio.to_thread(self.store.account, job.uid, job.acct) is None
        ):
            return {"state": "gone"}
        ctx = await asyncio.to_thread(self.context, job.uid, job.acct)
        source = await asyncio.to_thread(find_source, ctx.paths, ctx.index, spec)
        if source is None:
            return {"state": "gone"}
        rel = pg.png_relpath(source.page_id, source.content_hash, max_side=spec.max_side)
        path = pg.resolve_png(ctx.paths, rel)
        if not await asyncio.to_thread(path.is_file):
            data = await self._render(ctx, source, spec.max_side, job)
            await asyncio.to_thread(pg.write_png, ctx.paths, rel, data)
        size = (await asyncio.to_thread(path.stat)).st_size
        if spec.max_side is None:
            await asyncio.to_thread(
                ctx.index.set_rendered,
                source.page_id,
                rel,
                self.clock(),
                content_hash=source.content_hash,
            )
        return {"png": rel, "bytes": size}

    async def _render(
        self, ctx: AccountContext, source: pg.PageSource, max_side: int | None, job: JobRow
    ) -> bytes:
        """The page's PNG: at the archive scale, or with longest side ``max_side``."""
        scale = 1.0
        if max_side is not None:
            base_rel = pg.png_relpath(source.page_id, source.content_hash)
            base_path = pg.resolve_png(ctx.paths, base_rel)
            if not await asyncio.to_thread(base_path.is_file):
                base = await self._render(ctx, source, None, job)
                await asyncio.to_thread(pg.write_png, ctx.paths, base_rel, base)
            head = await asyncio.to_thread(_read_head, base_path)
            longest = max(pg.png_size(head))
            scale = min(render.MAX_SCALE, max(render.MIN_SCALE, max_side / longest))
        rm, pdf_blob = await asyncio.to_thread(self._inputs, ctx.paths, source)
        single_pdf = None
        if pdf_blob is not None and source.pdf_page is not None:
            # The whole PDF is cloud-supplied: only a render child ever parses it (§8.3).
            single_pdf = await self._in_pool(
                job, render.extract_pdf_page_from_blob, str(pdf_blob), source.pdf_page
            )
        if rm is not None:
            result = await self._in_pool(
                job, render.render_rm_page, rm, scale=scale, pdf_page=single_pdf
            )
        elif single_pdf is not None:
            result = await self._in_pool(job, render.render_pdf_page, single_pdf, 0, scale=scale)
        else:
            png = await asyncio.to_thread(pg.blank_page_png, scale)
            width, height = pg.png_size(png)
            result = render.RenderResult(png, width, height, (0.0, 0.0, 0.0, 0.0))
        if max_side is None:
            return result.png
        return await self._in_pool(job, render.page_image_bytes, result, max_side_px=max_side)

    async def _in_pool[**P, R](
        self, job: JobRow, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        """Run ``fn`` in the render pool, heartbeating the job; heal a broken pool.

        A pool that is broken (a child exited abruptly) or refuses work is dropped so the
        next job gets a fresh one; the error propagates and this job fails and is retried.
        """
        pool = self.executor()
        try:
            fut = render.render_in_pool(pool, fn, *args, **kwargs)
        except RuntimeError:  # BrokenProcessPool, or a pool that was shut down
            self.drop_executor(pool)
            raise
        try:
            return await self._await_with_heartbeat(fut, job)
        except BrokenExecutor:  # BrokenProcessPool: a child died while this job waited
            self.drop_executor(pool)
            raise

    @staticmethod
    def _inputs(paths: AccountPaths, source: pg.PageSource) -> tuple[bytes | None, Path | None]:
        """The page's ``.rm`` bytes and, for a PDF page, the PDF blob's resolved path.

        The worker's own process never opens the PDF: a render child reads it from that path
        and cuts the one page out (:func:`~scriptorium.library.render.extract_pdf_page_from_blob`).
        """
        rm = None
        if source.rm_sha is not None:
            rm = pg.read_blob(paths, source.rm_sha, limit=render.MAX_RM_BYTES)
        pdf = None
        if source.pdf_sha is not None and source.pdf_page is not None:
            pdf = pg.blob_path(paths, source.pdf_sha)
        return rm, pdf

    async def _await_with_heartbeat[T](self, fut: Future[T], job: JobRow) -> T:
        """Await a pool future, extending the job's lease while it runs."""
        waiter = asyncio.wrap_future(fut)
        while True:
            done, _ = await asyncio.wait({waiter}, timeout=self.timing.job_heartbeat_s)
            if done:
                return waiter.result()
            await asyncio.to_thread(
                self.queue.heartbeat, job.id, self.owner, self.timing.lease_ttl_s
            )


def _read_head(path: Path) -> bytes:
    with path.open("rb") as fh:
        return fh.read(64)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Sleep ``seconds`` or until ``stop`` is set."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))


# ---- one account ------------------------------------------------------------------------------


class AccountLoop:
    """The §7 loop of one linked account."""

    def __init__(self, worker: Worker, row: AccountRow) -> None:
        self.worker = worker
        self.uid = row.uid
        self.acct = row.acct
        self.backoffs = 0
        self.archive: Archive | None = None
        self._inflight: asyncio.Future[SyncReport] | None = None
        self._client: CloudClient | None = None
        self._u = worker.rng.uniform

    def abort(self) -> None:
        """Close the cloud client of a poll in progress (shutdown): its request fails fast."""
        client = self._client
        if client is not None:
            client.close()

    async def run(self, stop: asyncio.Event) -> None:
        ctx = await self._context(stop)
        if ctx is None:
            return
        try:
            await asyncio.to_thread(self.start, ctx)
        except Exception:
            log_event(
                logger, "account_start_failed", level=logging.ERROR, exc_info=True, acct=self.acct
            )
            return
        while not stop.is_set():
            try:
                delay = await self.poll_once(ctx)
            except Exception:
                log_event(logger, "poll_failed", level=logging.ERROR, exc_info=True, acct=self.acct)
                self.backoffs += 1
                delay = backoff_delay(self.worker.settings.poll_seconds, self.backoffs, None)
            await _wait(stop, delay)

    async def _context(self, stop: asyncio.Event) -> AccountContext | None:
        while not stop.is_set():
            try:
                return await asyncio.to_thread(self.worker.context, self.uid, self.acct)
            except SchemaMismatch:
                log_event(logger, "account_waiting_for_schema", acct=self.acct)
                await _wait(stop, self.worker.timing.schema_wait_s)
        return None

    def start(self, ctx: AccountContext) -> None:
        """Finish or discard interrupted commits, then reconcile every live document's pages."""
        self.archive = Archive(ctx.paths)
        self.archive.sweep_staging()
        queued = self.worker.refresh(ctx, sorted(ctx.index.document_uuids(deleted=False)))
        log_event(logger, "account_start", acct=self.acct, count=queued)

    async def poll_once(self, ctx: AccountContext) -> float:
        """One poll (unless the account cannot poll); returns the delay before the next."""
        worker = self.worker
        settings = worker.settings
        if self._inflight is not None:
            # The watchdog gave up on the last poll: never let two overlap.
            if not self._inflight.done():
                return worker.timing.recheck_s
            self._inflight = None
        if await asyncio.to_thread(ctx.index.get_state, STATE_NEEDS_RELINK) == "true":
            return worker.timing.recheck_s
        assert worker.client_factory is not None
        async with worker.sync_slots():
            token = await asyncio.to_thread(worker.device_token, self.uid, self.acct)
            if token is None:
                log_event(logger, "poll_skipped", acct=self.acct, reason="no_device_token")
                return worker.timing.recheck_s
            client = worker.client_factory(token)
            self._client = client
            started = worker.monotonic()
            loop = asyncio.get_running_loop()
            fut = loop.run_in_executor(worker._poll_executor, self._sync, ctx, client)
            try:
                report = await asyncio.wait_for(
                    asyncio.shield(fut), timeout=settings.poll_timeout_seconds
                )
            except TimeoutError:
                client.close()  # fails the request in flight; the thread then returns
                self._inflight = fut
                with contextlib.suppress(TimeoutError, Exception):
                    await asyncio.wait_for(
                        asyncio.shield(fut), timeout=worker.timing.watchdog_grace_s
                    )
                if fut.done():
                    self._inflight = None
                return await asyncio.to_thread(self._record_failure, ctx, "watchdog")
            except Exception:
                client.close()
                log_event(logger, "poll_failed", level=logging.ERROR, exc_info=True, acct=self.acct)
                return await asyncio.to_thread(self._record_failure, ctx, "error")
            finally:
                if fut.done():
                    self._client = None
            client.close()
        log_event(
            logger,
            "poll",
            acct=self.acct,
            outcome=report.outcome,
            reason=report.reason if report.outcome != "anomaly" else report.anomaly,
            count=len(report.committed),
            bytes=report.bytes_downloaded,
            duration_ms=round((worker.monotonic() - started) * 1000, 1),
        )
        return await asyncio.to_thread(self.after_poll, ctx, report)

    def _sync(self, ctx: AccountContext, client: CloudClient) -> SyncReport:
        assert self.archive is not None
        return SyncOnce(
            client,
            self.archive,
            ctx.index,
            self.worker.sync_settings,
            self.worker.clock,
            monotonic=self.worker.monotonic,
        ).run()

    def _record_failure(self, ctx: AccountContext, outcome: str) -> float:
        log_event(logger, "poll_abandoned", level=logging.WARNING, acct=self.acct, outcome=outcome)
        ctx.index.set_state(STATE_LAST_POLL_AT, to_db_time(self.worker.clock()))
        ctx.index.set_state(STATE_LAST_POLL_OUTCOME, outcome)
        self.backoffs += 1
        delay = backoff_delay(self.worker.settings.poll_seconds, self.backoffs, None)
        return jittered(delay, self._u(-1.0, 1.0))

    def after_poll(self, ctx: AccountContext, report: SyncReport) -> float:
        """Record the poll, refresh changed documents' pages; returns the next delay."""
        index = ctx.index
        settings = self.worker.settings
        now = self.worker.clock()
        index.set_state(STATE_LAST_POLL_AT, to_db_time(now))
        index.set_state(STATE_LAST_POLL_OUTCOME, report.outcome)
        u = self._u(-1.0, 1.0)
        if report.outcome in ("ok", "partial"):
            self.backoffs = 0
            index.set_state(STATE_TOKEN_REJECTIONS, None)
            index.set_state(STATE_LAST_SYNC_AT, to_db_time(now))
            changed = dict.fromkeys((*report.committed, *report.untombstoned))
            if changed:
                self.worker.refresh(ctx, changed)
            if report.outcome == "partial" and report.reason in BUDGET_REASONS:
                return jittered(self.worker.timing.continue_s, u, spread=0.5)
            return jittered(settings.poll_seconds, u)
        if report.outcome == "backoff":
            self.backoffs += 1
            index.set_state(STATE_TOKEN_REJECTIONS, None)
            delay = backoff_delay(settings.poll_seconds, self.backoffs, report.retry_after_s)
            return jittered(delay, u)
        if report.outcome == "token_rejected":
            previous = index.get_state(STATE_TOKEN_REJECTIONS) or "0"
            count = (int(previous) if previous.isdigit() else 0) + 1
            index.set_state(STATE_TOKEN_REJECTIONS, str(count))
            if count >= RELINK_AFTER:
                index.set_state(STATE_NEEDS_RELINK, "true")
                log_event(logger, "needs_relink", level=logging.WARNING, acct=self.acct)
                return self.worker.timing.recheck_s
            return jittered(settings.poll_seconds, u)
        # anomaly: SyncOnce makes no cloud request while it is set; keep the normal interval
        # so the account resumes on its own once an administrator clears it.
        return jittered(settings.poll_seconds, u)
