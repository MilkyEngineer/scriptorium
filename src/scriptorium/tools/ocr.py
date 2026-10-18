"""OCR tools (§11.3, M1 subset): save_transcript, and the lazy-mode instruction (§8.2).

In lazy mode the user's own Claude session transcribes: ``read_page`` returns the page image
with :data:`LAZY_INSTRUCTION`, and ``save_transcript`` stores the result keyed by
``(page uuid, content_hash)``. A transcript is accepted only for the page's *current* hash
(``stale_hash`` otherwise), so a page edited since it was read is never mislabelled.
Transcripts are note content: they are stored, returned to their owner, and never logged
or audited (the audit row carries only their byte count).
"""

from __future__ import annotations

import asyncio
from typing import Final

from mcp_types import CallToolResult
from pydantic import Field

from scriptorium.library.index import DocumentRow
from scriptorium.tools import ToolContext, ToolRegistry, ToolSpec
from scriptorium.tools._conventions import (
    RESULT_CAP_BYTES,
    ErrorCode,
    ToolArgs,
    ToolError,
    invalid_argument,
    json_result,
    tool_annotations,
)
from scriptorium.tools._library import (
    ACCT_PATTERN,
    IDENTIFIER_MAX,
    Account,
    load_pages,
    pick_page,
    use_account,
    use_document,
)

TRANSCRIPT_MAX_BYTES: Final = 30 * 1024
"""Largest transcript accepted: one page of handwriting is a few KB, and ``read_page`` must
return it whole within the 32 KB result cap (§11.0)."""
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
    row = index.page_by_id(source.page_id)
    if row is None or (row.document == doc.uuid and row.content_hash != source.content_hash):
        raise ToolError(
            ErrorCode.NOT_FOUND,
            "This page is not indexed yet.",
            "The worker indexes new pages right after each sync; try again in a minute.",
        )
    if row.content_hash != source.content_hash:
        # Another live document lists the same page id (open question 8): a transcript is
        # keyed by (page id, hash), so only an identical page can share one.
        raise ToolError(
            ErrorCode.REFUSED,
            "This page shares its id with a page of another document.",
            "Its transcript cannot be saved in this release.",
        )
    index.put_transcript(source.page_id, source.content_hash, args.text, args.model, LAZY_MODE)
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


def register(registry: ToolRegistry) -> None:
    """Add the M1 OCR tool."""
    registry.add(
        ToolSpec.from_handler(
            save_transcript,
            args=SaveTranscriptArgs,
            annotations=tool_annotations(read_only=False, idempotent=True),
        )
    )


__all__ = ["LAZY_INSTRUCTION", "OLD_VERSION_NOTE", "register", "save_transcript"]
