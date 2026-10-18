"""The worker's half of server-side OCR (Architecture §7 step 6, §8; Plan 05 §3.2).

- **The scan** runs after every ``ok`` or ``partial`` poll of an account. It first adopts any
  batch the API accepted whose ``ocr_batch`` job was never queued (``ocr_pending_batches``).
  With auto-OCR enabled for some folders and a user or server key available (``resolve``),
  it lists the pages under those folders that have no transcript of their current version,
  keeps those that :func:`~scriptorium.ocr.debounce.is_stable`, drops pages already in a
  running or pending batch or with an ``ocr`` job (a page version is queued once, ever,
  unless its last job died of a refused key), and bounds what is left by the cap's remaining
  room at an estimated price per page. More than ``OCR_BATCH_MIN`` pages go to Claude as one
  Message Batches request and an ``ocr_batch`` job polls it; fewer become single ``ocr``
  jobs. Caps are checked through the ledger's ``allowance`` for the resolved mode before
  every submission; a degraded allowance, or a key refused within ``KEY_REFUSED_HOLD``,
  queues nothing. Once a key with room and no refusal is available, the scan also wakes the
  account's deferred ``ocr`` jobs, whether or not auto-OCR is on. Nothing is scanned or sent
  for an account whose row is marked removed. After an ``ok`` poll the scan records the time
  under :data:`~scriptorium.ocr.debounce.STATE_OCR_POLL_AT`, whatever the mode, for the next
  poll's stability check.
- **An ``ocr`` job** transcribes one page version: ``gone`` when the account is unknown or
  marked removed, ``stale`` (nothing done) when the page changed since it was queued,
  ``exists`` when a transcript is already there (unless ``force``). When no key, no room or
  only a refused key is available now it is *deferred* (``JobQueue.defer``): put back under
  its own key, its attempt given back, until the refused key's hold ends, the month's caps
  reset, or :data:`DEFER_RECHECK` passes (no key), or earlier when a stored key, an allowed
  server key or the scan wakes it; waiting never spends an attempt or a re-run. Otherwise
  it renders the page if needed, downscales it to 2576 px in the render pool, checks it with
  F's ``check_png``, calls Claude in a thread while heartbeating the lease, records the
  spend (also for a refusal the API billed) and stores the transcript with the prompt
  version, token counts and cost. A refusal dies at once with its reason code
  (``key_<reason>`` when the key, not the page, was refused).
- **An ``ocr_batch`` job** polls its batch with the queue's backoff and, once it has ended,
  streams the results through :func:`~scriptorium.ocr.batches.collect`, records the spend at
  the batch price, stores what came back with the prompt version recorded at submission,
  records a ``dead`` ``ocr`` job for a page the API refused for good, and falls back to
  single ``ocr`` jobs for the rest.

Transcripts are note content: they go from the client to the index and nowhere else. Logs,
job payloads, job results and job errors carry identifiers, counts, costs and reason codes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn

from scriptorium.db.connect import from_db_time, to_db_time
from scriptorium.errors import InvalidArgumentError, NotFoundError, ScriptoriumError
from scriptorium.jobs.kinds import (
    MAX_BATCH_ITEMS,
    OCR,
    OCR_BATCH,
    PROMPT_VERSION_RE,
    BatchMode,
    OcrBatchSpec,
    OcrSpec,
    ocr_key,
    parse_ocr_batch_payload,
    parse_ocr_payload,
    rerun_key,
)
from scriptorium.jobs.queue import JobRow, LeaseLostError
from scriptorium.library import pages as pg
from scriptorium.library import render
from scriptorium.library.index import PageRow
from scriptorium.logging import log_event
from scriptorium.ocr import batches
from scriptorium.ocr._ports import OcrClientLike, OcrPorts
from scriptorium.ocr.client import Transcription
from scriptorium.ocr.debounce import STATE_OCR_POLL_AT, is_stable
from scriptorium.ocr.errors import KEY_REASONS, OcrRefused, OcrUnavailable
from scriptorium.ocr.ledger import cost_usd, month_bounds
from scriptorium.ocr.pipeline import (
    KEY_REFUSED_HOLD,
    OCR_IMAGE_SIDE,
    add_pending_batch,
    affordable_pages,
    blocked_reason,
    drop_pending_batch,
    enqueue_page,
    is_key_failure,
    key_error,
    key_mode,
    load_auto_ocr,
    load_key_refusal,
    load_pending_batches,
    note_key_refusal,
    remaining_usd,
    resolve_mode,
    safe_model,
)
from scriptorium.users.secrets import SecretBox

if TYPE_CHECKING:
    from scriptorium.worker import AccountContext, Worker

__all__ = ["OCR_SLOTS", "JobDeferred", "JobFailure", "OcrRunner"]

logger = logging.getLogger(__name__)

OCR_SLOTS: Final = 4
"""OCR jobs the worker runs at once (per user at most ``OCR_USER_CONCURRENCY``)."""
SCAN_LIMIT: Final = 500
"""Pages without a transcript one scan looks at (stable or not)."""
EST_INPUT_TOKENS: Final = 5_000
EST_OUTPUT_TOKENS: Final = 1_500
"""A generous estimate of one page's tokens, to bound a scan's submissions by the cap."""
MAX_PNG_BYTES: Final = 64 * 1024 * 1024
KEY_REFUSED: Final = "key_refused"
"""Deferral reason while the account's refused-key note applies to the mode."""
CAP_REASONS: Final = frozenset({"cap_reached_global", "cap_reached_user", "user_cap_reached"})
"""Deferral reasons that last until the UTC month's caps reset (or the scan finds room)."""
DEFER_RECHECK: Final = timedelta(hours=1)
"""How long a job deferred for want of a key waits before it looks again; storing a key,
being allowed the server key, or a scan that finds a usable key wakes it sooner."""
BATCH_KEY_REASONS: Final = KEY_REASONS - {"model_unavailable"}
"""Key reasons that a batch lookup can tell: a 404 there means the batch is unknown."""


class JobFailure(Exception):  # noqa: N818 - a job outcome, not an error type for callers
    """An OCR job failed with a reason code; ``final`` jobs die without a retry."""

    def __init__(self, reason: str, *, final: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.final = final


class JobDeferred(LeaseLostError):  # noqa: N818 - a job outcome, like JobFailure
    """The runner put the job back to wait (``JobQueue.defer``): nothing is left to report.

    A :class:`~scriptorium.jobs.queue.LeaseLostError` because that is what the worker's job
    loop already reads as "the job's row is settled; neither complete nor fail it".
    """


class _Defer(Exception):  # noqa: N818 - control flow inside the runner
    def __init__(self, reason: str, until: datetime) -> None:
        super().__init__(reason)
        self.reason = reason
        self.until = until


def _defer_until(reason: str, now: datetime) -> datetime:
    """When a job deferred because ``resolve`` found no usable key should look again."""
    if reason in CAP_REASONS:
        return month_bounds(now)[1]
    return now + DEFER_RECHECK


def _read_png(path: Path) -> bytes | None:
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_PNG_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(data) > MAX_PNG_BYTES:
        raise JobFailure("too_large", final=True)
    return data


def _prompt_version(version: object) -> str | None:
    if isinstance(version, str) and PROMPT_VERSION_RE.fullmatch(version):
        return version
    return None


class OcrRunner:
    """The worker's OCR jobs and auto-OCR scan; one per :class:`~scriptorium.worker.Worker`."""

    def __init__(self, worker: Worker) -> None:
        self.worker = worker
        self.executor = ThreadPoolExecutor(max_workers=OCR_SLOTS, thread_name_prefix="ocr")

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    @property
    def ports(self) -> OcrPorts | None:
        return self.worker.ocr

    async def run(self, job: JobRow) -> Mapping[str, Any]:
        """Run one leased OCR job; its result, or :class:`JobDeferred` once it is put back."""
        if job.kind == OCR:
            try:
                return await self.run_page(job)
            except _Defer as wait:
                await self._defer(job, wait)
        if job.kind == OCR_BATCH:
            return await self.run_batch(job)
        raise InvalidArgumentError("not an OCR job")

    async def _defer(self, job: JobRow, wait: _Defer) -> NoReturn:
        worker = self.worker
        await asyncio.to_thread(
            worker.queue.defer, job.id, worker.owner, until=wait.until, reason=wait.reason
        )
        log_event(logger, "ocr_deferred", job=job.id, acct=job.acct, reason=wait.reason)
        raise JobDeferred("The job was deferred.")

    def _gone(self, uid: str, acct: str | None) -> bool:
        """The account is unknown, or marked removed (its directory may not be moved yet)."""
        if acct is None:
            return True
        row = self.worker.store.account(uid, acct)
        return row is None or row.removed_at is not None

    async def _call[T](self, job: JobRow, fn: Callable[..., T], *args: Any) -> T:
        """Run a blocking OCR call in the OCR threads, heartbeating ``job``'s lease.

        ``OcrUnavailable`` becomes a retry; ``OcrRefused`` (a verdict) reaches the caller,
        which knows what was refused.
        """
        fut = self.executor.submit(fn, *args)
        try:
            return await self.worker._await_with_heartbeat(fut, job)
        except OcrUnavailable:
            raise JobFailure("ocr_unavailable") from None

    # ---- one page --------------------------------------------------------------------------

    async def run_page(self, job: JobRow) -> Mapping[str, Any]:
        spec = parse_ocr_payload(job.payload)
        worker = self.worker
        if job.acct is None or await asyncio.to_thread(self._gone, job.uid, job.acct):
            return {"state": "gone"}
        ctx = await asyncio.to_thread(worker.context, job.uid, job.acct)
        index = ctx.index
        row = await asyncio.to_thread(index.page_by_id, spec.document, spec.page)
        if row is None or row.content_hash != spec.hash:
            return {"state": "stale"}
        doc = await asyncio.to_thread(index.document, spec.document)
        if doc is None or doc.deleted_at is not None:
            return {"state": "gone"}
        if not spec.force and (
            await asyncio.to_thread(index.transcript, spec.document, spec.page, spec.hash)
            is not None
        ):
            return {"state": "exists"}
        ports = self.ports
        now = worker.clock()
        if ports is None:
            raise _Defer("not_configured", now + DEFER_RECHECK)
        settings = worker.settings
        choice = await asyncio.to_thread(resolve_mode, ports, settings, worker.store, job.uid, now)
        mode = key_mode(choice.mode)
        if mode is None or choice.api_key is None:
            raise _Defer(choice.reason, _defer_until(choice.reason, now))
        refusal = await asyncio.to_thread(load_key_refusal, index)
        if refusal is not None and refusal.applies(mode, now):
            raise _Defer(KEY_REFUSED, refusal.at + KEY_REFUSED_HOLD)
        allowance = await asyncio.to_thread(
            ports.ledger.allowance, settings, worker.store, job.uid, now, mode=mode
        )
        blocked = blocked_reason(allowance, mode)
        if blocked is not None:
            raise _Defer(blocked, month_bounds(now)[1])
        png = await self.ocr_png(ctx, spec, job)
        if png is None:
            return {"state": "stale"}
        unfit = batches.fits(png)
        if unfit is not None:
            raise JobFailure(unfit, final=True)
        if await asyncio.to_thread(self._gone, job.uid, job.acct):
            return {"state": "gone"}  # removed while the page was rendered: nothing is sent
        prompt = ports.prompt()
        client = ports.client(settings, choice.api_key)
        del choice
        try:
            result = await self._call(job, client.transcribe, png, prompt)
        except OcrRefused as exc:
            reason = await asyncio.to_thread(self._refused, ctx, mode, exc)
            raise JobFailure(reason, final=True) from None
        finally:
            client.close()
        outcome = batches.judge("p0", spec, result)
        t = outcome.transcription
        cost = 0.0
        if t is not None:
            cost = await asyncio.to_thread(
                self._record, job.uid, mode, t.input_tokens, t.output_tokens, t.model, False
            )
        if outcome.error == "too_large":
            raise JobFailure("too_large", final=True)
        if outcome.error is not None or t is None:
            raise JobFailure("bad_result")
        stored = await asyncio.to_thread(self._store, ctx, spec, mode, t, prompt.version, cost)
        if not stored:
            return {"state": "stale"}
        return {
            "state": "stored",
            "input_tokens": t.input_tokens,
            "output_tokens": t.output_tokens,
            "cost_usd": round(cost, 6),
        }

    def _refused(self, ctx: AccountContext, mode: BatchMode, exc: OcrRefused) -> str:
        """Record what a refused call was billed; note a refused key. The job's reason code."""
        if exc.input_tokens or exc.output_tokens:
            self._record(
                ctx.uid,
                mode,
                exc.input_tokens,
                exc.output_tokens,
                self.worker.settings.ocr_model,
                False,
            )
        if not exc.about_key:
            return exc.reason
        note_key_refusal(ctx.index, mode, exc.reason, self.worker.clock())
        log_event(
            logger, "ocr_key_refused", level=logging.WARNING, acct=ctx.acct, reason=exc.reason
        )
        return key_error(exc.reason)

    def _record(
        self,
        uid: str,
        mode: BatchMode,
        input_tokens: int,
        output_tokens: int,
        model: object,
        batch: bool,
    ) -> float:
        """Record one page's spend at F's price (half for a batch); returns its cost."""
        ports = self.ports
        assert ports is not None
        worker = self.worker
        cost = cost_usd(worker.settings, input_tokens, output_tokens, batch=batch)
        ports.ledger.record(
            uid=uid,
            mode=mode,
            model=safe_model(model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            at=worker.clock(),
        )
        return cost

    @staticmethod
    def _store(
        ctx: AccountContext,
        spec: OcrSpec,
        mode: str,
        t: Transcription,
        prompt_version: object,
        cost: float,
    ) -> bool:
        try:
            ctx.index.put_transcript(
                spec.document,
                spec.page,
                spec.hash,
                t.text,
                safe_model(t.model),
                mode,
                prompt_version=_prompt_version(prompt_version),
                input_tokens=t.input_tokens,
                output_tokens=t.output_tokens,
                cost_usd=cost,
            )
        except NotFoundError:  # the document no longer lists the page
            return False
        return True

    async def ocr_png(self, ctx: AccountContext, spec: OcrSpec, job: JobRow) -> bytes | None:
        """The page version's OCR image (longest side 2576 px); None if the version is gone.

        Rendered first if the archive PNG is missing (and recorded like a render job would);
        the downscale runs in the render pool and is cached next to the archive PNG under
        the same name ``read_page`` uses, so both paths send Claude the same bytes.
        """
        from scriptorium.worker import find_source

        worker = self.worker
        source = await asyncio.to_thread(find_source, ctx.paths, ctx.index, spec.render_spec())
        if source is None:
            return None
        small_rel = pg.png_relpath(source.page_id, source.content_hash, max_side=OCR_IMAGE_SIDE)
        small = await asyncio.to_thread(_read_png, pg.resolve_png(ctx.paths, small_rel))
        if small is not None:
            return small
        base_rel = pg.png_relpath(source.page_id, source.content_hash)
        base = await asyncio.to_thread(_read_png, pg.resolve_png(ctx.paths, base_rel))
        if base is None:
            base = await worker._render(ctx, source, None, job)
            await asyncio.to_thread(pg.write_png, ctx.paths, base_rel, base)
            await asyncio.to_thread(
                ctx.index.set_rendered,
                spec.document,
                source.page_id,
                source.content_hash,
                base_rel,
                worker.clock(),
            )
        return await self._fit(ctx, source.page_id, source.content_hash, base, job)

    async def _fit(
        self, ctx: AccountContext, page: str, content_hash: str, base: bytes, job: JobRow | None
    ) -> bytes:
        if max(pg.png_size(base)) <= OCR_IMAGE_SIDE:
            return base
        small = await self.worker._in_pool(
            job, render.downscale_png, base, max_side_px=OCR_IMAGE_SIDE
        )
        rel = pg.png_relpath(page, content_hash, max_side=OCR_IMAGE_SIDE)
        await asyncio.to_thread(pg.write_png, ctx.paths, rel, small)
        return small

    # ---- one batch -------------------------------------------------------------------------

    async def run_batch(self, job: JobRow) -> Mapping[str, Any]:
        bspec = parse_ocr_batch_payload(job.payload)
        worker = self.worker
        if job.acct is None or await asyncio.to_thread(self._gone, job.uid, job.acct):
            return {"state": "gone"}
        ctx = await asyncio.to_thread(worker.context, job.uid, job.acct)
        ports = self.ports
        if ports is None:
            return await self._orphan(ctx, bspec, "not_configured")
        box = SecretBox(worker.settings, job.uid)
        key = await asyncio.to_thread(ports.batch_key, worker.settings, box, bspec.mode)
        if key is None:
            return await self._orphan(ctx, bspec, "no_key")
        client = ports.client(worker.settings, key)
        del key
        try:
            status = await self._call(job, client.batch_status, bspec.batch_id)
            state = batches.poll_state(status)
            if state == "wait":
                raise JobFailure(batches.PENDING_REASON)
            if state == "unknown":
                raise JobFailure("batch_unknown_state")
            if status.state == "expired":
                # Archived, or unknown to this key: the results cannot be read any more.
                return await self._orphan(ctx, bspec, "expired")
            return await self._call(job, self._collect, ctx, job, bspec, client)
        except OcrRefused as exc:
            # The key that submitted the batch is refused now, or the results could not be
            # read: nobody can collect it. Whatever was stored stays; the rest goes single.
            reason = exc.reason
            if reason in BATCH_KEY_REASONS:
                await asyncio.to_thread(
                    note_key_refusal, ctx.index, bspec.mode, reason, worker.clock()
                )
                reason = key_error(reason)
            return await self._orphan(ctx, bspec, reason)
        finally:
            client.close()

    async def _orphan(
        self, ctx: AccountContext, bspec: OcrBatchSpec, reason: str
    ) -> dict[str, Any]:
        queued = await asyncio.to_thread(self._fall_back, ctx, bspec.items.values())
        return {"state": "orphaned", "reason": reason, "fallback": queued}

    def _collect(
        self, ctx: AccountContext, job: JobRow, bspec: OcrBatchSpec, client: OcrClientLike
    ) -> dict[str, Any]:
        """Stream the batch's results into the index (runs in an OCR thread)."""
        counts = dict.fromkeys(("stored", "exists", "stale", "fallback", "failed"), 0)
        cost = 0.0
        tally = batches.Tally()
        fall_back: list[OcrSpec] = []
        dead: list[tuple[OcrSpec, str]] = []
        refused_key: str | None = None
        model = self.worker.settings.ocr_model
        results = client.batch_results(bspec.batch_id)
        for outcome in batches.collect(results, bspec.items, tally=tally):
            spec, t = outcome.spec, outcome.transcription
            if t is not None:
                if ctx.index.transcript(spec.document, spec.page, spec.hash) is not None:
                    # Stored by an earlier run of this job (its spend is recorded) or by a
                    # lazy save; either way it is not replaced or counted again.
                    counts["exists"] += 1
                    continue
                page_cost = self._record(
                    job.uid, bspec.mode, t.input_tokens, t.output_tokens, t.model, True
                )
                cost += page_cost
                if outcome.error is None:
                    stored = self._store(ctx, spec, bspec.mode, t, bspec.prompt_version, page_cost)
                    counts["stored" if stored else "stale"] += 1
                elif outcome.error == "too_large":
                    dead.append((spec, "too_large"))  # the same page would be too large again
                else:
                    fall_back.append(spec)
                continue
            if outcome.billed != (0, 0):
                cost += self._record(job.uid, bspec.mode, *outcome.billed, model, True)
            error = outcome.error or "item_error"
            if error in KEY_REASONS:
                refused_key = error
                fall_back.append(spec)  # deferred while the key's note applies
            elif outcome.retryable:
                fall_back.append(spec)
            else:
                dead.append((spec, error))
        if refused_key is not None:
            note_key_refusal(ctx.index, bspec.mode, refused_key, self.worker.clock())
        counts["failed"] = self._settle(ctx, dead)
        counts["fallback"] = self._fall_back(ctx, fall_back)
        counts["stale"] += len(fall_back) - counts["fallback"]
        return {
            "state": "collected",
            **counts,
            "unexpected": tally.unexpected + tally.duplicates,
            "truncated": tally.truncated,
            "cost_usd": round(cost, 6),
        }

    def _current(self, ctx: AccountContext, spec: OcrSpec) -> bool:
        """Whether ``spec`` is still the page's version and has no transcript yet."""
        row = ctx.index.page_by_id(spec.document, spec.page)
        if row is None or row.content_hash != spec.hash:
            return False
        return ctx.index.transcript(spec.document, spec.page, spec.hash) is None

    def _fall_back(self, ctx: AccountContext, specs: Iterable[OcrSpec]) -> int:
        """Single ``ocr`` jobs for batch items still current and untranscribed."""
        queued = 0
        for spec in specs:
            if not self._current(ctx, spec):
                continue
            job, how = enqueue_page(
                self.worker.queue,
                uid=ctx.uid,
                acct=ctx.acct,
                spec=spec,
                retry_failed=False,
                retry_key_failure=True,
            )
            if job is not None and how in ("queued", "rerun"):
                queued += 1
        return queued

    def _settle(self, ctx: AccountContext, dead: Sequence[tuple[OcrSpec, str]]) -> int:
        """A ``dead`` ``ocr`` job for each page the API refused for good inside the batch.

        The page version then has a finished job like any refused page, so the scan does
        not send it again; ``transcribe`` can still re-run it on request.
        """
        queue = self.worker.queue
        settled = 0
        for spec, reason in dead:
            if not self._current(ctx, spec):
                continue
            key = spec.key(ctx.acct)
            family = queue.family(ctx.uid, key)
            if family:
                latest = family[-1]
                if latest.state not in ("done", "dead", "cancelled"):
                    continue  # a job for the page is already under way; let it finish
                key = rerun_key(latest.dedupe_key, latest.id)
            queue.record_dead(
                uid=ctx.uid,
                acct=ctx.acct,
                kind=OCR,
                dedupe_key=key,
                payload=spec.payload(),
                error=reason,
            )
            settled += 1
        return settled

    # ---- the scan --------------------------------------------------------------------------

    async def scan(self, ctx: AccountContext, *, complete: bool) -> int:
        """Queue OCR for the stable, untranscribed pages under the auto-OCR folders.

        Returns the pages queued or submitted. ``complete`` (an ``ok`` poll) records this
        poll's time for the next scan's stability check, whatever happens here. An account
        marked removed is left alone.
        """
        if await asyncio.to_thread(self._gone, ctx.uid, ctx.acct):
            return 0
        now = self.worker.clock()
        try:
            return await self._scan(ctx, now)
        finally:
            if complete:
                await asyncio.to_thread(ctx.index.set_state, STATE_OCR_POLL_AT, to_db_time(now))

    async def _scan(self, ctx: AccountContext, now: datetime) -> int:
        ports = self.ports
        if ports is None:
            return 0
        worker = self.worker
        settings = worker.settings
        index = ctx.index
        adopted = await asyncio.to_thread(self._adopt_pending, ctx)
        if adopted:
            log_event(logger, "ocr_batch_adopted", acct=ctx.acct, count=adopted)
        auto = await asyncio.to_thread(load_auto_ocr, index)
        scanning = auto.enabled and bool(auto.folders)
        if not scanning and not await asyncio.to_thread(
            worker.queue.deferred_counts, ctx.uid, acct=ctx.acct, kinds=(OCR,)
        ):
            return 0
        choice = await asyncio.to_thread(resolve_mode, ports, settings, worker.store, ctx.uid, now)
        mode = key_mode(choice.mode)
        del choice
        if mode is None:
            return 0
        refusal = await asyncio.to_thread(load_key_refusal, index)
        if refusal is not None and refusal.applies(mode, now):
            log_event(logger, "ocr_scan_skipped", acct=ctx.acct, reason=KEY_REFUSED)
            return 0
        allowance = await asyncio.to_thread(
            ports.ledger.allowance, settings, worker.store, ctx.uid, now, mode=mode
        )
        blocked = blocked_reason(allowance, mode)
        if blocked is not None:
            log_event(logger, "ocr_scan_skipped", acct=ctx.acct, reason=blocked)
            return 0
        # A key with room and no refusal: whatever the deferred jobs were waiting for is here.
        woken = await asyncio.to_thread(
            worker.queue.wake_deferred, ctx.uid, acct=ctx.acct, kinds=(OCR,)
        )
        if woken:
            log_event(logger, "ocr_woken", acct=ctx.acct, count=woken)
        if not scanning:
            return 0
        candidates, batch_running = await asyncio.to_thread(self._candidates, ctx, auto, now)
        if not candidates:
            return 0
        remaining = remaining_usd(allowance, mode)
        if len(candidates) > settings.ocr_batch_min:
            if batch_running:
                return 0  # one batch per account at a time; the rest goes in the next one
            per_item = cost_usd(settings, EST_INPUT_TOKENS, EST_OUTPUT_TOKENS, batch=True)
            afford = affordable_pages(remaining, per_item, len(candidates))
            submitted = await self._submit_batch(ctx, candidates[:afford], mode, now)
            if submitted is not None:
                return submitted
        per_page = cost_usd(settings, EST_INPUT_TOKENS, EST_OUTPUT_TOKENS)
        count = affordable_pages(remaining, per_page, len(candidates))
        queued = await asyncio.to_thread(self._enqueue_singles, ctx, candidates[:count])
        if queued:
            log_event(logger, "ocr_scan", acct=ctx.acct, outcome="single", count=queued)
        return queued

    def _adopt_pending(self, ctx: AccountContext) -> int:
        """Queue the ``ocr_batch`` job of every pending batch that has none; forget them."""
        queue = self.worker.queue
        adopted = 0
        for bspec in load_pending_batches(ctx.index):
            key = bspec.key(ctx.acct)
            if not queue.family(ctx.uid, key):
                queue.enqueue(
                    uid=ctx.uid,
                    acct=ctx.acct,
                    kind=bspec.kind,
                    dedupe_key=key,
                    payload=bspec.payload(),
                    max_attempts=batches.BATCH_MAX_ATTEMPTS,
                )
                adopted += 1
            drop_pending_batch(ctx.index, bspec.batch_id)
        return adopted

    def _candidates(
        self, ctx: AccountContext, auto: Any, now: datetime
    ) -> tuple[list[OcrSpec], bool]:
        """The stable pages to transcribe now (at most one batch's worth), and whether a
        batch of this account is still running."""
        index = ctx.index
        queue = self.worker.queue
        raw = index.get_state(STATE_OCR_POLL_AT)
        previous: datetime | None = None
        if raw is not None:
            try:
                previous = from_db_time(raw)
            except ValueError:
                previous = None
        rows = index.pages_without_transcript(folders=auto.folders, limit=SCAN_LIMIT)
        stable = [r for r in rows if is_stable(r, now=now, previous_poll_at=previous)]
        running = queue.active(ctx.uid, acct=ctx.acct, kind=OCR_BATCH)
        in_batch: set[tuple[str, str, str]] = set()
        specs: list[OcrBatchSpec] = load_pending_batches(index)
        for job in running:
            try:
                specs.append(parse_ocr_batch_payload(job.payload))
            except ScriptoriumError:
                continue
        for bspec in specs:
            in_batch.update((s.document, s.page, s.hash) for s in bspec.items.values())
        generations: dict[str, int | None] = {}
        out: list[OcrSpec] = []
        for row in stable:
            if (row.document, row.page_id, row.content_hash) in in_batch:
                continue
            family = queue.family(
                ctx.uid, ocr_key(ctx.acct, row.document, row.page_id, row.content_hash)
            )
            if family and not is_key_failure(family[-1]):
                continue  # queued, deferred, running or finished: one job per page version
            generation = self._generation(ctx, row, generations)
            if generation is None:
                continue
            out.append(OcrSpec(row.document, row.page_id, row.content_hash, generation))
            if len(out) >= MAX_BATCH_ITEMS:
                break
        return out, bool(running)

    @staticmethod
    def _generation(ctx: AccountContext, row: PageRow, cache: dict[str, int | None]) -> int | None:
        if row.document not in cache:
            try:
                version = pg.current_version(ctx.paths, ctx.index, row.document)
            except ScriptoriumError:
                version = None
            cache[row.document] = None if version is None else version.generation
        return cache[row.document]

    def _enqueue_singles(self, ctx: AccountContext, specs: Sequence[OcrSpec]) -> int:
        queued = 0
        for spec in specs:
            job, how = enqueue_page(
                self.worker.queue,
                uid=ctx.uid,
                acct=ctx.acct,
                spec=spec,
                retry_failed=False,
                retry_key_failure=True,
            )
            if job is not None and how in ("queued", "rerun"):
                queued += 1
        return queued

    async def _submit_batch(
        self, ctx: AccountContext, specs: Sequence[OcrSpec], mode: BatchMode, now: datetime
    ) -> int | None:
        """Submit ``specs`` (those rendered already) as one batch and queue its job.

        None when too few pages are rendered for a batch: the caller queues single jobs,
        which render as they go. 0 when the submission did not happen (logged). A page whose
        image the API would refuse (F's ``check_png``) is left out of the batch and queued
        as a single job, which dies with the reason.
        """
        ports = self.ports
        assert ports is not None
        worker = self.worker
        pages: list[batches.Planned] = []
        unfit: list[OcrSpec] = []
        for spec in specs:
            rel = pg.png_relpath(spec.page, spec.hash)
            base = await asyncio.to_thread(_read_png, pg.resolve_png(ctx.paths, rel))
            if base is None:
                continue
            png = await self._fit(ctx, spec.page, spec.hash, base, None)
            if batches.fits(png) is not None:
                unfit.append(spec)
                continue
            pages.append(batches.Planned(spec, png))
        planned = batches.plan(pages)
        if len(planned) <= worker.settings.ocr_batch_min:
            return None
        # Checked again right before the submission: another account of the user, or a
        # tool call, may have spent since the scan began.
        choice = await asyncio.to_thread(
            resolve_mode, ports, worker.settings, worker.store, ctx.uid, worker.clock()
        )
        key = choice.api_key if choice.mode == mode else None
        del choice
        if key is None:
            return 0
        allowance = await asyncio.to_thread(
            ports.ledger.allowance,
            worker.settings,
            worker.store,
            ctx.uid,
            worker.clock(),
            mode=mode,
        )
        if blocked_reason(allowance, mode) is not None:
            return 0
        if await asyncio.to_thread(self._gone, ctx.uid, ctx.acct):
            return 0  # removed since the scan began: nothing is sent for it
        prompt = ports.prompt()
        client = ports.client(worker.settings, key)
        del key
        try:
            batch_id = await asyncio.to_thread(batches.submit, client, prompt, planned)
        except (InvalidArgumentError, OcrRefused, OcrUnavailable) as exc:
            if isinstance(exc, OcrRefused):
                reason = await asyncio.to_thread(self._refused, ctx, mode, exc)
            elif isinstance(exc, OcrUnavailable):
                reason = "ocr_unavailable"
            else:
                reason = "bad_batch_id"
            log_event(
                logger,
                "ocr_batch_submit_failed",
                level=logging.WARNING,
                acct=ctx.acct,
                reason=reason,
            )
            return 0
        finally:
            client.close()
        bspec = OcrBatchSpec(
            batch_id,
            mode,
            {cid: p.spec for cid, p in planned.items()},
            prompt_version=_prompt_version(prompt.version),
        )
        # Paid for from here on: remembered before anything else can fail, so the next scan
        # adopts the batch if its job cannot be queued now.
        await asyncio.to_thread(add_pending_batch, ctx.index, bspec)
        job = await asyncio.to_thread(
            worker.queue.enqueue,
            uid=ctx.uid,
            acct=ctx.acct,
            kind=bspec.kind,
            dedupe_key=bspec.key(ctx.acct),
            payload=bspec.payload(),
            max_attempts=batches.BATCH_MAX_ATTEMPTS,
            run_at=now + batches.BATCH_FIRST_POLL,
        )
        await asyncio.to_thread(drop_pending_batch, ctx.index, batch_id)
        log_event(
            logger,
            "ocr_batch_submitted",
            acct=ctx.acct,
            job=job.id,
            count=len(planned),
            bytes=sum(len(p.png) for p in planned.values()),
        )
        singles = await asyncio.to_thread(self._enqueue_singles, ctx, unfit)
        return len(planned) + singles
