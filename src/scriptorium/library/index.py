"""The per-account index, ``index.sqlite`` (Architecture §6.1; M1 plan §3.4).

The index is the queryable view of one account's archive: the library tree and its tombstones,
version history, pages and their render cache, transcripts with full-text search, and the
sync loop's key/value state. It holds no file content, only names, hashes and timestamps, plus
transcripts (which are note content: nothing here logs them).

Conventions:

- ``DocumentRow.parent`` is ``None`` for the root, ``"trash"`` for the top of the trash, or
  the parent folder's UUID. ``""`` (the cloud's spelling of the root) is stored as ``None``.
- Page indices are 0-based; tools convert (§11.0).
- Every timestamp in and out is a timezone-aware ``datetime``; naive values are refused.
- Cursors are opaque URL-safe base64 of the last returned row's sort key, never offsets, so a
  page boundary does not move when rows are inserted before it.
- Each write method is one short ``BEGIN IMMEDIATE`` transaction; inside
  :meth:`Index.transaction` they join the caller's transaction instead (§7 step 3 commits
  history, pages and the document row atomically).

The connection is shared by the threads of the caller's pool; a re-entrant lock serialises its
use, and :meth:`Index.transaction` holds it for the whole block.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from scriptorium.db.connect import (
    DatabaseSetupError,
    from_db_time,
    open_sqlite,
    opt_from_db_time,
    to_db_time,
    utc_now,
)
from scriptorium.db.migrations import SchemaMismatch, check_version, current_version
from scriptorium.db.migrations import migrate as run_migrations
from scriptorium.db.schema_account import ACCOUNT_STEPS
from scriptorium.errors import InvalidArgumentError, NotFoundError

DOCUMENT_TYPES: Final = frozenset({"DocumentType", "CollectionType"})
DELETION_CAUSES: Final = frozenset({"user", "aged", "unknown"})
TRANSCRIPT_MODES: Final = frozenset({"user", "server", "lazy"})
TRASH: Final = "trash"
MAX_LIMIT: Final = 1000
MAX_SEARCH_TERMS: Final = 64
"""Whitespace-separated terms a search may have (each is one clause of the name fallback)."""
MAX_PATH_DEPTH: Final = 128
"""Parent chains come from cloud metadata; walks stop here (and on cycles)."""
_RESOLVE_ANY: Final = (
    "SELECT uuid FROM documents WHERE parent IS ? AND name = ? AND deleted_at IS NULL ORDER BY uuid"
)
_RESOLVE_FOLDER: Final = (
    "SELECT uuid FROM documents WHERE parent IS ? AND name = ? AND deleted_at IS NULL "
    "AND type = 'CollectionType' ORDER BY uuid"
)


# ---- rows -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocumentRow:
    uuid: str
    type: str  # "DocumentType" | "CollectionType"
    parent: str | None
    name: str
    last_modified: datetime
    last_opened: datetime | None = None
    deleted_at: datetime | None = None
    deletion_cause: str | None = None  # "user" | "aged" | "unknown" when deleted
    source_kind: str | None = None  # e.g. "notebook", "pdf", "epub"
    page_count: int = 0
    hash_root: str | None = None
    generation: int | None = None


@dataclass(frozen=True, slots=True)
class ChangeRow:
    """A document or folder whose ``last_modified`` is at or after ``recent(since=…)``."""

    uuid: str
    type: str
    parent: str | None
    name: str
    last_modified: datetime
    page_count: int
    generation: int | None
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class HistoryRow:
    uuid: str
    generation: int
    hash_root: str
    seen_at: datetime
    index_sha256: str | None = None  # blob holding this version's file list (§6)


@dataclass(frozen=True, slots=True)
class PageRow:
    page_id: str  # the page UUID from the document's .content
    document: str
    idx: int  # 0-based
    content_hash: str
    stable_since: datetime | None = None
    rendered_at: datetime | None = None
    png_path: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptRow:
    page_id: str
    content_hash: str
    document: str
    text: str
    model: str
    mode: str  # "user" | "server" | "lazy"
    created_at: datetime
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A transcript matching a search, as a page reference.

    ``idx`` is the page's current 0-based index, or ``None`` if the page no longer exists;
    ``current`` is false when the transcript is of an older version of the page.
    """

    page_id: str
    document: str
    idx: int | None
    content_hash: str
    current: bool
    doc_name: str
    doc_path: str
    snippet: str


@dataclass(frozen=True, slots=True)
class DocumentHit:
    """A document with no transcript whose name contains every search term (§11.2).

    Name hits follow the transcript hits: only transcribed pages are in the full-text index,
    so this is how an untranscribed document is found at all.
    """

    document: str
    doc_name: str
    doc_path: str


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None


# ---- cursors --------------------------------------------------------------------------------


def _encode_cursor(kind: str, key: Sequence[str | int | float]) -> str:
    raw = json.dumps([kind, *key], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str, kind: str, types: Sequence[type]) -> list[Any]:
    """Decode a cursor made by :func:`_encode_cursor` for ``kind``; else ``invalid_argument``."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
    except (binascii.Error, ValueError):
        value = None
    if (
        isinstance(value, list)
        and len(value) == len(types) + 1
        and value[0] == kind
        and all(
            isinstance(v, t) and not isinstance(v, bool)
            for v, t in zip(value[1:], types, strict=True)
        )
    ):
        return value[1:]
    raise InvalidArgumentError("The cursor is not valid.", hint="Start again without a cursor.")


def _check_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidArgumentError(f"limit must be between 1 and {MAX_LIMIT}.")


def _search_terms(query: str) -> list[str]:
    terms = query.split()
    if not terms:
        raise InvalidArgumentError("The search query is empty.")
    if len(terms) > MAX_SEARCH_TERMS:
        raise InvalidArgumentError(f"A search takes at most {MAX_SEARCH_TERMS} words.")
    return terms


def _fts_query(terms: Sequence[str]) -> str:
    """Every term as a quoted FTS5 string: no operators, no syntax errors."""
    return " ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _fold(value: object) -> str | None:
    """Case-insensitive form for the name fallback (``str.casefold``: Unicode, not just ASCII).

    Registered on the connection as the SQL function ``scriptorium_fold``.
    """
    return value.casefold() if isinstance(value, str) else None


def _norm_parent(parent: str | None) -> str | None:
    return parent or None


def _opt_time(moment: datetime | None) -> str | None:
    return None if moment is None else to_db_time(moment)


def _document(row: sqlite3.Row) -> DocumentRow:
    return DocumentRow(
        uuid=row["uuid"],
        type=row["type"],
        parent=row["parent"],
        name=row["name"],
        last_modified=from_db_time(row["last_modified"]),
        last_opened=opt_from_db_time(row["last_opened"]),
        deleted_at=opt_from_db_time(row["deleted_at"]),
        deletion_cause=row["deletion_cause"],
        source_kind=row["source_kind"],
        page_count=row["page_count"],
        hash_root=row["hash_root"],
        generation=row["generation"],
    )


def _page(row: sqlite3.Row) -> PageRow:
    return PageRow(
        page_id=row["uuid"],
        document=row["document"],
        idx=row["idx"],
        content_hash=row["content_hash"],
        stable_since=opt_from_db_time(row["stable_since"]),
        rendered_at=opt_from_db_time(row["rendered_at"]),
        png_path=row["png_path"],
    )


# ---- the index ------------------------------------------------------------------------------


class Index:
    """One account's ``index.sqlite``. Open with :meth:`open`; close with :meth:`close`."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Callable[[], datetime] = utc_now):
        self._conn = conn
        self._clock = clock
        conn.create_function("scriptorium_fold", 1, _fold, deterministic=True)
        self._lock = threading.RLock()
        self._depth = 0

    @classmethod
    def open(cls, path: Path, *, migrate: bool, clock: Callable[[], datetime] = utc_now) -> Index:
        """Open ``path`` with the §6.1 pragmas.

        ``migrate=True`` (the server) applies :data:`ACCOUNT_STEPS`, creating the file if needed.
        ``migrate=False`` (the worker) raises :class:`SchemaMismatch` unless the file exists at
        exactly the code's version, and never creates it.
        """
        if not migrate and not path.is_file():
            raise SchemaMismatch(0, len(ACCOUNT_STEPS))
        conn = open_sqlite(path)
        try:
            if migrate:
                _migrate_account(conn)
            else:
                check_version(conn, ACCOUNT_STEPS)
        except BaseException:
            conn.close()
            raise
        return cls(conn, clock=clock)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def version(self) -> int:
        with self._lock:
            return current_version(self._conn)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """``BEGIN IMMEDIATE`` … ``COMMIT``; rollback if the block raises.

        Nested use (including every write method called inside the block) joins the outermost
        transaction; only the outermost block commits. Keep the block short: it holds the
        database write lock and this index's thread lock.
        """
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0

    # ---- documents ----------------------------------------------------------------------

    def upsert_document(self, doc: DocumentRow) -> None:
        """Insert or update a document's metadata by UUID.

        ``deleted_at``/``deletion_cause`` are taken from ``doc`` only when the row is new; on an
        update they are left alone (:meth:`mark_deleted` and :meth:`unmark_deleted` own them).
        A rename or move refreshes the name and path indexed with the transcripts of the
        document and of everything below it.
        """
        if doc.type not in DOCUMENT_TYPES:
            raise InvalidArgumentError("Unknown document type.")
        if (doc.deleted_at is None) != (doc.deletion_cause is None):
            raise InvalidArgumentError("deleted_at and deletion_cause go together.")
        if doc.deletion_cause is not None and doc.deletion_cause not in DELETION_CAUSES:
            raise InvalidArgumentError("Unknown deletion cause.")
        if doc.page_count < 0:
            raise InvalidArgumentError("page_count is negative.")
        parent = _norm_parent(doc.parent)
        with self.transaction():
            before = self._conn.execute(
                "SELECT name, parent FROM documents WHERE uuid = ?", (doc.uuid,)
            ).fetchone()
            self._conn.execute(
                "INSERT INTO documents(uuid, type, parent, name, last_modified, last_opened, "
                "deleted_at, deletion_cause, source_kind, page_count, hash_root, generation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(uuid) DO UPDATE SET type = excluded.type, "
                "parent = excluded.parent, name = excluded.name, "
                "last_modified = excluded.last_modified, last_opened = excluded.last_opened, "
                "source_kind = excluded.source_kind, page_count = excluded.page_count, "
                "hash_root = excluded.hash_root, generation = excluded.generation",
                (
                    doc.uuid,
                    doc.type,
                    parent,
                    doc.name,
                    to_db_time(doc.last_modified),
                    _opt_time(doc.last_opened),
                    _opt_time(doc.deleted_at),
                    doc.deletion_cause,
                    doc.source_kind,
                    doc.page_count,
                    doc.hash_root,
                    doc.generation,
                ),
            )
            if before is None or before["name"] != doc.name or before["parent"] != parent:
                self._refresh_transcript_paths(doc.uuid)

    def mark_deleted(self, uuid: str, cause: str, at: datetime) -> None:
        """Record a tombstone (§7 step 4). The files are moved by the archive, not here."""
        if cause not in DELETION_CAUSES:
            raise InvalidArgumentError("Unknown deletion cause.")
        with self.transaction():
            cur = self._conn.execute(
                "UPDATE documents SET deleted_at = ?, deletion_cause = ? WHERE uuid = ?",
                (to_db_time(at), cause, uuid),
            )
            if cur.rowcount == 0:
                raise NotFoundError("Document not found.")

    def unmark_deleted(self, uuid: str) -> None:
        with self.transaction():
            cur = self._conn.execute(
                "UPDATE documents SET deleted_at = NULL, deletion_cause = NULL WHERE uuid = ?",
                (uuid,),
            )
            if cur.rowcount == 0:
                raise NotFoundError("Document not found.")

    def document(self, uuid: str) -> DocumentRow | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM documents WHERE uuid = ?", (uuid,)).fetchone()
        return None if row is None else _document(row)

    def document_uuids(self, *, deleted: bool) -> frozenset[str]:
        """Every document and folder UUID: tombstoned ones with ``deleted=True``, the rest
        (the trash included) with ``deleted=False``. These are the tombstone planner's
        ``library`` and ``tombstoned`` sets (§7 step 4)."""
        sql = (
            "SELECT uuid FROM documents WHERE deleted_at IS NOT NULL"
            if deleted
            else "SELECT uuid FROM documents WHERE deleted_at IS NULL"
        )
        with self._lock:
            return frozenset(row["uuid"] for row in self._conn.execute(sql))

    def children(
        self,
        parent: str | None,
        *,
        include_trash: bool,
        include_deleted: bool,
        cursor: str | None,
        limit: int,
    ) -> Page[DocumentRow]:
        """Children of ``parent`` (``None`` = root), sorted folders first, then by name.

        Sort key ``(type, name case-insensitively, uuid)``. At the root, ``include_trash`` also
        lists the top of the trash; pass ``parent="trash"`` to list only that. Tombstoned rows
        appear only with ``include_deleted``.
        """
        _check_limit(limit)
        parent = _norm_parent(parent)
        where: list[str] = []
        params: list[Any] = []
        if parent is None and include_trash:
            where.append("(parent IS NULL OR parent = ?)")
            params.append(TRASH)
        else:
            where.append("parent IS ?")
            params.append(parent)
        if not include_deleted:
            where.append("deleted_at IS NULL")
        if cursor is not None:
            key = _decode_cursor(cursor, "children", (str, str, str))
            where.append("(type, name COLLATE NOCASE, uuid) > (?, ?, ?)")
            params.extend(key)
        sql = (
            f"SELECT * FROM documents WHERE {' AND '.join(where)} "  # noqa: S608 - fixed clauses
            "ORDER BY type, name COLLATE NOCASE, uuid LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (*params, limit + 1)).fetchall()
        items = [_document(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = _encode_cursor("children", (last.type, last.name, last.uuid))
        return Page(items, next_cursor)

    def trash(self, cursor: str | None, limit: int) -> Page[DocumentRow]:
        """Trashed (``parent == "trash"``) and tombstoned documents, sorted as :meth:`children`."""
        _check_limit(limit)
        where = "(parent = ? OR deleted_at IS NOT NULL)"
        params: list[Any] = [TRASH]
        if cursor is not None:
            where += " AND (type, name COLLATE NOCASE, uuid) > (?, ?, ?)"
            params.extend(_decode_cursor(cursor, "trash", (str, str, str)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM documents WHERE {where} "  # noqa: S608 - fixed clauses
                "ORDER BY type, name COLLATE NOCASE, uuid LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        items = [_document(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = _encode_cursor("trash", (last.type, last.name, last.uuid))
        return Page(items, next_cursor)

    def resolve_path(self, parts: Sequence[str]) -> list[str]:
        """UUIDs reached by walking visible names from the root; ``len > 1`` means ambiguous.

        Visible: not tombstoned and not in the trash. Every part but the last must name a
        folder; the last may name a folder or a document. Names match exactly. Duplicate names
        at any level fan out, so all candidates are returned (sorted). No match: ``[]``.
        """
        if not parts:
            return []
        current: list[str | None] = [None]
        with self._lock:
            for i, part in enumerate(parts):
                folder_only = i < len(parts) - 1
                found: list[str | None] = []
                for parent in current:
                    rows = self._conn.execute(
                        _RESOLVE_FOLDER if folder_only else _RESOLVE_ANY, (parent, part)
                    ).fetchall()
                    found.extend(r["uuid"] for r in rows)
                if not found:
                    return []
                current = found
        return sorted(u for u in current if u is not None)

    def recent(self, since: datetime, cursor: str | None, limit: int) -> Page[ChangeRow]:
        """Documents and folders with ``last_modified >= since``, newest first, then by UUID.

        Tombstoned and trashed rows are included (``deleted_at``, ``parent`` say which).
        """
        _check_limit(limit)
        where = "last_modified >= ?"
        params: list[Any] = [to_db_time(since)]
        if cursor is not None:
            modified, uuid = _decode_cursor(cursor, "recent", (str, str))
            where += " AND (last_modified < ? OR (last_modified = ? AND uuid > ?))"
            params.extend((modified, modified, uuid))
        with self._lock:
            rows = self._conn.execute(
                "SELECT uuid, type, parent, name, last_modified, page_count, generation, "  # noqa: S608
                f"deleted_at FROM documents WHERE {where} "  # fixed clauses, values bound
                "ORDER BY last_modified DESC, uuid LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        items = [
            ChangeRow(
                uuid=r["uuid"],
                type=r["type"],
                parent=r["parent"],
                name=r["name"],
                last_modified=from_db_time(r["last_modified"]),
                page_count=r["page_count"],
                generation=r["generation"],
                deleted_at=opt_from_db_time(r["deleted_at"]),
            )
            for r in rows[:limit]
        ]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = _encode_cursor("recent", (last["last_modified"], last["uuid"]))
        return Page(items, next_cursor)

    # ---- history and pages ----------------------------------------------------------------

    def append_history(
        self,
        uuid: str,
        generation: int,
        hash_root: str,
        seen_at: datetime,
        *,
        index_sha256: str | None = None,
    ) -> None:
        """Record that root ``generation`` holds ``hash_root`` for ``uuid``. Idempotent: a
        second call for the same ``(uuid, generation)`` keeps the first row.

        ``index_sha256`` names the blob holding this version's document index (its file
        list), which is what makes the version recoverable from ``blobs/`` (§6).
        """
        with self.transaction():
            self._conn.execute(
                "INSERT INTO history(uuid, generation, hash_root, seen_at, index_sha256) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(uuid, generation) DO NOTHING",
                (uuid, generation, hash_root, to_db_time(seen_at), index_sha256),
            )

    def history(self, uuid: str) -> list[HistoryRow]:
        """Every recorded version of ``uuid``, newest generation first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM history WHERE uuid = ? ORDER BY generation DESC", (uuid,)
            ).fetchall()
        return [
            HistoryRow(
                r["uuid"],
                r["generation"],
                r["hash_root"],
                from_db_time(r["seen_at"]),
                r["index_sha256"],
            )
            for r in rows
        ]

    def replace_pages(self, uuid: str, pages: Sequence[PageRow]) -> None:
        """Make ``pages`` the document's page list.

        Pages are matched by ``page_id``. A page whose ``content_hash`` is unchanged keeps its
        stored ``stable_since``, ``rendered_at`` and ``png_path`` (only its index may move). A
        new or changed page takes ``stable_since`` from the argument and ``rendered_at = NULL``
        (it needs rendering); a changed page keeps its old ``png_path`` as the last good image
        unless the argument gives one. Pages no longer listed are dropped from the current view
        (their transcripts and the archive's files are untouched).
        """
        ids = [p.page_id for p in pages]
        if len(set(ids)) != len(ids):
            raise InvalidArgumentError("Duplicate page id.")
        if len({p.idx for p in pages}) != len(pages) or any(p.idx < 0 for p in pages):
            raise InvalidArgumentError("Page indices must be distinct and non-negative.")
        if any(p.document != uuid for p in pages):
            raise InvalidArgumentError("A page belongs to another document.")
        keep = set(ids)
        with self.transaction():
            existing = {
                r["uuid"]: r
                for r in self._conn.execute(
                    "SELECT uuid, content_hash FROM pages WHERE document = ?", (uuid,)
                )
            }
            self._conn.executemany(
                "DELETE FROM pages WHERE uuid = ?",
                [(pid,) for pid in existing if pid not in keep],
            )
            # Park current indices out of the way so moves cannot hit UNIQUE(document, idx).
            self._conn.execute("UPDATE pages SET idx = -1 - idx WHERE document = ?", (uuid,))
            for p in pages:
                old = existing.get(p.page_id)
                if old is not None and old["content_hash"] == p.content_hash:
                    self._conn.execute(
                        "UPDATE pages SET idx = ? WHERE uuid = ?", (p.idx, p.page_id)
                    )
                    continue
                self._conn.execute(
                    "INSERT INTO pages(uuid, document, idx, content_hash, stable_since, "
                    "rendered_at, png_path) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(uuid) DO UPDATE SET document = excluded.document, "
                    "idx = excluded.idx, content_hash = excluded.content_hash, "
                    "stable_since = excluded.stable_since, rendered_at = NULL, "
                    "png_path = coalesce(excluded.png_path, pages.png_path)",
                    (
                        p.page_id,
                        uuid,
                        p.idx,
                        p.content_hash,
                        _opt_time(p.stable_since),
                        _opt_time(p.rendered_at),
                        p.png_path,
                    ),
                )

    def page(self, uuid: str, idx: int) -> PageRow | None:
        """The page at 0-based ``idx`` of document ``uuid``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pages WHERE document = ? AND idx = ?", (uuid, idx)
            ).fetchone()
        return None if row is None else _page(row)

    def pages(self, uuid: str) -> list[PageRow]:
        """Every current page of document ``uuid``, in order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pages WHERE document = ? ORDER BY idx", (uuid,)
            ).fetchall()
        return [_page(r) for r in rows]

    def set_rendered(self, page_id: str, png_path: str, at: datetime) -> None:
        with self.transaction():
            cur = self._conn.execute(
                "UPDATE pages SET rendered_at = ?, png_path = ? WHERE uuid = ?",
                (to_db_time(at), png_path, page_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("Page not found.")

    # ---- transcripts --------------------------------------------------------------------

    def put_transcript(
        self, page_id: str, content_hash: str, text: str, model: str, mode: str
    ) -> None:
        """Store (or replace) the transcript of one version of a page.

        The page must be in the index (``not_found`` otherwise). Whether ``content_hash`` is the
        page's *current* hash is the caller's check (``save_transcript`` answers ``stale_hash``);
        a transcript of an older version is still valid for that version.
        """
        if mode not in TRANSCRIPT_MODES:
            raise InvalidArgumentError("Unknown transcript mode.")
        with self.transaction():
            row = self._conn.execute(
                "SELECT document FROM pages WHERE uuid = ?", (page_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("Page not found.")
            document = row["document"]
            name, path = self._name_and_path(document)
            self._conn.execute(
                "INSERT INTO transcripts(page, content_hash, document, text, model, mode, "
                "created_at, doc_name, doc_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(page, content_hash) DO UPDATE SET document = excluded.document, "
                "text = excluded.text, model = excluded.model, mode = excluded.mode, "
                "created_at = excluded.created_at, doc_name = excluded.doc_name, "
                "doc_path = excluded.doc_path, input_tokens = NULL, output_tokens = NULL, "
                "cost_usd = NULL",
                (
                    page_id,
                    content_hash,
                    document,
                    text,
                    model,
                    mode,
                    to_db_time(self._clock()),
                    name,
                    path,
                ),
            )

    def transcript(self, page_id: str, content_hash: str) -> TranscriptRow | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM transcripts WHERE page = ? AND content_hash = ?",
                (page_id, content_hash),
            ).fetchone()
        if r is None:
            return None
        return TranscriptRow(
            page_id=r["page"],
            content_hash=r["content_hash"],
            document=r["document"],
            text=r["text"],
            model=r["model"],
            mode=r["mode"],
            created_at=from_db_time(r["created_at"]),
            input_tokens=r["input_tokens"],
            output_tokens=r["output_tokens"],
            cost_usd=r["cost_usd"],
        )

    def search(
        self, query: str, *, cursor: str | None, limit: int
    ) -> Page[SearchHit | DocumentHit]:
        """Full-text search over transcripts, document names and folder paths, then names.

        Every whitespace-separated term must match (FTS5 operators are not interpreted). The
        transcript hits come first, sorted by bm25 rank, then transcript id; the cursor carries
        the last ``(rank, id)``, so a page boundary is exact for an unchanged index (ranks
        shift when transcripts are added). After them come :class:`DocumentHit` rows: live
        documents with no transcript at all whose name contains every term, compared
        case-insensitively (§11.2), sorted by name and UUID.
        """
        _check_limit(limit)
        terms = _search_terms(query)
        name_key: list[Any] | None = None
        fts_key: list[Any] | None = None
        if cursor is not None:
            try:
                fts_key = _decode_cursor(cursor, "search", (float, int))
            except InvalidArgumentError:
                name_key = _decode_cursor(cursor, "search-name", (str, str))
        items: list[SearchHit | DocumentHit] = []
        if name_key is None:
            rows = self._search_fts(terms, fts_key, limit + 1)
            items.extend(_search_hit(r) for r in rows[:limit])
            if len(rows) > limit:
                last = rows[limit - 1]
                key = (float(last["rank"]), int(last["id"]))
                return Page(items, _encode_cursor("search", key))
        room = limit - len(items)
        names = self._search_names(terms, name_key, room + 1)
        hits = names[:room]
        with self._lock:
            for uuid, name in hits:
                _, path = self._name_and_path(uuid)
                items.append(DocumentHit(document=uuid, doc_name=name, doc_path=path))
        next_cursor = None
        if len(names) > room:
            # The name phase's sort key is (name, uuid); "" sorts before every name.
            after = (hits[-1][1], hits[-1][0]) if hits else ("", "")
            next_cursor = _encode_cursor("search-name", after)
        return Page(items, next_cursor)

    def _search_fts(
        self, terms: Sequence[str], after: Sequence[Any] | None, limit: int
    ) -> list[sqlite3.Row]:
        where = "transcripts_fts MATCH ?"
        params: list[Any] = [_fts_query(terms)]
        if after is not None:
            rank, last_id = after
            where += " AND (transcripts_fts.rank > ? OR (transcripts_fts.rank = ? AND t.id > ?))"
            params.extend((rank, rank, last_id))
        with self._lock:
            rows: list[sqlite3.Row] = self._conn.execute(
                "SELECT t.id, t.page, t.content_hash, t.document, t.doc_name, t.doc_path, "  # noqa: S608
                "p.idx AS idx, p.content_hash AS current_hash, transcripts_fts.rank AS rank, "
                "snippet(transcripts_fts, 0, '', '', '…', 16) AS snippet "
                "FROM transcripts_fts JOIN transcripts t ON t.id = transcripts_fts.rowid "
                "LEFT JOIN pages p ON p.uuid = t.page "
                f"WHERE {where} ORDER BY transcripts_fts.rank, t.id LIMIT ?",  # fixed clauses
                (*params, limit),
            ).fetchall()
        return rows

    def _search_names(
        self, terms: Sequence[str], after: Sequence[Any] | None, limit: int
    ) -> list[tuple[str, str]]:
        """``(uuid, name)`` of untranscribed live documents whose name holds every term."""
        where = [
            "d.type = 'DocumentType'",
            "d.deleted_at IS NULL",
            "NOT EXISTS (SELECT 1 FROM transcripts t WHERE t.document = d.uuid)",
        ]
        params: list[Any] = []
        for term in terms:
            where.append("instr(scriptorium_fold(d.name), ?) > 0")
            params.append(_fold(term))
        if after is not None:
            where.append("(d.name COLLATE NOCASE, d.uuid) > (?, ?)")
            params.extend(after)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT d.uuid, d.name FROM documents d WHERE {' AND '.join(where)} "  # noqa: S608
                "ORDER BY d.name COLLATE NOCASE, d.uuid LIMIT ?",  # fixed clauses, values bound
                (*params, limit),
            ).fetchall()
        return [(r["uuid"], r["name"]) for r in rows]

    # ---- sync state -----------------------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT v FROM sync_state WHERE k = ?", (key,)).fetchone()
        return None if row is None else str(row["v"])

    def set_state(self, key: str, value: str | None) -> None:
        """Set ``key``; ``None`` removes it."""
        with self.transaction():
            if value is None:
                self._conn.execute("DELETE FROM sync_state WHERE k = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO sync_state(k, v) VALUES (?, ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (key, value),
                )

    # ---- internals ------------------------------------------------------------------------

    def _name_and_path(self, uuid: str) -> tuple[str, str]:
        """The document's name and its folder path (``"A/B"``; ``""`` at the root or in trash).

        Walks parent links from cloud metadata, so it stops at :data:`MAX_PATH_DEPTH`, on a
        cycle, or at a parent that is not (yet) in the index.
        """
        row = self._conn.execute(
            "SELECT name, parent FROM documents WHERE uuid = ?", (uuid,)
        ).fetchone()
        if row is None:
            return "", ""
        name: str = row["name"]
        folders: list[str] = []
        seen = {uuid}
        parent: str | None = row["parent"]
        while parent is not None and parent != TRASH and len(folders) < MAX_PATH_DEPTH:
            if parent in seen:
                break
            seen.add(parent)
            up = self._conn.execute(
                "SELECT name, parent FROM documents WHERE uuid = ?", (parent,)
            ).fetchone()
            if up is None:
                break
            folders.append(up["name"])
            parent = up["parent"]
        return name, "/".join(reversed(folders))

    def _refresh_transcript_paths(self, uuid: str) -> None:
        """Re-derive ``doc_name``/``doc_path`` for transcripts under ``uuid`` (inclusive)."""
        documents = self._conn.execute(
            "WITH RECURSIVE sub(uuid) AS (SELECT ? UNION "
            "SELECT d.uuid FROM documents d JOIN sub ON d.parent = sub.uuid) "
            "SELECT DISTINCT t.document FROM transcripts t JOIN sub ON t.document = sub.uuid",
            (uuid,),
        ).fetchall()
        for row in documents:
            name, path = self._name_and_path(row["document"])
            self._conn.execute(
                "UPDATE transcripts SET doc_name = ?, doc_path = ? "
                "WHERE document = ? AND (doc_name != ? OR doc_path != ?)",
                (name, path, row["document"], name, path),
            )


def _search_hit(r: sqlite3.Row) -> SearchHit:
    return SearchHit(
        page_id=r["page"],
        document=r["document"],
        idx=r["idx"],
        content_hash=r["content_hash"],
        current=r["current_hash"] == r["content_hash"],
        doc_name=r["doc_name"],
        doc_path=r["doc_path"],
        snippet=r["snippet"],
    )


def _migrate_account(conn: sqlite3.Connection) -> None:
    try:
        run_migrations(conn, ACCOUNT_STEPS)
    except sqlite3.OperationalError as exc:
        if "fts5" in str(exc).lower():
            raise DatabaseSetupError(
                "this SQLite build lacks FTS5, which the index needs",
                hint="Use the Scriptorium image (python:3.12-slim ships FTS5).",
            ) from exc
        raise
