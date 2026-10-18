"""OCR tools (§11.3): save_transcript, transcribe, ocr_status; the lazy instruction (§8.2).

In lazy mode the user's own Claude session transcribes: ``read_page`` returns the page image
with :data:`LAZY_INSTRUCTION`, and ``save_transcript`` stores the result keyed by
``(document, page uuid, content_hash)``: documents uploaded from the same files share page
UUIDs, so each document's page has its own transcript. A transcript is accepted only for the
page's *current* hash (``stale_hash`` otherwise), so a page edited since it was read is never
mislabelled.

With a user or server key (M2, §8.1) the worker transcribes: ``transcribe`` queues ``ocr``
jobs, ``read_page`` queues one on a miss and waits ``PENDING_WAIT_SECONDS`` for it
(:func:`await_server_transcript`), and ``ocr_status`` reports the queue, the last failure
reasons and the month's spend. The server never holds a key beyond the ``resolve`` call
that decides the mode; the worker resolves again before it spends anything.

Transcripts are note content: they are stored, returned to their owner, and never logged
or audited (the audit row carries only their byte count).
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from typing import Annotated, Any, Final

from mcp_types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from scriptorium.jobs.kinds import OCR_KINDS, OcrSpec
from scriptorium.library import pages as pg
from scriptorium.library.index import DocumentRow, TranscriptRow
from scriptorium.ocr.batches import PENDING_REASON
from scriptorium.ocr.pipeline import (
    KEY_REFUSED_HOLD,
    LAZY,
    OCR_IMAGE_SIDE,
    SERVER_MODES,
    TRANSCRIPT_MAX_BYTES,
    KeyRefusal,
    enqueue_page,
    load_auto_ocr,
    load_key_refusal,
)
from scriptorium.tools import ToolContext, ToolRegistry, ToolSpec
from scriptorium.tools._conventions import (
    READ_ONLY,
    RESULT_CAP_BYTES,
    ErrorCode,
    ToolArgs,
    ToolError,
    invalid_argument,
    iso_utc,
    json_result,
    json_text,
    page_to_index,
    tool_annotations,
)
from scriptorium.tools._library import (
    ACCT_PATTERN,
    IDENTIFIER_MAX,
    Account,
    Pages,
    downscaled,
    load_pages,
    page_png,
    page_ref,
    pick_page,
    services,
    use_account,
    use_document,
)
from scriptorium.tools._ocr import OCR_SERVER_NOTE, ocr_mode, ocr_queue, ocr_summary
from scriptorium.tools._services import Services

MODEL_MAX: Final = 100
LAZY_MODE: Final = "lazy"

LAZY_INSTRUCTION: Final = (
    "This page has no transcript yet. Transcribe the image verbatim as Markdown: keep its "
    "structure (headings, lists, `- [ ]` for check boxes, arrows, tables where the page "
    "rules them), write [?] for a word you cannot read, describe a sketch in one bracketed "
    "line, and never summarise. Then call save_transcript with the document, page and "
    "content_hash below, the Markdown as text and your model name as model, so the next "
    "read is instant and the page becomes searchable."
)
"""What ``read_page`` tells the client in lazy mode (the §8.2 rules; M2's server-side OCR
takes its prompt from ``ocr/prompt.md``)."""
OLD_VERSION_NOTE: Final = (
    "This is an older version of the page and it has no transcript. Read it from the image; "
    "a transcript can only be saved for the current version."
)


class SaveTranscriptArgs(ToolArgs):
    document: str = Field(
        min_length=1, max_length=IDENTIFIER_MAX, description="Document UUID or path of names."
    )
    page: int = Field(ge=1, le=100_000, description="Page number, from 1.")
    content_hash: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="content_hash as read_page returned it."
    )
    text: str = Field(
        min_length=1,
        max_length=TRANSCRIPT_MAX_BYTES,
        description="The transcript as Markdown (at most 30 KB).",
    )
    model: str = Field(
        min_length=1, max_length=MODEL_MAX, description="The model that transcribed the page."
    )
    account: str | None = Field(
        default=None, pattern=ACCT_PATTERN, description="Account id; the linked one by default."
    )


def _save(account: Account, doc: DocumentRow, args: SaveTranscriptArgs) -> str:
    pages = load_pages(account, doc, None)
    source = pick_page(pages, args.page)
    if source.content_hash != args.content_hash:
        raise ToolError(
            ErrorCode.STALE_HASH,
            "The page has changed since that content_hash was read.",
            "Call read_page again and transcribe the current image.",
        )
    index = account.index
    row = index.page_by_id(doc.uuid, source.page_id)
    if row is None or row.content_hash != source.content_hash:
        raise ToolError(
            ErrorCode.NOT_FOUND,
            "This page is not indexed yet.",
            "The worker indexes new pages right after each sync; try again in a minute.",
        )
    index.put_transcript(
        doc.uuid, source.page_id, source.content_hash, args.text, args.model, LAZY_MODE
    )
    return source.page_id


async def save_transcript(ctx: ToolContext, args: SaveTranscriptArgs) -> CallToolResult:
    """Store your transcript of a page (lazy OCR mode): after read_page returned a page's
    image with instructions, send the Markdown you transcribed together with the document,
    page and content_hash it gave you, and your model name. Fails with stale_hash if the page
    has changed since; read it again then. The transcript makes later reads instant and the
    page searchable."""
    size = len(args.text.encode("utf-8"))
    if size > TRANSCRIPT_MAX_BYTES:
        raise invalid_argument(
            f"A transcript is at most {TRANSCRIPT_MAX_BYTES // 1024} KB.",
            "Shorten it: transcribe the page, do not annotate it.",
        )
    if not args.model.isprintable():
        raise invalid_argument("model must be a printable name, such as claude-sonnet-5-5.")
    account = await use_account(ctx, args.account)
    doc = await use_document(ctx, account, args.document)
    ctx.audit["page"] = args.page
    await asyncio.to_thread(_save, account, doc, args)
    ctx.audit["bytes"] = size
    return json_result(
        {
            "saved": True,
            "document": doc.uuid,
            "page": args.page,
            "content_hash": args.content_hash,
            "bytes": size,
        },
        cap=RESULT_CAP_BYTES,
    )


# ---- the mode ----------------------------------------------------------------------------------


LAZY_MAX_PAGES: Final = 3
"""Page images ``transcribe`` returns per call in lazy mode (each is a few hundred KB)."""
TRANSCRIBE_MAX_PAGES: Final = 200
RECENT_ERRORS: Final = 5
_REASON: Final = re.compile(r"[a-z_]{1,40}")
LAZY_TRANSCRIBE_NOTE: Final = (
    "Lazy mode: no API key is available to this server for your pages, so you transcribe "
    "them. For each page below, follow the instructions and call save_transcript. To have "
    "the server transcribe in the background instead, set your own Claude API key with "
    "set_claude_api_key or ask the operator for the server key."
)


def _reason(error: str | None) -> str:
    return error if error is not None and _REASON.fullmatch(error) else "other"


# ---- read_page's server-side path --------------------------------------------------------------


async def await_server_transcript(
    ctx: ToolContext, account: Account, doc: DocumentRow, source: pg.PageSource, generation: int
) -> TranscriptRow | None:
    """With a user or server key: queue OCR for the current page version and wait for it.

    Returns the transcript if it arrives within ``PENDING_WAIT_SECONDS``; raises ``pending``
    with the job id if the job is still queued or running then. None means "answer as in
    lazy mode": no key, a page not indexed yet, or a job that finished without a transcript
    (refused, failed for good, re-runs spent). A deferred job is queued again, since a key is
    available now.
    """
    svc = services(ctx)
    mode, _ = await asyncio.to_thread(ocr_mode, svc, account.uid)
    if mode not in SERVER_MODES:
        return None
    index = account.index
    row = await asyncio.to_thread(index.page_by_id, doc.uuid, source.page_id)
    if row is None or row.content_hash != source.content_hash:
        return None
    spec = OcrSpec(doc.uuid, source.page_id, source.content_hash, generation)
    # A dead job is not retried from here: every read of a page the API refuses would pay
    # again. transcribe retries it on request.
    job, _how = await asyncio.to_thread(
        enqueue_page, svc.queue, uid=account.uid, acct=account.acct, spec=spec, retry_failed=False
    )
    if job is None:
        return None
    ctx.audit["job"] = job.id
    deadline = time.monotonic() + svc.settings.pending_wait_seconds
    while True:
        transcript = await asyncio.to_thread(
            index.transcript, doc.uuid, source.page_id, source.content_hash
        )
        if transcript is not None:
            return transcript
        if job.state in ("done", "dead", "cancelled"):
            return None
        if time.monotonic() >= deadline:
            raise ToolError(
                ErrorCode.PENDING,
                "The page is being transcribed.",
                "Call read_page again in a few seconds; ocr_status shows the queue.",
                extra={"job": job.id, "state": job.state},
            )
        await svc.sleep(svc.poll_interval_s)
        fresh = await asyncio.to_thread(svc.queue.get, job.id, account.uid)
        job = job if fresh is None else fresh


# ---- transcribe --------------------------------------------------------------------------------


class TranscribeArgs(ToolArgs):
    document: str = Field(
        min_length=1, max_length=IDENTIFIER_MAX, description="Document UUID or path of names."
    )
    pages: list[Annotated[int, Field(ge=1, le=100_000)]] = Field(
        default_factory=list,
        max_length=TRANSCRIBE_MAX_PAGES,
        description="Page numbers, from 1; every page of the document when empty.",
    )
    force: bool = Field(
        default=False, description="Transcribe again even pages that have a transcript."
    )
    account: str | None = Field(
        default=None, pattern=ACCT_PATTERN, description="Account id; the linked one by default."
    )


def _page_numbers(pages: Pages, wanted: list[int]) -> list[int]:
    if not pages.sources:
        raise invalid_argument("This document has no pages.")
    count = len(pages.sources)
    numbers = sorted(set(wanted)) if wanted else list(range(1, count + 1))
    for number in numbers:
        page_to_index(number, count)
    if len(numbers) > TRANSCRIBE_MAX_PAGES:
        raise invalid_argument(
            f"At most {TRANSCRIBE_MAX_PAGES} pages per call; this document has {count}.",
            "Pass pages in groups.",
        )
    return numbers


def _queue_pages(
    svc: Services,
    account: Account,
    doc: DocumentRow,
    pages: Pages,
    numbers: list[int],
    force: bool,
) -> list[dict[str, object]]:
    index = account.index
    out: list[dict[str, object]] = []
    for number in numbers:
        source = pick_page(pages, number)
        item: dict[str, object] = {"page": number}
        row = index.page_by_id(doc.uuid, source.page_id)
        if row is None or row.content_hash != source.content_hash:
            item["state"] = "not_indexed"
        elif not force and index.transcript(doc.uuid, source.page_id, source.content_hash):
            item["state"] = "transcribed"
        else:
            spec = OcrSpec(
                doc.uuid, source.page_id, source.content_hash, pages.version.generation, force
            )
            job, how = enqueue_page(
                svc.queue, uid=account.uid, acct=account.acct, spec=spec, retry_failed=True
            )
            if job is None:
                item["state"] = "rerun_cap"
            else:
                item.update({"job": job.id, "state": job.state, "how": how})
        out.append(item)
    return out


async def _lazy_pages(
    ctx: ToolContext,
    account: Account,
    doc: DocumentRow,
    pages: Pages,
    numbers: list[int],
    force: bool,
    reason: str,
) -> CallToolResult:
    content: list[TextContent | ImageContent] = []
    listed: list[dict[str, object]] = []
    total = 0
    for number in numbers:
        source = pick_page(pages, number)
        ref = page_ref(doc, pages, source)
        exists = await asyncio.to_thread(
            account.index.transcript, doc.uuid, source.page_id, source.content_hash
        )
        if exists is not None and not force:
            listed.append({**ref, "transcript": "exists"})
            continue
        png = await page_png(ctx, account, source, pages.version.generation)
        png = await downscaled(ctx, account, source, png, OCR_IMAGE_SIDE)
        total += len(png)
        listed.append({**ref, "transcript": None})
        content.append(TextContent(text=json_text(ref)))
        content.append(
            ImageContent(data=base64.b64encode(png).decode("ascii"), mime_type="image/png")
        )
    head = {"mode": LAZY, "reason": reason, "document": doc.uuid, "pages": listed}
    note = LAZY_TRANSCRIBE_NOTE if content else "Every page asked for has a transcript already."
    text = f"{note}\n\n{LAZY_INSTRUCTION if content else ''}\n\n{json_text(head)}".strip()
    ctx.audit["bytes"] = total
    return CallToolResult(content=[TextContent(text=text), *content])


async def transcribe(ctx: ToolContext, args: TranscribeArgs) -> CallToolResult:
    """Transcribe pages of a reMarkable document (all pages when pages is empty). With an
    API key available (your own, or the server's if the operator allows it) the pages are
    queued for background transcription and the call returns the job ids and the mode; read
    the pages with read_page once the jobs are done. In lazy mode (no key) it returns up to
    three page images with instructions for you to transcribe and save with
    save_transcript. force transcribes pages again even if they have a transcript."""
    account = await use_account(ctx, args.account)
    doc = await use_document(ctx, account, args.document)
    svc = services(ctx)
    pages = await asyncio.to_thread(load_pages, account, doc, None)
    numbers = _page_numbers(pages, args.pages)
    ctx.audit["pages"] = numbers[:50]
    ctx.audit["generation"] = pages.version.generation
    mode, reason = await asyncio.to_thread(ocr_mode, svc, account.uid)
    if mode not in SERVER_MODES:
        if len(numbers) > LAZY_MAX_PAGES:
            raise invalid_argument(
                f"In lazy mode transcribe returns at most {LAZY_MAX_PAGES} page images per "
                f"call; this asks for {len(numbers)} pages.",
                "Ask for up to three pages at a time, e.g. pages=[1, 2, 3], and save each "
                "transcript with save_transcript.",
            )
        return await _lazy_pages(ctx, account, doc, pages, numbers, args.force, reason)
    jobs = await asyncio.to_thread(_queue_pages, svc, account, doc, pages, numbers, args.force)
    ctx.audit["count"] = sum(1 for j in jobs if "job" in j)
    return json_result(
        {
            "document": doc.uuid,
            "mode": mode,
            "reason": reason,
            "generation": pages.version.generation,
            "jobs": jobs,
            "hint": "read_page returns each page's transcript once its job is done; "
            "ocr_status shows the queue.",
        },
        cap=RESULT_CAP_BYTES,
    )


# ---- ocr_status --------------------------------------------------------------------------------


class OcrStatusArgs(ToolArgs):
    account: str | None = Field(
        default=None, pattern=ACCT_PATTERN, description="Account id; the linked one by default."
    )


def _key_note(refusal: KeyRefusal) -> str:
    whose = "your own Claude API key" if refusal.mode == "user" else "this server's Claude API key"
    fix = (
        "Store a working key with set_claude_api_key, or remove it with clear_claude_api_key."
        if refusal.mode == "user"
        else "Tell the operator."
    )
    return (
        f"The Claude API refused {whose} ({refusal.reason}); background transcription with "
        f"it is paused and is tried again after {iso_utc(refusal.at + KEY_REFUSED_HOLD)}. {fix}"
    )


def _ocr_status(svc: Services, account: Account) -> dict[str, Any]:
    """F's ``ocr_summary`` (the same spend and cap numbers as ``account_status.ocr``) plus the
    account's queue, auto-OCR folders, latest failure reasons and refused-key note."""
    queue = ocr_queue(svc, account)
    errors = [
        {"kind": kind, "reason": _reason(error), "at": iso_utc(at)}
        for kind, error, at in svc.queue.recent_errors(
            account.uid, acct=account.acct, kinds=OCR_KINDS, limit=RECENT_ERRORS * 4
        )
        if error != PENDING_REASON
    ][:RECENT_ERRORS]
    summary = ocr_summary(svc, account.uid)
    if summary.get("mode") in SERVER_MODES:
        summary["note"] = OCR_SERVER_NOTE
    auto = load_auto_ocr(account.index)
    refusal = load_key_refusal(account.index)
    key_refused: dict[str, object] | None = None
    if refusal is not None:
        applies = refusal.applies(str(summary.get("mode")), svc.clock())
        key_refused = {
            "mode": refusal.mode,
            "reason": refusal.reason,
            "since": iso_utc(refusal.at),
            "paused": applies,
        }
        if applies:
            summary["note"] = _key_note(refusal)
    return {
        "account": account.acct,
        **summary,
        "auto_ocr": {"enabled": auto.enabled, "folders": list(auto.folders)},
        "queue": queue,
        "dead": sum(q["dead"] for q in queue.values()),
        "recent_errors": errors,
        "key_refused": key_refused,
    }


async def ocr_status(ctx: ToolContext, args: OcrStatusArgs) -> CallToolResult:
    """Show background transcription for your account: the OCR mode and why (your own API
    key, the server key, or lazy mode), this month's spend and what remains under each cap
    that applies (the same numbers as account_status), whether auto-OCR is on and for which
    folders, the queue of transcription jobs by state, the reasons of the latest failures,
    and whether the Claude API refused the key in use."""
    account = await use_account(ctx, args.account)
    payload = await asyncio.to_thread(_ocr_status, services(ctx), account)
    return json_result(payload, cap=RESULT_CAP_BYTES)


def register(registry: ToolRegistry) -> None:
    """Add the OCR tools."""
    registry.add(
        ToolSpec.from_handler(
            save_transcript,
            args=SaveTranscriptArgs,
            annotations=tool_annotations(read_only=False, idempotent=True),
        )
    )
    registry.add(
        ToolSpec.from_handler(
            transcribe,
            args=TranscribeArgs,
            annotations=tool_annotations(read_only=False, open_world=True),
        )
    )
    registry.add(ToolSpec.from_handler(ocr_status, args=OcrStatusArgs, annotations=READ_ONLY))


__all__ = [
    "LAZY_INSTRUCTION",
    "OLD_VERSION_NOTE",
    "TRANSCRIPT_MAX_BYTES",
    "await_server_transcript",
    "ocr_status",
    "register",
    "save_transcript",
    "transcribe",
]
