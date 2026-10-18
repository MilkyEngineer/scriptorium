"""Plumbing shared by the account, library and OCR tools (§11.0).

- **The account** is always one of the requesting user's (``Services.uid(ctx.user)``); a tool's
  ``account`` argument only picks among them. Without one, the active account is used, else
  the most recently linked (so an archive stays readable after ``disconnect_remarkable``).
- **Identifiers** are a UUID or a ``/``-separated path of visible names from the root; an
  ambiguous path is ``ambiguous`` with the candidate UUIDs.
- **Pages** come from the version's blobs (``library/pages.py``), so an old version reads
  exactly like the current one, and the server never parses a ``.rm`` itself: a PNG that is
  not on disk yet is a ``render`` (or ``page_image``) job for the worker, which the tool
  waits for up to ``PENDING_WAIT_SECONDS`` before answering ``pending`` with the job id.
- **Audit**: helpers record the account, document and page they resolved in ``ctx.audit``.

Blocking work runs in a thread (``asyncio.to_thread``); handlers stay on the event loop.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from scriptorium.auth.oidc import User
from scriptorium.errors import InvalidArgumentError
from scriptorium.jobs.kinds import RenderSpec, is_render_verdict
from scriptorium.library import pages as pg
from scriptorium.library import render
from scriptorium.library.index import DocumentRow, Index
from scriptorium.tools import ToolContext
from scriptorium.tools._conventions import (
    RESULT_CAP_BYTES,
    ErrorCode,
    ToolError,
    invalid_argument,
    is_uuid,
    iso_utc,
    json_text,
    page_to_index,
)
from scriptorium.tools._services import Services
from scriptorium.users.store import AccountPaths, AccountRow, account_paths

__all__ = [
    "ACCT_PATTERN",
    "CONNECT_URL",
    "DOCUMENT",
    "FOLDER",
    "IDENTIFIER_MAX",
    "Account",
    "Pages",
    "display_name",
    "document_item",
    "downscaled",
    "fit_png",
    "iso_or_none",
    "load_pages",
    "not_linked",
    "open_account",
    "page_png",
    "page_ref",
    "pick_page",
    "resolve",
    "safe_filename",
    "services",
    "shrink_to_fit",
    "state_of",
    "use_account",
    "use_document",
]

DOCUMENT: Final = "DocumentType"
FOLDER: Final = "CollectionType"
TRASH: Final = "trash"
ACCT_PATTERN: Final = r"^[0-9A-HJKMNP-TV-Z]{26}$"
"""An account id (a ULID) as ``account_status`` reports it."""
IDENTIFIER_MAX: Final = 1024
CONNECT_URL: Final = "https://my.remarkable.com/device/browser/connect"
MAX_PNG_BYTES: Final = 64 * 1024 * 1024
MAX_CANDIDATES: Final = 20
_NAME_MAX: Final = 256


def services(ctx: ToolContext) -> Services:
    if ctx.services is None:  # a wiring bug: the server registers these tools with services
        raise RuntimeError("library tools need the server's services")
    return ctx.services


def not_linked() -> ToolError:
    return ToolError(
        ErrorCode.NOT_LINKED,
        "No reMarkable account is linked.",
        f"Get a one-time code from {CONNECT_URL} and call connect_remarkable with it.",
    )


# ---- the account -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Account:
    """One of the requesting user's accounts, with its paths and open index."""

    uid: str
    row: AccountRow
    paths: AccountPaths
    index: Index

    @property
    def acct(self) -> str:
        return self.row.acct


def open_account(svc: Services, user: User, acct: str | None) -> Account:
    """The user's account ``acct``, or by default the active (else the latest) one."""
    uid = svc.uid(user)
    rows = svc.store.accounts(uid)
    row: AccountRow | None
    if acct is not None:
        row = next((r for r in rows if r.acct == acct), None)
        if row is None:
            raise ToolError(
                ErrorCode.NOT_FOUND,
                "You have no account with that id.",
                "account_status lists your accounts.",
            )
    else:
        active = [r for r in rows if r.active]
        row = active[-1] if active else (rows[-1] if rows else None)
        if row is None:
            raise not_linked()
    paths = account_paths(svc.settings.data_dir, uid, row.acct)
    return Account(uid, row, paths, svc.index(uid, row.acct))


async def use_account(ctx: ToolContext, acct: str | None = None) -> Account:
    account = await asyncio.to_thread(open_account, services(ctx), ctx.user, acct)
    ctx.audit["acct"] = account.acct
    return account


# ---- identifiers -----------------------------------------------------------------------------


def resolve(index: Index, identifier: str, *, want: str | None) -> DocumentRow:
    """The document or folder ``identifier`` names (a UUID or a path of visible names)."""
    candidate = identifier.strip().lower()
    if is_uuid(candidate):
        doc = index.document(candidate)
        if doc is None:
            raise ToolError(
                ErrorCode.NOT_FOUND,
                "Nothing in this account has that UUID.",
                "Use list, recent or trash to find it.",
            )
    else:
        trimmed = identifier.strip("/")
        parts = trimmed.split("/")
        if not trimmed or any(not part for part in parts):
            raise invalid_argument(
                "A path is names separated by '/', from the root (e.g. Notes/Projects/Plan).",
                "Or pass the UUID.",
            )
        found = index.resolve_path(parts)
        if not found:
            raise ToolError(
                ErrorCode.NOT_FOUND,
                "Nothing has that path.",
                "Names match exactly, from the root, and trashed items are not on any path; "
                "use list to browse, or pass the UUID.",
            )
        if len(found) > 1:
            raise ToolError(
                ErrorCode.AMBIGUOUS,
                f"{len(found)} items have that path.",
                "Pass one of the candidate UUIDs instead.",
                extra={"candidates": found[:MAX_CANDIDATES]},
            )
        located = index.document(found[0])
        if located is None:  # removed between the two reads
            raise ToolError(ErrorCode.NOT_FOUND, "Nothing has that path.", "Try again.")
        doc = located
    if want == DOCUMENT and doc.type != DOCUMENT:
        raise invalid_argument("That is a folder, not a document.", "Use list to see inside it.")
    if want == FOLDER and doc.type != FOLDER:
        raise invalid_argument("That is a document, not a folder.")
    return doc


async def use_document(
    ctx: ToolContext, account: Account, identifier: str, *, want: str | None = DOCUMENT
) -> DocumentRow:
    doc = await asyncio.to_thread(resolve, account.index, identifier, want=want)
    ctx.audit["uuid"] = doc.uuid
    return doc


# ---- listing rows ----------------------------------------------------------------------------


def state_of(doc: DocumentRow) -> str:
    if doc.deleted_at is not None:
        return "deleted"
    return "trashed" if doc.parent == TRASH else "live"


def display_name(name: str) -> str:
    """A cloud-supplied name as display data: bounded, never interpreted."""
    return name if len(name) <= _NAME_MAX else name[: _NAME_MAX - 1] + "…"


def document_item(doc: DocumentRow) -> dict[str, object]:
    """One row of ``list``/``trash``: name, type, uuid, modified, page count, state."""
    item: dict[str, object] = {
        "uuid": doc.uuid,
        "name": display_name(doc.name),
        "type": "folder" if doc.type == FOLDER else "document",
        "modified": iso_utc(doc.last_modified),
        "state": state_of(doc),
    }
    if doc.type == DOCUMENT:
        item["pages"] = doc.page_count
        if doc.source_kind:
            item["kind"] = doc.source_kind
    if doc.deleted_at is not None:
        item["deleted_at"] = iso_utc(doc.deleted_at)
        item["deletion_cause"] = doc.deletion_cause or "unknown"
    return item


def shrink_to_fit[T](
    fetch: Callable[[int], T], render_payload: Callable[[T], dict[str, object]], limit: int
) -> dict[str, object]:
    """``render_payload(fetch(limit))``, halving ``limit`` until the JSON fits in 32 KB.

    The index makes the cursor for whatever page size was fetched, so a smaller page still
    continues exactly where it stopped.
    """
    while True:
        payload = render_payload(fetch(limit))
        if len(json_text(payload).encode("utf-8")) <= RESULT_CAP_BYTES or limit == 1:
            return payload
        limit = max(1, limit // 2)


# ---- pages -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pages:
    """A version of a document and its pages, in order."""

    version: pg.Version
    sources: list[pg.PageSource]
    current: bool


def load_pages(account: Account, doc: DocumentRow, generation: int | None) -> Pages:
    """The document's current version, or the one recorded at ``generation``."""
    if generation is None:
        version = pg.current_version(account.paths, account.index, doc.uuid)
        if version is None:
            raise ToolError(
                ErrorCode.NOT_FOUND,
                "This document has no archived version yet.",
                "The worker archives it on its next poll; try again in a few minutes.",
            )
    else:
        version = pg.version_at(account.paths, account.index, doc.uuid, generation)
    current = doc.hash_root is not None and version.hash_root == doc.hash_root
    return Pages(version, pg.page_sources(account.paths, version), current)


def pick_page(pages: Pages, page: int) -> pg.PageSource:
    if not pages.sources:
        raise invalid_argument("This version of the document has no pages.")
    return pages.sources[page_to_index(page, len(pages.sources))]


def _read_png(path: Path) -> bytes | None:
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_PNG_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(data) > MAX_PNG_BYTES:
        raise InvalidArgumentError("the page image is too large to return")
    return data


async def page_png(
    ctx: ToolContext,
    account: Account,
    source: pg.PageSource,
    generation: int,
    *,
    max_side: int | None = None,
) -> bytes:
    """The page's PNG (archive scale, or longest side ``max_side``), rendering it if needed.

    A missing PNG becomes a worker job (the server never parses ``.rm`` or PDF data); the
    call waits up to ``PENDING_WAIT_SECONDS`` for it, then answers ``pending`` with the job.
    A dead job whose error is the renderer's verdict on the page is ``refused``; a job that
    finished without leaving the PNG, or died for any other reason (a broken pool, expired
    leases), is re-run under a new key, at most ``RERUN_CAP`` times per page version for
    all time (``JobQueue.rerun``), which also bounds the jobs one call can queue; a call
    that finds re-runs already made follows them. A dead job whose re-runs are spent is
    ``refused`` like a verdict.
    """
    svc = services(ctx)
    rel = pg.png_relpath(source.page_id, source.content_hash, max_side=max_side)
    path = pg.resolve_png(account.paths, rel)
    data = await asyncio.to_thread(_read_png, path)
    if data is not None:
        return data
    spec = RenderSpec(source.document, source.page_id, source.content_hash, generation, max_side)
    job = await asyncio.to_thread(
        svc.queue.enqueue,
        uid=account.uid,
        acct=account.acct,
        kind=spec.kind,
        dedupe_key=spec.key(account.acct),
        payload=spec.payload(),
    )
    ctx.audit["job"] = job.id
    deadline = time.monotonic() + svc.settings.pending_wait_seconds
    while True:
        data = await asyncio.to_thread(_read_png, path)
        if data is not None:
            return data
        if job.state == "dead" and is_render_verdict(job.error):
            raise ToolError(
                ErrorCode.REFUSED,
                "This page could not be rendered.",
                "The page may be damaged or in a format the renderer does not support.",
                extra={"job": job.id},
            )
        if job.state in ("done", "cancelled", "dead"):
            # Terminates: each step is an existing re-run (the family is finite) or a new
            # one (at most RERUN_CAP for all time), and None once the family is spent.
            nxt = await asyncio.to_thread(svc.queue.rerun, job)
            if nxt is None and job.state == "dead":
                raise ToolError(
                    ErrorCode.REFUSED,
                    "This page could not be rendered.",
                    "Rendering keeps failing for this version of the page; it is not retried "
                    "again. Editing the page on the tablet makes a new version.",
                    extra={"job": job.id},
                )
            if nxt is None:
                raise ToolError(
                    ErrorCode.NOT_FOUND,
                    "The page image could not be produced.",
                    "Try again after the next sync.",
                )
            job = nxt
            ctx.audit["job"] = job.id
            continue
        if time.monotonic() >= deadline:
            raise ToolError(
                ErrorCode.PENDING,
                "The page is still being rendered.",
                "Call the tool again in a few seconds.",
                extra={"job": job.id, "state": job.state},
            )
        await svc.sleep(svc.poll_interval_s)
        fresh = await asyncio.to_thread(svc.queue.get, job.id, account.uid)
        job = job if fresh is None else fresh


def fit_png(data: bytes, max_side: int) -> bytes:
    """``data`` with its longest side at most ``max_side`` (Lanczos, deterministic).

    Refuses (``refused``) a PNG larger than the renderer's canvas budget before decoding
    it: the worker never writes one, so it is not ours to decode in the server process.
    """
    width, height = pg.png_size(data)
    if max(width, height) <= max_side:
        return data
    if width * height > render.MAX_CANVAS_PIXELS:
        raise ToolError(
            ErrorCode.REFUSED,
            "This page image is too large to scale on the server.",
            "Use read_page for its text instead.",
        )
    result = render.RenderResult(data, width, height, (0.0, 0.0, 0.0, 0.0))
    return render.page_image_bytes(result, max_side_px=max_side)


def _cached_variant(account: Account, source: pg.PageSource, base: bytes, side: int) -> bytes:
    rel = pg.png_relpath(source.page_id, source.content_hash, max_side=side)
    path = pg.resolve_png(account.paths, rel)
    data = _read_png(path)
    if data is not None:
        return data
    data = fit_png(base, side)
    pg.write_png(account.paths, rel, data)
    return data


async def downscaled(
    ctx: ToolContext, account: Account, source: pg.PageSource, base: bytes, side: int
) -> bytes:
    """``base`` (the page's archive PNG) with its longest side at most ``side``.

    A smaller copy is made once per page version and size and cached next to the archive PNG
    (``<hash>.<side>px.png``); callers pass a size from a short fixed list, so the cache stays
    bounded. Decoding runs in a thread, at most ``IMAGE_SLOTS`` at a time per process.
    """
    if max(pg.png_size(base)) <= side:
        return base
    async with services(ctx).image_slots:
        return await asyncio.to_thread(_cached_variant, account, source, base, side)


def page_ref(doc: DocumentRow, pages: Pages, source: pg.PageSource) -> dict[str, object]:
    """The identifiers a client needs to come back to this page (``save_transcript``)."""
    return {
        "document": doc.uuid,
        "page": source.idx + 1,
        "page_count": len(pages.sources),
        "page_id": source.page_id,
        "content_hash": source.content_hash,
        "generation": pages.version.generation,
        "current": pages.current,
    }


def iso_or_none(moment: datetime | None) -> str | None:
    return None if moment is None else iso_utc(moment)


_SAFE: Final = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(stem: str, suffix: str) -> str:
    """A download filename from display data: ``[A-Za-z0-9._-]`` only, never empty."""
    clean = _SAFE.sub("_", stem).strip("._")[:80] or "page"
    return f"{clean}{suffix}"
