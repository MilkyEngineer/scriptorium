"""The per-account index, ``index.sqlite`` (Architecture §6.1; M1 plan §3.4).

The index is the queryable view of one account's archive: the library tree and its tombstones,
version history, pages and their render cache, transcripts with full-text search, and the
sync loop's key/value state. It holds no file content, only names, hashes and timestamps, plus
transcripts (which are note content: nothing here logs them).

Conventions:

- ``DocumentRow.parent`` is ``None`` for the root, ``"trash"`` for the top of the trash, or
  the parent folder's UUID. ``""`` (the cloud's spelling of the root) is stored as ``None``.
- Page indices are 0-based; tools convert (§11.0).
- A page is ``(document, page_id)``: uploading the same notebook files more than once
  (open question 8; push and restore do it) makes documents that share page UUIDs, so a page
  id alone does not name a page. Transcripts are keyed by
  ``(document, page_id, content_hash)``. The archive PNG is keyed by ``(page_id,
  content_hash)`` only, because identical content renders identically, so copies share it.
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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType, TracebackType
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
from scriptorium.library.layout import is_uuid
from scriptorium.library.tags import MAX_TAGS_PER_DOCUMENT, TagSet, normalise_tag

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
    tags: tuple[str, ...] = ()  # the document's own tags, sorted (not its pages'); M2
    excluded_at: datetime | None = None
    """Set while the document is outside the account's sync scope (M2, §11.1): only its
    ``.metadata`` and ``.content`` were fetched, and ``hash_root``/``generation`` still name
    the last version archived before it left the scope (None if it never was)."""


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
    page_id: str  # the page UUID from the document's .content (copies share it)
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
    prompt_version: str | None = None  # ocr/prompt.md version (server-side OCR, M2)


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
    tags: tuple[str, ...] = ()  # the page's own tags and its document's, sorted


@dataclass(frozen=True, slots=True)
class DocumentHit:
    """A document with no transcript whose name contains every search term (§11.2).

    Name hits follow the transcript hits: only transcribed pages are in the full-text index,
    so this is how an untranscribed document is found at all.
    """

    document: str
    doc_name: str
    doc_path: str
    tags: tuple[str, ...] = ()  # the document's own tags, sorted


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
    except (binascii.Error, ValueError, RecursionError):  # deep nesting: RecursionError
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


def _tag_filter(tags: Sequence[str] | None) -> list[str] | None:
    """The names of a ``tags=`` filter, normalised as stored; ``None`` (or empty) = no filter.

    A filter is an any-of match on exact (normalised) names. A name the tablet could never
    have stored (not a string, a control character, empty) is ``invalid_argument``.
    """
    if not tags:
        return None
    if isinstance(tags, str):
        raise InvalidArgumentError("tags is a list of tag names.")
    if len(tags) > MAX_TAGS_PER_DOCUMENT:
        raise InvalidArgumentError(f"A filter takes at most {MAX_TAGS_PER_DOCUMENT} tags.")
    names: list[str] = []
    for tag in tags:
        name = normalise_tag(tag)
        if name is None:
            raise InvalidArgumentError("A tag name is empty or holds a control character.")
        if name not in names:
            names.append(name)
    return names


def _marks(count: int) -> str:
    """``?, ?, …`` for an ``IN`` list of ``count`` bound values."""
    return ", ".join("?" * count)


def _has_tag(document: str, page: str, count: int) -> str:
    """``EXISTS`` clause: the row ``document`` names has one of ``count`` bound tag names.

    ``document`` and ``page`` are fixed SQL fragments from this module, never input.
    """
    return (
        f"EXISTS (SELECT 1 FROM tags g WHERE g.document = {document} AND {page} "  # noqa: S608
        f"AND g.name IN ({_marks(count)}))"
    )


def _tags_text(names: Iterable[str]) -> str:
    """``transcripts.doc_tags``: the names, sorted, one per line (each is its own FTS text)."""
    return "\n".join(sorted(names))


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


def _document(row: sqlite3.Row, tags: Mapping[str, tuple[str, ...]] | None = None) -> DocumentRow:
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
        tags=() if tags is None else tags.get(row["uuid"], ()),
        excluded_at=opt_from_db_time(row["excluded_at"]),
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
        ``excluded_at`` is always taken from ``doc``: the sync loop decides the scope on every
        upsert (None for a document it archived, the time it left the scope otherwise).
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
                "deleted_at, deletion_cause, source_kind, page_count, hash_root, generation, "
                "excluded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(uuid) DO UPDATE SET type = excluded.type, "
                "parent = excluded.parent, name = excluded.name, "
                "last_modified = excluded.last_modified, last_opened = excluded.last_opened, "
                "source_kind = excluded.source_kind, page_count = excluded.page_count, "
                "hash_root = excluded.hash_root, generation = excluded.generation, "
                "excluded_at = excluded.excluded_at",
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
                    _opt_time(doc.excluded_at),
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
            if row is None:
                return None
            return _document(row, self._document_tags([uuid]))

    def document_uuids(self, *, deleted: bool, excluded: bool | None = None) -> frozenset[str]:
        """Every document and folder UUID: tombstoned ones with ``deleted=True``, the rest
        (the trash included) with ``deleted=False``. These are the tombstone planner's
        ``library`` and ``tombstoned`` sets (§7 step 4). ``excluded`` narrows to the rows
        outside the sync scope (True) or inside it (False)."""
        sql = (
            "SELECT uuid FROM documents WHERE deleted_at IS NOT NULL"
            if deleted
            else "SELECT uuid FROM documents WHERE deleted_at IS NULL"
        )
        if excluded is not None:
            sql += " AND excluded_at IS NOT NULL" if excluded else " AND excluded_at IS NULL"
        with self._lock:
            return frozenset(row["uuid"] for row in self._conn.execute(sql))

    def children(
        self,
        parent: str | None,
        *,
        include_trash: bool,
        include_deleted: bool,
        tags: Sequence[str] | None = None,
        cursor: str | None,
        limit: int,
    ) -> Page[DocumentRow]:
        """Children of ``parent`` (``None`` = root), sorted folders first, then by name.

        Sort key ``(type, name case-insensitively, uuid)``. At the root, ``include_trash`` also
        lists the top of the trash; pass ``parent="trash"`` to list only that. Tombstoned rows
        appear only with ``include_deleted``.

        ``tags`` (§11.2) keeps the documents and folders carrying any of the names as their
        own tags (exact, normalised names; page tags do not count) and makes the listing
        recursive: everything below ``parent`` at any depth, in one flat list under the same
        sort key. At the root, the trash's subtree is included only with ``include_trash``.
        """
        _check_limit(limit)
        parent = _norm_parent(parent)
        names = _tag_filter(tags)
        start: str
        params: list[Any] = []
        if parent is None and include_trash:
            start = "(parent IS NULL OR parent = ?)"
            params.append(TRASH)
        else:
            start = "parent IS ?"
            params.append(parent)
        where: list[str] = []
        prefix = ""
        if names is None:
            where.append(start)
        else:
            # Every row reachable from parent; UNION drops repeats, so a parent cycle in cloud
            # metadata ends the walk.
            prefix = (
                f"WITH RECURSIVE sub(uuid) AS (SELECT uuid FROM documents WHERE {start} "  # noqa: S608
                "UNION SELECT d.uuid FROM documents d JOIN sub ON d.parent = sub.uuid) "
            )
            where.append("uuid IN (SELECT uuid FROM sub)")
            where.append(_has_tag("documents.uuid", "g.page = ''", len(names)))
            params.extend(names)
        if not include_deleted:
            where.append("deleted_at IS NULL")
        if cursor is not None:
            key = _decode_cursor(cursor, "children", (str, str, str))
            where.append("(type, name COLLATE NOCASE, uuid) > (?, ?, ?)")
            params.extend(key)
        sql = (
            f"{prefix}SELECT * FROM documents WHERE {' AND '.join(where)} "  # noqa: S608
            "ORDER BY type, name COLLATE NOCASE, uuid LIMIT ?"  # fixed clauses, values bound
        )
        with self._lock:
            rows = self._conn.execute(sql, (*params, limit + 1)).fetchall()
            found = self._document_tags([r["uuid"] for r in rows[:limit]])
        items = [_document(r, found) for r in rows[:limit]]
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
            found = self._document_tags([r["uuid"] for r in rows[:limit]])
        items = [_document(r, found) for r in rows[:limit]]
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

        Pages are matched by ``page_id`` within the document; another document listing the
        same page ids (the same files uploaded twice) is not affected. A page whose ``content_hash``
        is unchanged keeps its stored ``stable_since``, ``rendered_at`` and ``png_path`` (only
        its index may move). A new or changed page takes ``stable_since`` from the argument
        and ``rendered_at = NULL`` (it needs rendering); a changed page keeps its old
        ``png_path`` as the last good image unless the argument gives one. Pages no longer
        listed are dropped from the current view (their transcripts and the archive's files
        are untouched).
        """
        ids = [p.page_id for p in pages]
        if len(set(ids)) != len(ids):
            raise InvalidArgumentError("Duplicate page id.")
        if len({p.idx for p in pages}) != len(pages) or any(p.idx < 0 for p in pages):
            raise InvalidArgumentError("Page indices must be distinct and non-negative.")
        if any(p.document != uuid for p in pages):
            raise InvalidArgumentError("A page row names a different document from uuid.")
        keep = set(ids)
        with self.transaction():
            existing = {
                r["uuid"]: r
                for r in self._conn.execute(
                    "SELECT uuid, content_hash FROM pages WHERE document = ?", (uuid,)
                )
            }
            self._conn.executemany(
                "DELETE FROM pages WHERE document = ? AND uuid = ?",
                [(uuid, pid) for pid in existing if pid not in keep],
            )
            # Park current indices out of the way so moves cannot hit UNIQUE(document, idx).
            self._conn.execute("UPDATE pages SET idx = -1 - idx WHERE document = ?", (uuid,))
            for p in pages:
                old = existing.get(p.page_id)
                if old is not None and old["content_hash"] == p.content_hash:
                    self._conn.execute(
                        "UPDATE pages SET idx = ? WHERE document = ? AND uuid = ?",
                        (p.idx, uuid, p.page_id),
                    )
                    continue
                self._conn.execute(
                    "INSERT INTO pages(uuid, document, idx, content_hash, stable_since, "
                    "rendered_at, png_path) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(document, uuid) DO UPDATE SET idx = excluded.idx, "
                    "content_hash = excluded.content_hash, "
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

    def counts(self) -> dict[str, int]:
        """Totals for ``account_status``: documents, folders, trashed, tombstoned, pages,
        rendered pages and transcripts. Documents and folders exclude trashed and tombstoned
        rows; pages and rendered pages are those of documents that are not tombstoned
        (trashed ones included, as they are still in the mirror), so the page total
        tracks the documents a user sees. Tombstoned documents keep their pages and
        renders; they are just not counted here."""
        with self._lock:
            row = self._conn.execute(
                "SELECT "
                "sum(type = 'DocumentType' AND deleted_at IS NULL AND parent IS NOT ?) AS docs, "
                "sum(type = 'CollectionType' AND deleted_at IS NULL AND parent IS NOT ?) AS dirs, "
                "sum(deleted_at IS NULL AND parent = ?) AS trashed, "
                "sum(deleted_at IS NOT NULL) AS tombstoned FROM documents",
                (TRASH, TRASH, TRASH),
            ).fetchone()
            pages = self._conn.execute(
                "SELECT count(*) AS n, count(p.rendered_at) AS r FROM pages AS p "
                "JOIN documents AS d ON d.uuid = p.document WHERE d.deleted_at IS NULL"
            ).fetchone()
            transcripts = self._conn.execute("SELECT count(*) AS n FROM transcripts").fetchone()
        return {
            "documents": int(row["docs"] or 0),
            "folders": int(row["dirs"] or 0),
            "trashed": int(row["trashed"] or 0),
            "tombstoned": int(row["tombstoned"] or 0),
            "pages": int(pages["n"]),
            "rendered": int(pages["r"]),
            "transcripts": int(transcripts["n"]),
        }

    def page_by_id(self, document: str, page_id: str) -> PageRow | None:
        """The current page ``page_id`` of ``document`` (another document may share the id)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pages WHERE document = ? AND uuid = ?", (document, page_id)
            ).fetchone()
        return None if row is None else _page(row)

    def set_rendered(
        self, document: str, page_id: str, content_hash: str, png_path: str, at: datetime
    ) -> bool:
        """Record the archive PNG of page version ``(page_id, content_hash)``.

        The PNG is keyed by the page version, not the document, so it is recorded on every
        document whose page ``page_id`` currently has ``content_hash`` (documents uploaded
        from the same files share one render and one render job). Rows whose page has moved
        on to another hash are left alone: a render that finishes after the page changed
        again is not recorded. Returns whether ``document``'s own page was one of the rows
        marked.
        """
        with self.transaction():
            own = self._conn.execute(
                "SELECT 1 FROM pages WHERE document = ? AND uuid = ? AND content_hash = ?",
                (document, page_id, content_hash),
            ).fetchone()
            self._conn.execute(
                "UPDATE pages SET rendered_at = ?, png_path = ? "
                "WHERE uuid = ? AND content_hash = ?",
                (to_db_time(at), png_path, page_id, content_hash),
            )
        return own is not None

    # ---- tags (§6.1, M2) ----------------------------------------------------------------

    def replace_tags(self, document: str, tags: TagSet) -> None:
        """Make ``tags`` the document's tags: a whole replace, so a tag removed on the tablet
        disappears here. Joins the caller's transaction (§7 step 3 commits pages and tags
        together).

        ``tags`` comes from :func:`scriptorium.library.tags.parse_tags`; a set it could not
        have produced (too many names, a name it would not store, a page id that is not a
        UUID) is ``invalid_argument``. The tag text indexed with the document's transcripts
        is refreshed in the same transaction.
        """
        if len(tags) > MAX_TAGS_PER_DOCUMENT:
            raise InvalidArgumentError(f"A document has at most {MAX_TAGS_PER_DOCUMENT} tags.")
        rows: list[tuple[str, str, str]] = [(document, "", name) for name in tags.document]
        for page_id, names in tags.pages.items():
            if not is_uuid(page_id):
                raise InvalidArgumentError("A page tag names a page id that is not a UUID.")
            rows.extend((document, page_id, name) for name in names)
        if any(normalise_tag(name) != name for _, _, name in rows):
            raise InvalidArgumentError("A tag name is not normalised.")
        with self.transaction():
            self._conn.execute("DELETE FROM tags WHERE document = ?", (document,))
            self._conn.executemany("INSERT INTO tags(document, page, name) VALUES (?, ?, ?)", rows)
            for row in self._conn.execute(
                "SELECT id, page, doc_tags FROM transcripts WHERE document = ?", (document,)
            ).fetchall():
                text = _tags_text(tags.for_page(row["page"]))
                if text != row["doc_tags"]:
                    self._conn.execute(
                        "UPDATE transcripts SET doc_tags = ? WHERE id = ?", (text, row["id"])
                    )

    def tags_of(self, document: str) -> TagSet:
        """The document's stored tags (empty for an unknown document)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT page, name FROM tags WHERE document = ?", (document,)
            ).fetchall()
        own = frozenset(r["name"] for r in rows if r["page"] == "")
        pages: dict[str, set[str]] = {}
        for r in rows:
            if r["page"] != "":
                pages.setdefault(r["page"], set()).add(r["name"])
        return TagSet(own, MappingProxyType({p: frozenset(n) for p, n in pages.items()}))

    def documents_tagged(self, names: Sequence[str]) -> frozenset[str]:
        """Documents and folders (any state) carrying any of ``names`` as their own tags.

        Names are normalised as stored and matched exactly; a name that could never be stored
        matches nothing. Page tags do not count. For the sync scope filter (§11.1).
        """
        wanted = sorted({n for n in map(normalise_tag, names) if n is not None})
        found: set[str] = set()
        with self._lock:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                found.update(
                    r["document"]
                    for r in self._conn.execute(
                        "SELECT DISTINCT document FROM tags "  # noqa: S608 - placeholders only
                        f"WHERE page = '' AND name IN ({_marks(len(chunk))})",
                        chunk,
                    )
                )
        return frozenset(found)

    def _document_tags(self, uuids: Sequence[str]) -> dict[str, tuple[str, ...]]:
        """Each document's own tags, sorted; documents without any are absent."""
        out: dict[str, list[str]] = {}
        for start in range(0, len(uuids), 500):
            chunk = list(uuids[start : start + 500])
            for r in self._conn.execute(
                "SELECT document, name FROM tags "  # noqa: S608 - placeholders only
                f"WHERE page = '' AND document IN ({_marks(len(chunk))}) ORDER BY document, name",
                chunk,
            ):
                out.setdefault(r["document"], []).append(r["name"])
        return {uuid: tuple(names) for uuid, names in out.items()}

    def _page_tags(self, documents: Iterable[str]) -> dict[str, dict[str, set[str]]]:
        """``document → page id ('' for the document) → names`` for ``documents``."""
        wanted = sorted(set(documents))
        out: dict[str, dict[str, set[str]]] = {}
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            for r in self._conn.execute(
                "SELECT document, page, name FROM tags "  # noqa: S608 - placeholders only
                f"WHERE document IN ({_marks(len(chunk))})",
                chunk,
            ):
                out.setdefault(r["document"], {}).setdefault(r["page"], set()).add(r["name"])
        return out

    # ---- transcripts --------------------------------------------------------------------

    def put_transcript(
        self,
        document: str,
        page_id: str,
        content_hash: str,
        text: str,
        model: str,
        mode: str,
        *,
        prompt_version: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Store (or replace) the transcript of one version of ``document``'s page ``page_id``.

        The document's page must be in the index (``not_found`` otherwise); a copy of the
        page in another document has its own transcript. Whether ``content_hash`` is the
        page's *current* hash is the caller's check (``save_transcript`` answers ``stale_hash``);
        a transcript of an older version is still valid for that version. Server-side OCR
        records the prompt version, the token counts and the cost (§6.1, §8.2); a replacement
        takes the new values (``None`` where the new transcript has none).
        """
        if mode not in TRANSCRIPT_MODES:
            raise InvalidArgumentError("Unknown transcript mode.")
        for count in (input_tokens, output_tokens):
            if count is not None and (type(count) is not int or count < 0):
                raise InvalidArgumentError("Token counts are non-negative integers.")
        if cost_usd is not None and not 0 <= cost_usd < float("inf"):
            raise InvalidArgumentError("A cost is a non-negative amount.")
        with self.transaction():
            row = self._conn.execute(
                "SELECT 1 FROM pages WHERE document = ? AND uuid = ?", (document, page_id)
            ).fetchone()
            if row is None:
                raise NotFoundError("Page not found.")
            name, path = self._name_and_path(document)
            on_document = self._page_tags([document]).get(document, {})
            tags_text = _tags_text(on_document.get("", set()) | on_document.get(page_id, set()))
            self._conn.execute(
                "INSERT INTO transcripts(page, content_hash, document, text, model, mode, "
                "created_at, doc_name, doc_path, doc_tags, prompt_version, input_tokens, "
                "output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(document, page, content_hash) DO UPDATE SET text = excluded.text, "
                "model = excluded.model, mode = excluded.mode, "
                "created_at = excluded.created_at, doc_name = excluded.doc_name, "
                "doc_path = excluded.doc_path, doc_tags = excluded.doc_tags, "
                "prompt_version = excluded.prompt_version, "
                "input_tokens = excluded.input_tokens, output_tokens = excluded.output_tokens, "
                "cost_usd = excluded.cost_usd",
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
                    tags_text,
                    prompt_version,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                ),
            )

    def transcript(self, document: str, page_id: str, content_hash: str) -> TranscriptRow | None:
        """The transcript of ``document``'s page ``page_id`` at ``content_hash``, if any."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM transcripts WHERE document = ? AND page = ? AND content_hash = ?",
                (document, page_id, content_hash),
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
            prompt_version=r["prompt_version"],
        )

    def pages_without_transcript(
        self, *, folders: Sequence[str], recursive: bool = True, limit: int
    ) -> list[PageRow]:
        """Current pages under ``folders`` with no transcript of their current version.

        ``folders`` are folder (``CollectionType``) UUIDs; with ``recursive`` every document
        below them counts, otherwise only their direct children. Only live documents: not
        tombstoned, not in the trash, not excluded from the sync scope. A page copied into
        another document is listed under each document (transcripts are per document).
        Sorted by document UUID and page index; at most ``limit`` rows. Parent links come
        from cloud metadata, so the walk tolerates cycles (``UNION`` visits a folder once).
        """
        _check_limit(limit)
        roots = sorted(set(folders))
        if not roots:
            return []
        marks = ", ".join("?" * len(roots))
        # A folder that is itself in the trash is not live; nor is anything below it.
        start = (
            f"SELECT uuid FROM documents WHERE uuid IN ({marks}) "  # noqa: S608 - placeholders
            "AND type = 'CollectionType' AND deleted_at IS NULL AND parent IS NOT 'trash'"
        )
        if recursive:
            tree = (
                f"WITH RECURSIVE tree(uuid) AS ({start} "  # noqa: S608 - fixed clauses
                "UNION SELECT d.uuid FROM documents d JOIN tree ON d.parent = tree.uuid "
                "WHERE d.type = 'CollectionType' AND d.deleted_at IS NULL) "
            )
        else:
            tree = f"WITH tree(uuid) AS ({start}) "
        sql = (
            tree  # noqa: S608 - fixed clauses, values bound
            + "SELECT p.* FROM pages p JOIN documents d ON d.uuid = p.document "
            "WHERE d.parent IN (SELECT uuid FROM tree) AND d.type = 'DocumentType' "
            "AND d.deleted_at IS NULL AND d.excluded_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM transcripts t WHERE t.document = p.document "
            "AND t.page = p.uuid AND t.content_hash = p.content_hash) "
            "ORDER BY p.document, p.idx LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (*roots, limit)).fetchall()
        return [_page(r) for r in rows]

    def search(
        self,
        query: str,
        *,
        folder: str | None = None,
        tags: Sequence[str] | None = None,
        cursor: str | None,
        limit: int,
    ) -> Page[SearchHit | DocumentHit]:
        """Full-text search over transcripts, document names, folder paths and tag names, then
        names (§11.2).

        Every whitespace-separated term must match (each is quoted: FTS5 operators, tag names
        included, are never interpreted). The transcript hits come first, sorted by bm25 rank,
        then transcript id; the cursor carries the last ``(rank, id)``, so a page boundary is
        exact for an unchanged index (ranks shift when transcripts are added). After them come
        :class:`DocumentHit` rows: live documents with no transcript at all whose name or own
        tags contain every term, compared case-insensitively, sorted by name and UUID.

        ``folder`` (a folder UUID) keeps documents anywhere below it. ``tags`` is an any-of
        filter on exact, normalised names: a transcript hit matches on its page's own tags or
        its document's, a document hit on its document's own tags. Hits carry ``tags``: for a
        page, its own and its document's; for a document, its own.
        """
        _check_limit(limit)
        terms = _search_terms(query)
        names = _tag_filter(tags)
        name_key: list[Any] | None = None
        fts_key: list[Any] | None = None
        if cursor is not None:
            try:
                fts_key = _decode_cursor(cursor, "search", (float, int))
            except InvalidArgumentError:
                name_key = _decode_cursor(cursor, "search-name", (str, str))
        items: list[SearchHit | DocumentHit] = []
        with self._lock:
            if name_key is None:
                rows = self._search_fts(terms, folder, names, fts_key, limit + 1)
                found = self._page_tags(r["document"] for r in rows[:limit])
                items.extend(_search_hit(r, found) for r in rows[:limit])
                if len(rows) > limit:
                    last = rows[limit - 1]
                    key = (float(last["rank"]), int(last["id"]))
                    return Page(items, _encode_cursor("search", key))
            room = limit - len(items)
            matched = self._search_names(terms, folder, names, name_key, room + 1)
            hits = matched[:room]
            own = self._document_tags([uuid for uuid, _ in hits])
            for uuid, name in hits:
                _, path = self._name_and_path(uuid)
                items.append(
                    DocumentHit(document=uuid, doc_name=name, doc_path=path, tags=own.get(uuid, ()))
                )
        next_cursor = None
        if len(matched) > room:
            # The name phase's sort key is (name, uuid); "" sorts before every name.
            after = (hits[-1][1], hits[-1][0]) if hits else ("", "")
            next_cursor = _encode_cursor("search-name", after)
        return Page(items, next_cursor)

    @staticmethod
    def _subtree(folder: str | None) -> tuple[str, list[Any]]:
        """A ``WITH`` prefix naming ``sub``, every row below ``folder``; ``""`` for none."""
        if folder is None:
            return "", []
        # UNION drops repeats, so a parent cycle in cloud metadata ends the walk.
        return (
            "WITH RECURSIVE sub(uuid) AS (SELECT uuid FROM documents WHERE parent = ? "
            "UNION SELECT d.uuid FROM documents d JOIN sub ON d.parent = sub.uuid) ",
            [folder],
        )

    def _search_fts(
        self,
        terms: Sequence[str],
        folder: str | None,
        names: Sequence[str] | None,
        after: Sequence[Any] | None,
        limit: int,
    ) -> list[sqlite3.Row]:
        prefix, params = self._subtree(folder)
        where = ["transcripts_fts MATCH ?"]
        params.append(_fts_query(terms))
        if folder is not None:
            where.append("t.document IN (SELECT uuid FROM sub)")
        if names is not None:
            where.append(_has_tag("t.document", "g.page IN ('', t.page)", len(names)))
            params.extend(names)
        if after is not None:
            rank, last_id = after
            where.append("(transcripts_fts.rank > ? OR (transcripts_fts.rank = ? AND t.id > ?))")
            params.extend((rank, rank, last_id))
        rows: list[sqlite3.Row] = self._conn.execute(
            f"{prefix}SELECT t.id, t.page, t.content_hash, t.document, t.doc_name, "  # noqa: S608
            "t.doc_path, p.idx AS idx, p.content_hash AS current_hash, "
            "transcripts_fts.rank AS rank, "
            "snippet(transcripts_fts, 0, '', '', '…', 16) AS snippet "
            "FROM transcripts_fts JOIN transcripts t ON t.id = transcripts_fts.rowid "
            "LEFT JOIN pages p ON p.document = t.document AND p.uuid = t.page "
            f"WHERE {' AND '.join(where)} "  # fixed clauses, values bound
            "ORDER BY transcripts_fts.rank, t.id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return rows

    def _search_names(
        self,
        terms: Sequence[str],
        folder: str | None,
        names: Sequence[str] | None,
        after: Sequence[Any] | None,
        limit: int,
    ) -> list[tuple[str, str]]:
        """``(uuid, name)`` of untranscribed live documents whose name or own tags hold every
        term."""
        prefix, params = self._subtree(folder)
        where = [
            "d.type = 'DocumentType'",
            "d.deleted_at IS NULL",
            "NOT EXISTS (SELECT 1 FROM transcripts t WHERE t.document = d.uuid)",
        ]
        for term in terms:
            where.append(
                "(instr(scriptorium_fold(d.name), ?) > 0 OR EXISTS (SELECT 1 FROM tags g "
                "WHERE g.document = d.uuid AND g.page = '' "
                "AND instr(scriptorium_fold(g.name), ?) > 0))"
            )
            folded = _fold(term)
            params.extend((folded, folded))
        if folder is not None:
            where.append("d.uuid IN (SELECT uuid FROM sub)")
        if names is not None:
            where.append(_has_tag("d.uuid", "g.page = ''", len(names)))
            params.extend(names)
        if after is not None:
            where.append("(d.name COLLATE NOCASE, d.uuid) > (?, ?)")
            params.extend(after)
        rows = self._conn.execute(
            f"{prefix}SELECT d.uuid, d.name FROM documents d "  # noqa: S608
            f"WHERE {' AND '.join(where)} "  # fixed clauses, values bound
            "ORDER BY d.name COLLATE NOCASE, d.uuid LIMIT ?",
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

    # ---- per-account settings (§6.1) ------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        """The JSON text stored under ``key`` (``auto_ocr``, ``sync_scope``), or ``None``."""
        with self._lock:
            row = self._conn.execute("SELECT v FROM settings WHERE k = ?", (key,)).fetchone()
        return None if row is None else str(row["v"])

    def set_setting(self, key: str, value: str | None) -> None:
        """Set ``key`` to the JSON text ``value``; ``None`` removes it."""
        with self.transaction():
            if value is None:
                self._conn.execute("DELETE FROM settings WHERE k = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO settings(k, v) VALUES (?, ?) "
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


def _search_hit(r: sqlite3.Row, tags: Mapping[str, Mapping[str, set[str]]]) -> SearchHit:
    on_document = tags.get(r["document"], {})
    names = on_document.get("", set()) | on_document.get(r["page"], set())
    return SearchHit(
        page_id=r["page"],
        document=r["document"],
        idx=r["idx"],
        content_hash=r["content_hash"],
        current=r["current_hash"] == r["content_hash"],
        doc_name=r["doc_name"],
        doc_path=r["doc_path"],
        snippet=r["snippet"],
        tags=tuple(sorted(names)),
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
