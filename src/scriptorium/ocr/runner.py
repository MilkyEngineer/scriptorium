"""The worker's half of server-side OCR (Architecture §7 step 6, §8; Plan 05 §3.2).

- **The scan** runs after every ``ok`` or ``partial`` poll of an account. With auto-OCR
  enabled for some folders and a user or server key available (``resolve``), it lists the
  pages under those folders that have no transcript of their current version, keeps those
  that :func:`~scriptorium.ocr.debounce.is_stable`, drops pages already in a running batch
  or with an ``ocr`` job (a page version is queued once, ever, unless its last job was
  deferred), and bounds what is left by the cap's remaining room at an estimated price per
  page. More than ``OCR_BATCH_MIN`` pages go to Claude as one Message Batches request and an
  ``ocr_batch`` job polls it; fewer become single ``ocr`` jobs. Caps are checked through the
  ledger's ``allowance`` before every submission; a degraded allowance queues nothing.
  After an ``ok`` poll the scan records the time under
  :data:`~scriptorium.ocr.debounce.STATE_OCR_POLL_AT`, whatever the mode, for the next
  poll's stability check.
- **An ``ocr`` job** transcribes one page version: ``stale`` (nothing done) when the page
  changed since it was queued, ``exists`` when a transcript is already there (unless
  ``force``), ``deferred`` when no key or no room is available now; otherwise it renders the
  page if needed, downscales it to 2576 px in the render pool, calls Claude in a thread
  while heartbeating the lease, records the spend and stores the transcript with the prompt
  version, token counts and cost.
- **An ``ocr_batch`` job** polls its batch with the queue's backoff and, once it has ended,
  streams the results through :func:`~scriptorium.ocr.batches.collect`, records the spend at
  the batch price, stores what came back and falls back to single ``ocr`` jobs for the rest.

Transcripts are note content: they go from the client to the index and nowhere else. Logs,
job payloads, job results and job errors carry identifiers, counts, costs and reason codes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pydantic import SecretStr

from scriptorium.db.connect import from_db_time, to_db_time
from scriptorium.errors import InvalidArgumentError, NotFoundError, ScriptoriumError
from scriptorium.jobs.kinds import (
    MAX_BATCH_ITEMS,
    OCR,
    OCR_BATCH,
    BatchMode,
    OcrBatchSpec,
    OcrSpec,
    ocr_key,
    parse_ocr_batch_payload,
    parse_ocr_payload,
)
from scriptorium.jobs.queue import JobRow
from scriptorium.library import pages as pg
from scriptorium.library import render
from scriptorium.library.index import PageRow
from scriptorium.logging import log_event
from scriptorium.ocr import batches
from scriptorium.ocr._ports import BATCH_DISCOUNT, OcrPorts, TranscriptionLike
from scriptorium.ocr.debounce import STATE_OCR_POLL_AT, is_stable
from scriptorium.ocr.pipeline import (
    OCR_IMAGE_SIDE,
    SERVER_MODES,
    affordable_pages,
    blocked_reason,
    enqueue_page,
    is_deferred,
    load_auto_ocr,
    remaining_usd,
    resolve_mode,
    safe_model,
)
from scriptorium.users.secrets import SecretBox

if TYPE_CHECKING:
    from scriptorium.worker import AccountContext, Worker

__all__ = ["OCR_SLOTS", "JobFailure", "OcrRunner"]

logger = logging.getLogger(__name__)

OCR_SLOTS: Final = 4
"""OCR jobs the worker runs at once (per user at most ``OCR_USER_CONCURRENCY``)."""
SCAN_LIMIT: Final = 500
"""Pages without a transcript one scan looks at (stable or not)."""
EST_INPUT_TOKENS: Final = 5_000
EST_OUTPUT_TOKENS: Final = 1_500
"""A generous estimate of one page's tokens, to bound a scan's submissions by the cap."""
MAX_PNG_BYTES: Final = 64 * 1024 * 1024
PROMPT_VERSION_MAX: Final = 32


class JobFailure(Exception):  # noqa: N818 - a job outcome, not an error type for callers
    """An OCR job failed with a reason code; ``final`` jobs die without a retry."""

    def __init__(self, reason: str, *, final: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.final = final


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
    if isinstance(version, str) and 0 < len(version) <= PROMPT_VERSION_MAX:
        return version if version.isprintable() else None
    return None


def _deferred(reason: str) -> dict[str, Any]:
    return {"state": "deferred", "reason": reason}


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
        if job.kind == OCR:
            return await self.run_page(job)
        if job.kind == OCR_BATCH:
            return await self.run_batch(job)
        raise InvalidArgumentError("not an OCR job")

    async def _call[T](self, job: JobRow, fn: Callable[..., T], *args: Any) -> T:
        """Run a blocking OCR call in the OCR threads, heartbeating ``job``'s lease."""
        ports = self.ports
        assert ports is not None
        fut = self.executor.submit(fn, *args)
        try:
            return await self.worker._await_with_heartbeat(fut, job)
        except ports.refused:
            raise JobFailure("ocr_refused", final=True) from None
        except ports.unavailable:
            raise JobFailure("ocr_unavailable") from None

    # ---- one page --------------------------------------------------------------------------

    async def run_page(self, job: JobRow) -> Mapping[str, Any]:
        spec = parse_ocr_payload(job.payload)
        worker = self.worker
        if (
            job.acct is None
            or await asyncio.to_thread(worker.store.account, job.uid, job.acct) is None
        ):
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
        if ports is None:
            return _deferred("not_configured")
        settings = worker.settings
        now = worker.clock()
        resolution = await asyncio.to_thread(
            resolve_mode, ports, settings, worker.store, job.uid, now
        )
        mode, key = resolution.mode, resolution.api_key
        if mode not in SERVER_MODES or key is None:
            return _deferred(resolution.reason)
        allowance = await asyncio.to_thread(
            ports.ledger.allowance, settings, worker.store, job.uid, now
        )
        blocked = blocked_reason(allowance, mode)
        if blocked is not None:
            return _deferred(blocked)
        png = await self.ocr_png(ctx, spec, job)
        if png is None:
            return {"state": "stale"}
        client = ports.client(key)
        del key, resolution
        prompt = ports.prompt()
        result = await self._call(job, client.transcribe, png, prompt)
        outcome = batches.judge("p0", spec, result, ports.transcription)
        cost = 0.0
        if outcome.transcription is not None:
            cost = await asyncio.to_thread(self._record, job.uid, mode, outcome.transcription, 1.0)
        if outcome.error == "too_large":
            raise JobFailure("too_large", final=True)
        if outcome.error is not None or outcome.transcription is None:
            raise JobFailure("bad_result")
        stored = await asyncio.to_thread(
            self._store, ctx, spec, mode, outcome.transcription, prompt.version, cost
        )
        if not stored:
            return {"state": "stale"}
        return {
            "state": "stored",
            "input_tokens": outcome.transcription.input_tokens,
            "output_tokens": outcome.transcription.output_tokens,
            "cost_usd": round(cost, 6),
        }

    def _record(self, uid: str, mode: str, t: TranscriptionLike, factor: float) -> float:
        """Record one page's spend; returns its cost (``factor`` is the batch discount)."""
        ports = self.ports
        assert ports is not None
        worker = self.worker
        cost = ports.cost_usd(worker.settings, t.input_tokens, t.output_tokens) * factor
        ports.ledger.record(
            uid=uid,
            mode=mode,
            model=safe_model(t.model),
            input_tokens=t.input_tokens,
            output_tokens=t.output_tokens,
            cost_usd=cost,
            at=worker.clock(),
        )
        return cost

    @staticmethod
    def _store(
        ctx: AccountContext,
        spec: OcrSpec,
        mode: str,
        t: TranscriptionLike,
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
        if (
            job.acct is None
            or await asyncio.to_thread(worker.store.account, job.uid, job.acct) is None
        ):
            return {"state": "gone"}
        ctx = await asyncio.to_thread(worker.context, job.uid, job.acct)
        ports = self.ports
        key: SecretStr | None = None
        if ports is not None:
            box = SecretBox(worker.settings, job.uid)
            key = await asyncio.to_thread(ports.batch_key, worker.settings, box, bspec.mode)
        if ports is None or key is None:
            queued = await asyncio.to_thread(self._fall_back, ctx, bspec.items.values())
            return {"state": "orphaned", "fallback": queued}
        client = ports.client(key)
        del key
        try:
            status = await self._call(job, client.batch_status, bspec.batch_id)
        except JobFailure as exc:
            if not exc.final:
                raise
            # The key that submitted the batch is refused now: nobody can collect it.
            queued = await asyncio.to_thread(self._fall_back, ctx, bspec.items.values())
            return {"state": "orphaned", "fallback": queued}
        state = batches.poll_state(status)
        if state == "wait":
            raise JobFailure(batches.PENDING_REASON)
        if state == "unknown":
            raise JobFailure("batch_unknown_state")
        prompt_version = ports.prompt().version
        return await self._call(job, self._collect, ctx, job, bspec, client, prompt_version)

    def _collect(
        self,
        ctx: AccountContext,
        job: JobRow,
        bspec: OcrBatchSpec,
        client: Any,
        prompt_version: object,
    ) -> dict[str, Any]:
        """Stream the batch's results into the index (runs in an OCR thread)."""
        ports = self.ports
        assert ports is not None
        counts = dict.fromkeys(("stored", "exists", "stale", "fallback"), 0)
        cost = 0.0
        tally = batches.Tally()
        fall_back: list[OcrSpec] = []
        results = client.batch_results(bspec.batch_id)
        for outcome in batches.collect(
            results, bspec.items, transcription_type=ports.transcription, tally=tally
        ):
            spec, t = outcome.spec, outcome.transcription
            if t is not None:
                if ctx.index.transcript(spec.document, spec.page, spec.hash) is not None:
                    # Stored by an earlier run of this job (its spend is recorded) or by a
                    # lazy save; either way it is not replaced or counted again.
                    counts["exists"] += 1
                    continue
                page_cost = self._record(job.uid, bspec.mode, t, BATCH_DISCOUNT)
                cost += page_cost
                if outcome.error is None:
                    stored = self._store(ctx, spec, bspec.mode, t, prompt_version, page_cost)
                    counts["stored" if stored else "stale"] += 1
                    continue
            fall_back.append(spec)
        counts["fallback"] = self._fall_back(ctx, fall_back)
        counts["stale"] += len(fall_back) - counts["fallback"]
        return {
            "state": "collected",
            **counts,
            "unexpected": tally.unexpected + tally.duplicates,
            "truncated": tally.truncated,
            "cost_usd": round(cost, 6),
        }

    def _fall_back(self, ctx: AccountContext, specs: Any) -> int:
        """Single ``ocr`` jobs for batch items still current and untranscribed."""
        queued = 0
        for spec in specs:
            row = ctx.index.page_by_id(spec.document, spec.page)
            if row is None or row.content_hash != spec.hash:
                continue
            if ctx.index.transcript(spec.document, spec.page, spec.hash) is not None:
                continue
            job, how = enqueue_page(
                self.worker.queue, uid=ctx.uid, acct=ctx.acct, spec=spec, retry_failed=False
            )
            if job is not None and how in ("queued", "rerun"):
                queued += 1
        return queued

    # ---- the scan --------------------------------------------------------------------------

    async def scan(self, ctx: AccountContext, *, complete: bool) -> int:
        """Queue OCR for the stable, untranscribed pages under the auto-OCR folders.

        Returns the pages queued or submitted. ``complete`` (an ``ok`` poll) records this
        poll's time for the next scan's stability check, whatever happens here.
        """
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
        auto = await asyncio.to_thread(load_auto_ocr, index)
        if not auto.enabled or not auto.folders:
            return 0
        resolution = await asyncio.to_thread(
            resolve_mode, ports, settings, worker.store, ctx.uid, now
        )
        mode = resolution.mode
        if mode not in SERVER_MODES or resolution.api_key is None:
            return 0
        allowance = await asyncio.to_thread(
            ports.ledger.allowance, settings, worker.store, ctx.uid, now
        )
        blocked = blocked_reason(allowance, mode)
        if blocked is not None:
            log_event(logger, "ocr_scan_skipped", acct=ctx.acct, reason=blocked)
            return 0
        candidates, batch_running = await asyncio.to_thread(self._candidates, ctx, auto, now)
        if not candidates:
            return 0
        per_page = ports.cost_usd(settings, EST_INPUT_TOKENS, EST_OUTPUT_TOKENS)
        remaining = remaining_usd(allowance, mode)
        if len(candidates) > settings.ocr_batch_min:
            if batch_running:
                return 0  # one batch per account at a time; the rest goes in the next one
            afford = affordable_pages(remaining, per_page * BATCH_DISCOUNT, len(candidates))
            submitted = await self._submit_batch(ctx, candidates[:afford], mode, now)
            if submitted is not None:
                return submitted
        count = affordable_pages(remaining, per_page, len(candidates))
        queued = await asyncio.to_thread(self._enqueue_singles, ctx, candidates[:count])
        if queued:
            log_event(logger, "ocr_scan", acct=ctx.acct, outcome="single", count=queued)
        return queued

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
        for job in running:
            try:
                items = parse_ocr_batch_payload(job.payload).items.values()
            except ScriptoriumError:
                continue
            in_batch.update((s.document, s.page, s.hash) for s in items)
        generations: dict[str, int | None] = {}
        out: list[OcrSpec] = []
        for row in stable:
            if (row.document, row.page_id, row.content_hash) in in_batch:
                continue
            family = queue.family(
                ctx.uid, ocr_key(ctx.acct, row.document, row.page_id, row.content_hash)
            )
            if family and not is_deferred(family[-1]):
                continue
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
                self.worker.queue, uid=ctx.uid, acct=ctx.acct, spec=spec, retry_failed=False
            )
            if job is not None and how in ("queued", "rerun"):
                queued += 1
        return queued

    async def _submit_batch(
        self, ctx: AccountContext, specs: Sequence[OcrSpec], mode: str, now: datetime
    ) -> int | None:
        """Submit ``specs`` (those rendered already) as one batch and queue its job.

        None when too few pages are rendered for a batch: the caller queues single jobs,
        which render as they go. 0 when the submission did not happen (logged).
        """
        ports = self.ports
        assert ports is not None
        worker = self.worker
        pages: list[batches.Planned] = []
        for spec in specs:
            rel = pg.png_relpath(spec.page, spec.hash)
            base = await asyncio.to_thread(_read_png, pg.resolve_png(ctx.paths, rel))
            if base is None:
                continue
            png = await self._fit(ctx, spec.page, spec.hash, base, None)
            pages.append(batches.Planned(spec, png))
        planned = batches.plan(pages)
        if len(planned) <= worker.settings.ocr_batch_min:
            return None
        # Checked again right before the submission: another account of the user, or a
        # tool call, may have spent since the scan began.
        resolution = await asyncio.to_thread(
            resolve_mode, ports, worker.settings, worker.store, ctx.uid, worker.clock()
        )
        key = resolution.api_key
        if resolution.mode != mode or key is None:
            return 0
        allowance = await asyncio.to_thread(
            ports.ledger.allowance, worker.settings, worker.store, ctx.uid, worker.clock()
        )
        if blocked_reason(allowance, mode) is not None:
            return 0
        client = ports.client(key)
        del key, resolution
        try:
            batch_id = await asyncio.to_thread(
                batches.submit, client, ports.prompt(), planned, ports.batch_item
            )
        except Exception as exc:
            if isinstance(exc, InvalidArgumentError):
                reason = "bad_batch_id"
            elif isinstance(exc, ports.refused):
                reason = "ocr_refused"
            elif isinstance(exc, ports.unavailable):
                reason = "ocr_unavailable"
            else:
                raise
            log_event(
                logger,
                "ocr_batch_submit_failed",
                level=logging.WARNING,
                acct=ctx.acct,
                reason=reason,
            )
            return 0
        batch_mode: BatchMode = "user" if mode == "user" else "server"
        bspec = OcrBatchSpec(batch_id, batch_mode, {cid: p.spec for cid, p in planned.items()})
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
        log_event(
            logger,
            "ocr_batch_submitted",
            acct=ctx.acct,
            job=job.id,
            count=len(planned),
            bytes=sum(len(p.png) for p in planned.values()),
        )
        return len(planned)
