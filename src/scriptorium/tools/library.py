"""Library tools (§11.2, §11.5): list, recent, search, read_page, read_document, page_image,
versions, trash.

Everything here reads the archive; nothing writes to the cloud. Page images that are not on
disk yet are rendered by the worker (see ``_library.page_png``); the server only downscales
PNGs the renderer wrote. Cloud-supplied names are display data: bounded, returned to their
owner, never used in a path, a log line or an audit row.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from mcp_types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from scriptorium.library import pages as pg
from scriptorium.library.index import (
    ChangeRow,
    DocumentHit,
    DocumentRow,
    HistoryRow,
    Page,
    SearchHit,
)
from scriptorium.library.tags import MAX_TAGS_PER_DOCUMENT
from scriptorium.tools import ToolContext, ToolRegistry, ToolSpec
from scriptorium.tools._conventions import (
    READ_ONLY,
    RESULT_CAP_BYTES,
    ErrorCode,
    PageArgs,
    ToolArgs,
    ToolError,
    cap_text,
    decode_cursor,
    encode_cursor,
    invalid_argument,
    iso_utc,
    json_result,
    json_text,
    parse_iso_utc,
)
from scriptorium.tools._library import (
    ACCT_PATTERN,
    DOCUMENT,
    FOLDER,
    IDENTIFIER_MAX,
    Account,
    Pages,
    display_name,
    document_item,
    downscaled,
    load_pages,
    page_png,
    page_ref,
    pick_page,
    safe_filename,
    shrink_to_fit,
    state_of,
    use_account,
    use_document,
)
from scriptorium.tools.ocr import LAZY_INSTRUCTION, OLD_VERSION_NOTE, await_server_transcript

PNG: Final = "image/png"
PAGE_IMAGE_SIDE: Final = 1024
SCALE_STEP: Final = 0.25
"""``page_image(scale=1)``: longest side 1024 px, about 200 KB for a handwritten page (S6)."""
MODEL_IMAGE_SIDE: Final = 2576
"""The Claude API's longest image side; ``read_page`` downscales taller pages to it (§8.3)."""
CHANGED_PAGES_MAX: Final = 50
_META_ROOM: Final = 2048
TAG_ARG_MAX: Final = 256
"""Longest tag name a filter accepts; stored names are cut to 64 characters, and a longer
argument is cut the same way before it is compared."""
SNIPPET_MAX: Final = 512
PATH_MAX: Final = 1024


def _account_field() -> Any:
    return Field(
        default=None,
        pattern=ACCT_PATTERN,
        description="An account id from account_status; the linked account when omitted.",
    )


def _document_field() -> Any:
    return Field(
        min_length=1,
        max_length=IDENTIFIER_MAX,
        description="Document UUID, or its path of names from the root (e.g. Notes/Journal).",
    )


def _page_field() -> Any:
    return Field(ge=1, le=100_000, description="Page number, from 1.")


TagName = Annotated[str, Field(min_length=1, max_length=TAG_ARG_MAX)]


def _tags_field(description: str) -> Any:
    return Field(default=None, max_length=MAX_TAGS_PER_DOCUMENT, description=description)


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _png_block(data: bytes) -> ImageContent:
    return ImageContent(data=base64.b64encode(data).decode("ascii"), mime_type=PNG)


# ---- list --------------------------------------------------------------------------------------


class ListArgs(PageArgs):
    folder: str | None = Field(
        default=None,
        min_length=1,
        max_length=IDENTIFIER_MAX,
        description="Folder UUID or path of names (e.g. Notes/Projects); the root by default.",
    )
    include_trash: bool = Field(
        default=False, description="At the root, also list what is in the trash."
    )
    include_deleted: bool = Field(
        default=False, description="Also list items deleted from the cloud but kept here."
    )
    tags: list[TagName] | None = _tags_field(
        "Only items carrying any of these tags (names as list shows them), on the item itself "
        "or on one of its pages; the listing then covers everything below the folder, not "
        "just its direct children."
    )
    account: str | None = _account_field()


async def list_documents(ctx: ToolContext, args: ListArgs) -> CallToolResult:
    """List the folders and documents in a folder of your reMarkable library (the root by
    default), folders first, then by name. Each item has its name, type, UUID, last-modified
    time, page count, state (live, trashed, or deleted from the cloud but kept here) and
    tags. folder is a UUID or a path of names such as "Notes/Projects". tags keeps only the
    items tagged with any of the given names (on the item or one of its pages), searching
    everything below the folder rather than one level. Use next_cursor to continue a long
    listing."""
    account = await use_account(ctx, args.account)
    parent: str | None = None
    if args.folder is not None:
        parent = (await use_document(ctx, account, args.folder, want=FOLDER)).uuid

    def fetch(limit: int) -> Page[DocumentRow]:
        return account.index.children(
            parent,
            include_trash=args.include_trash,
            include_deleted=args.include_deleted,
            tags=args.tags,
            cursor=args.cursor,
            limit=limit,
        )

    def render(page: Page[DocumentRow]) -> dict[str, object]:
        return {
            "account": account.acct,
            "folder": parent,
            "recursive": bool(args.tags),
            "items": [document_item(doc) for doc in page.items],
            "next_cursor": page.next_cursor,
        }

    payload = await asyncio.to_thread(shrink_to_fit, fetch, render, args.limit)
    items = payload["items"]
    ctx.audit["count"] = len(items) if isinstance(items, list) else 0
    return json_result(payload, cap=RESULT_CAP_BYTES)


# ---- recent ------------------------------------------------------------------------------------


class RecentArgs(PageArgs):
    since: str = Field(
        min_length=1, max_length=64, description="ISO 8601 UTC, e.g. 2026-10-17T09:00:00Z."
    )
    account: str | None = _account_field()


def _change_item(account: Account, row: ChangeRow, since: datetime) -> dict[str, object]:
    item: dict[str, object] = {
        "uuid": row.uuid,
        "name": display_name(row.name),
        "type": "folder" if row.type == FOLDER else "document",
        "modified": iso_utc(row.last_modified),
        "state": state_of(row),
    }
    if row.type == DOCUMENT:
        item["pages"] = row.page_count
        changed = [
            p.idx + 1
            for p in account.index.pages(row.uuid)
            if p.stable_since is not None and p.stable_since >= since
        ]
        item["changed_pages"] = changed[:CHANGED_PAGES_MAX]
        item["changed_page_count"] = len(changed)
    return item


async def recent(ctx: ToolContext, args: RecentArgs) -> CallToolResult:
    """List documents and folders changed since a time, newest first, with the pages of
    each document that changed (1-based page numbers). since is ISO 8601 UTC, e.g.
    2026-10-17T09:00:00Z. Deleted, trashed and excluded (outside the sync scope) items are
    included and marked. Use next_cursor to continue."""
    since = parse_iso_utc(args.since)
    account = await use_account(ctx, args.account)

    def fetch(limit: int) -> tuple[Page[ChangeRow], list[dict[str, object]]]:
        page = account.index.recent(since, args.cursor, limit)
        return page, [_change_item(account, row, since) for row in page.items]

    def render(found: tuple[Page[ChangeRow], list[dict[str, object]]]) -> dict[str, object]:
        page, items = found
        return {
            "account": account.acct,
            "since": iso_utc(since),
            "items": items,
            "next_cursor": page.next_cursor,
        }

    payload = await asyncio.to_thread(shrink_to_fit, fetch, render, args.limit)
    items = payload["items"]
    ctx.audit["count"] = len(items) if isinstance(items, list) else 0
    return json_result(payload, cap=RESULT_CAP_BYTES)


# ---- search ------------------------------------------------------------------------------------


class SearchArgs(PageArgs):
    query: str = Field(
        min_length=1,
        max_length=1000,
        description="Words to find; every word must match. Quotes and operators are literal.",
    )
    folder: str | None = Field(
        default=None,
        min_length=1,
        max_length=IDENTIFIER_MAX,
        description="Only documents anywhere below this folder (UUID or path of names).",
    )
    tags: list[TagName] | None = _tags_field(
        "Only results tagged with any of these names: a page matches on its own tags or its "
        "document's, a document on its own or any of its pages'."
    )
    account: str | None = _account_field()


def _hit_item(hit: SearchHit | DocumentHit) -> dict[str, object]:
    if isinstance(hit, SearchHit):
        return {
            "kind": "page",
            "document": hit.document,
            "name": display_name(hit.doc_name),
            "path": _bounded(hit.doc_path, PATH_MAX),
            "page": None if hit.idx is None else hit.idx + 1,
            "page_id": hit.page_id,
            "content_hash": hit.content_hash,
            "current": hit.current,
            "snippet": _bounded(hit.snippet, SNIPPET_MAX),
            "tags": list(hit.tags),
        }
    return {
        "kind": "document",
        "document": hit.document,
        "name": display_name(hit.doc_name),
        "path": _bounded(hit.doc_path, PATH_MAX),
        "tags": list(hit.tags),
    }


async def search(ctx: ToolContext, args: SearchArgs) -> CallToolResult:
    """Search your reMarkable library: the transcripts of transcribed pages, document names,
    folder paths and tag names, every word of query must match. Page results come first,
    best match first, each with its document, 1-based page number (null if the page is no
    longer in the document), a snippet, its tags, and current (false when the snippet is
    from an older version of the page); read_page fetches the full text. Documents with no
    transcribed page are found by name or tag and follow as document results. folder limits
    the search to everything below a folder; tags keeps results tagged with any of the
    given names. Use next_cursor to continue."""
    account = await use_account(ctx, args.account)
    folder: str | None = None
    if args.folder is not None:
        folder = (await use_document(ctx, account, args.folder, want=FOLDER)).uuid

    def fetch(limit: int) -> Page[SearchHit | DocumentHit]:
        return account.index.search(
            args.query, folder=folder, tags=args.tags, cursor=args.cursor, limit=limit
        )

    def render(page: Page[SearchHit | DocumentHit]) -> dict[str, object]:
        return {
            "account": account.acct,
            "folder": folder,
            "items": [_hit_item(hit) for hit in page.items],
            "next_cursor": page.next_cursor,
        }

    payload = await asyncio.to_thread(shrink_to_fit, fetch, render, args.limit)
    items = payload["items"]
    ctx.audit["count"] = len(items) if isinstance(items, list) else 0
    return json_result(payload, cap=RESULT_CAP_BYTES)


# ---- read_page ---------------------------------------------------------------------------------


class ReadPageArgs(ToolArgs):
    document: str = _document_field()
    page: int = _page_field()
    version: int | None = Field(
        default=None,
        ge=0,
        le=2**62,
        description="A generation from versions(); the current version when omitted.",
    )
    account: str | None = _account_field()


async def read_page(ctx: ToolContext, args: ReadPageArgs) -> CallToolResult:
    """Read one page of a reMarkable document (pages are numbered from 1). Returns the
    page's transcript when there is one. Without one, if the server transcribes with an API
    key it queues the page, waits a few seconds and answers pending with a job id if the
    transcript is not ready yet (call again shortly); otherwise it returns the page image with
    instructions: transcribe it and store the result with save_transcript, using the
    content_hash given. version reads the page as it was at an earlier generation listed by
    versions()."""
    account = await use_account(ctx, args.account)
    doc = await use_document(ctx, account, args.document)
    ctx.audit["page"] = args.page
    pages = await asyncio.to_thread(load_pages, account, doc, args.version)
    source = pick_page(pages, args.page)
    ctx.audit["generation"] = pages.version.generation
    ref = page_ref(doc, pages, source)
    transcript = await asyncio.to_thread(
        account.index.transcript, doc.uuid, source.page_id, source.content_hash
    )
    if transcript is None and pages.current:
        transcript = await await_server_transcript(
            ctx, account, doc, source, pages.version.generation
        )
    if transcript is not None:
        text, truncated = cap_text(transcript.text, RESULT_CAP_BYTES - _META_ROOM)
        meta = {
            **ref,
            "transcript": {
                "model": transcript.model,
                "mode": transcript.mode,
                "created_at": iso_utc(transcript.created_at),
                "truncated": truncated,
            },
        }
        ctx.audit["bytes"] = len(text.encode("utf-8"))
        return CallToolResult(content=[TextContent(text=json_text(meta)), TextContent(text=text)])
    png = await page_png(ctx, account, source, pages.version.generation)
    png = await downscaled(ctx, account, source, png, MODEL_IMAGE_SIDE)
    note = LAZY_INSTRUCTION if pages.current else OLD_VERSION_NOTE
    meta = {**ref, "transcript": None}
    ctx.audit["bytes"] = len(png)
    return CallToolResult(
        content=[TextContent(text=f"{note}\n\n{json_text(meta)}"), _png_block(png)]
    )


# ---- read_document -----------------------------------------------------------------------------


class ReadDocumentArgs(ToolArgs):
    document: str = _document_field()
    format: Literal["markdown", "pdf"] = Field(
        default="markdown", description='"markdown" (transcripts by page); "pdf" comes later.'
    )
    pages: str | None = Field(
        default=None,
        max_length=32,
        pattern=r"^\s*\d+\s*(-\s*\d*\s*)?$",
        description='Page range such as "3", "1-10" or "5-"; every page when omitted.',
    )
    cursor: str | None = Field(
        default=None, max_length=256, description="next_cursor from the previous call."
    )
    account: str | None = _account_field()


def _page_range(spec: str | None, count: int) -> tuple[int, int]:
    """1-based inclusive ``(first, last)`` of ``spec`` within ``count`` pages."""
    if spec is None:
        return 1, count
    first_text, dash, last_text = spec.replace(" ", "").partition("-")
    first = int(first_text)
    last = int(last_text) if last_text else (count if dash else first)
    if first < 1 or last < first:
        raise invalid_argument("A page range is first-last with 1 <= first <= last.")
    if first > count:
        raise invalid_argument(f"The document has {count} pages.")
    return first, min(last, count)


def _markdown(
    account: Account, doc: DocumentRow, pages: Pages, first: int, last: int, start: int
) -> tuple[str, dict[str, object]]:
    budget = RESULT_CAP_BYTES - _META_ROOM
    parts: list[str] = []
    used = 0
    if start == first:
        heading = f"# {display_name(doc.name)}\n\n"
        parts.append(heading)
        used += len(heading.encode("utf-8"))
    transcribed = 0
    missing: list[int] = []
    number = start
    while number <= last:
        source = pages.sources[number - 1]
        row = account.index.transcript(doc.uuid, source.page_id, source.content_hash)
        if row is None:
            body = (
                "_Not transcribed yet: read_page returns its image to transcribe._"
                if pages.current
                else "_Not transcribed._"
            )
        else:
            body = row.text
        section = f"## Page {number}\n\n{body}\n\n"
        size = len(section.encode("utf-8"))
        if used + size > budget:
            if number > start:
                break
            section, _ = cap_text(section, budget - used)
            section += "\n\n_(page truncated)_\n\n"
            size = len(section.encode("utf-8"))
        parts.append(section)
        used += size
        if row is None:
            missing.append(number)
        else:
            transcribed += 1
        number += 1
    returned_last = number - 1
    next_cursor = (
        encode_cursor({"d": doc.uuid, "g": pages.version.generation, "p": number})
        if number <= last
        else None
    )
    meta: dict[str, object] = {
        "document": doc.uuid,
        "generation": pages.version.generation,
        "page_count": len(pages.sources),
        "pages": f"{start}-{returned_last}",
        "transcribed": transcribed,
        "not_transcribed": missing[:100],
        "next_cursor": next_cursor,
    }
    return "".join(parts), meta


async def read_document(ctx: ToolContext, args: ReadDocumentArgs) -> CallToolResult:
    """Read a whole reMarkable document as Markdown: the transcript of each page under a
    "## Page N" heading, with untranscribed pages marked (read_page returns their images).
    pages limits the range, such as "1-10". Long documents come in parts of up to 32 KB;
    pass next_cursor back to continue. format="pdf" is not available yet."""
    if args.format == "pdf":
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            "PDF output is not available in this release.",
            'Use format="markdown", or page_image for a picture of a page.',
        )
    account = await use_account(ctx, args.account)
    doc = await use_document(ctx, account, args.document)
    pages = await asyncio.to_thread(load_pages, account, doc, None)
    ctx.audit["generation"] = pages.version.generation
    if not pages.sources:
        return json_result({"document": doc.uuid, "page_count": 0, "pages": None})
    first, last = _page_range(args.pages, len(pages.sources))
    start = first
    if args.cursor is not None:
        state = decode_cursor(args.cursor)
        if state.get("d") != doc.uuid or state.get("g") != pages.version.generation:
            raise invalid_argument(
                "That cursor belongs to another document or version.",
                "Start again without a cursor.",
            )
        resume = state.get("p")
        if not isinstance(resume, int) or not first <= resume <= last:
            raise invalid_argument("The cursor is not valid.", "Start again without a cursor.")
        start = resume
    text, meta = await asyncio.to_thread(_markdown, account, doc, pages, first, last, start)
    ctx.audit["bytes"] = len(text.encode("utf-8"))
    return CallToolResult(content=[TextContent(text=text), TextContent(text=json_text(meta))])


# ---- page_image --------------------------------------------------------------------------------


class PageImageArgs(ToolArgs):
    document: str = _document_field()
    page: int = _page_field()
    scale: float = Field(
        default=1.0,
        ge=0.25,
        le=2.0,
        description=(
            "1 gives a longest side of 1024 px (about 200 KB); 0.25-2, in steps of 0.25 "
            "(other values are rounded to the nearest step)."
        ),
    )
    account: str | None = _account_field()


def image_side(scale: float) -> int:
    """The longest side for ``scale``, snapped to a step of 0.25 (256 px).

    Every size is a cached file or a worker job per page version, so only the eight steps
    exist: one user cannot fill the disk or the render queue with sizes (§8.3).
    """
    steps = min(max(round(scale / SCALE_STEP), 1), round(2 / SCALE_STEP))
    return round(PAGE_IMAGE_SIDE * SCALE_STEP) * steps


async def page_image(ctx: ToolContext, args: PageImageArgs) -> CallToolResult:
    """Show a page of a reMarkable document as a PNG image (pages are numbered from 1), plus
    a download link for the person, valid for 10 minutes. scale 1 gives a longest side of
    1024 px; up to 2 for more detail. For the text of a page use read_page."""
    account = await use_account(ctx, args.account)
    doc = await use_document(ctx, account, args.document)
    ctx.audit["page"] = args.page
    pages = await asyncio.to_thread(load_pages, account, doc, None)
    source = pick_page(pages, args.page)
    generation = pages.version.generation
    side = image_side(args.scale)
    base = await page_png(ctx, account, source, generation)
    if max(pg.png_size(base)) >= side:
        data = await downscaled(ctx, account, source, base, side)
    else:
        data = await page_png(ctx, account, source, generation, max_side=side)
    width, height = pg.png_size(data)
    filename = safe_filename(f"{doc.name}-p{args.page}", ".png")
    token = await asyncio.to_thread(
        ctx.downloads.mint, data, media_type=PNG, owner=ctx.user.uid_hash, filename=filename
    )
    ctx.audit["bytes"] = len(data)
    link = (
        f"[Download {filename}]({ctx.download_url(token)}) "
        f"({width}x{height} px PNG; the link works for 10 minutes)."
    )
    return CallToolResult(content=[_png_block(data), TextContent(text=link)])


# ---- versions ----------------------------------------------------------------------------------


class VersionsArgs(PageArgs):
    document: str = _document_field()
    account: str | None = _account_field()


def _version_item(doc: DocumentRow, row: HistoryRow) -> dict[str, object]:
    current = (
        row.generation == doc.generation
        if doc.generation is not None
        else row.hash_root == doc.hash_root
    )
    return {
        "generation": row.generation,
        "seen_at": iso_utc(row.seen_at),
        "current": current,
        "readable": row.index_sha256 is not None,
    }


async def versions(ctx: ToolContext, args: VersionsArgs) -> CallToolResult:
    """List the archived versions of a reMarkable document, newest first: each version's
    generation, when this server first saw it, and whether it is the current one. Read an
    old version's page with read_page(document, page, version=generation). Use next_cursor
    to continue."""
    account = await use_account(ctx, args.account)
    doc = await use_document(ctx, account, args.document)
    rows = await asyncio.to_thread(account.index.history, doc.uuid)
    if args.cursor is not None:
        state = decode_cursor(args.cursor)
        before = state.get("g")
        if state.get("d") != doc.uuid or not isinstance(before, int):
            raise invalid_argument("The cursor is not valid.", "Start again without a cursor.")
        rows = [row for row in rows if row.generation < before]
    chosen = rows[: args.limit]
    next_cursor = (
        encode_cursor({"d": doc.uuid, "g": chosen[-1].generation})
        if len(rows) > args.limit
        else None
    )
    ctx.audit["count"] = len(chosen)
    return json_result(
        {
            "document": doc.uuid,
            "name": display_name(doc.name),
            "state": state_of(doc),
            "versions": [_version_item(doc, row) for row in chosen],
            "next_cursor": next_cursor,
        },
        cap=RESULT_CAP_BYTES,
    )


# ---- trash -------------------------------------------------------------------------------------


class TrashArgs(PageArgs):
    account: str | None = _account_field()


async def trash(ctx: ToolContext, args: TrashArgs) -> CallToolResult:
    """List what is in the reMarkable trash and what was deleted from the cloud but is kept
    on this server (tombstoned), with when and why it was deleted (deletion_cause). Nothing
    here is ever removed automatically. Use next_cursor to continue."""
    account = await use_account(ctx, args.account)

    def fetch(limit: int) -> Page[DocumentRow]:
        return account.index.trash(args.cursor, limit)

    def render(page: Page[DocumentRow]) -> dict[str, object]:
        return {
            "account": account.acct,
            "items": [document_item(doc) for doc in page.items],
            "next_cursor": page.next_cursor,
        }

    payload = await asyncio.to_thread(shrink_to_fit, fetch, render, args.limit)
    items = payload["items"]
    ctx.audit["count"] = len(items) if isinstance(items, list) else 0
    return json_result(payload, cap=RESULT_CAP_BYTES)


# ---- registration ------------------------------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    """Add the library tools."""
    registry.add(
        ToolSpec.from_handler(list_documents, args=ListArgs, annotations=READ_ONLY, name="list")
    )
    registry.add(ToolSpec.from_handler(recent, args=RecentArgs, annotations=READ_ONLY))
    registry.add(ToolSpec.from_handler(search, args=SearchArgs, annotations=READ_ONLY))
    registry.add(ToolSpec.from_handler(read_page, args=ReadPageArgs, annotations=READ_ONLY))
    registry.add(ToolSpec.from_handler(read_document, args=ReadDocumentArgs, annotations=READ_ONLY))
    registry.add(ToolSpec.from_handler(page_image, args=PageImageArgs, annotations=READ_ONLY))
    registry.add(ToolSpec.from_handler(versions, args=VersionsArgs, annotations=READ_ONLY))
    registry.add(ToolSpec.from_handler(trash, args=TrashArgs, annotations=READ_ONLY))


__all__ = [
    "list_documents",
    "page_image",
    "read_document",
    "read_page",
    "recent",
    "register",
    "search",
    "trash",
    "versions",
]
