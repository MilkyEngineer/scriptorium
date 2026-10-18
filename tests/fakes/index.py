"""In-memory fake of ``library/index.py:Index`` (plan §3.4), the part sync uses.

It follows the merged ``Index`` where sync depends on it: ``upsert_document`` validates the
row and never changes ``deleted_at``/``deletion_cause`` of an existing one,
``append_history`` is idempotent per ``(uuid, generation)``, ``mark_deleted`` and
``unmark_deleted`` raise ``NotFoundError`` for an unknown document, and ``recent`` pages
newest first with tombstoned rows included. ``transaction()`` snapshots the tables and
restores them if the block raises anything, ``BaseException`` included, so a crash inside
the commit step leaves no row behind, as SQLite's rollback would; nested blocks join the
outermost one, as in the real index.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from scriptorium.cloud.ports import DocumentRow
from scriptorium.errors import InvalidArgumentError, NotFoundError

DOCUMENT_TYPES = frozenset({"DocumentType", "CollectionType"})
DELETION_CAUSES = frozenset({"user", "aged", "unknown"})


@dataclass(frozen=True)
class ChangeRow:
    uuid: str
    last_modified: datetime
    deleted_at: datetime | None


@dataclass(frozen=True)
class Page:
    items: list[ChangeRow]
    next_cursor: str | None


@dataclass
class FakeIndex:
    documents: dict[str, DocumentRow] = field(default_factory=dict)
    history: list[tuple[str, int, str, datetime]] = field(default_factory=list)
    state: dict[str, str] = field(default_factory=dict)
    depth: int = 0
    rollbacks: int = 0
    writes_outside_transaction: int = 0
    recent_calls: int = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self.depth:
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1
            return
        snapshot = (dict(self.documents), list(self.history), dict(self.state))
        self.depth = 1
        try:
            yield
        except BaseException:
            self.documents, self.history, self.state = snapshot
            self.rollbacks += 1
            raise
        finally:
            self.depth = 0

    # ---- documents ----------------------------------------------------------------------

    def upsert_document(self, doc: DocumentRow) -> None:
        if doc.type not in DOCUMENT_TYPES:
            raise InvalidArgumentError("Unknown document type.")
        if (doc.deleted_at is None) != (doc.deletion_cause is None):
            raise InvalidArgumentError("deleted_at and deletion_cause go together.")
        if doc.page_count < 0:
            raise InvalidArgumentError("page_count is negative.")
        for moment in (doc.last_modified, doc.last_opened, doc.deleted_at):
            if moment is not None and moment.tzinfo is None:
                raise InvalidArgumentError("Timestamps must carry a time zone.")
        self._write()
        before = self.documents.get(doc.uuid)
        if before is not None:
            doc = dataclasses.replace(
                doc, deleted_at=before.deleted_at, deletion_cause=before.deletion_cause
            )
        self.documents[doc.uuid] = doc

    def mark_deleted(self, uuid: str, cause: str, at: datetime) -> None:
        if cause not in DELETION_CAUSES:
            raise InvalidArgumentError("Unknown deletion cause.")
        self._write()
        row = self._row(uuid)
        self.documents[uuid] = dataclasses.replace(row, deleted_at=at, deletion_cause=cause)

    def unmark_deleted(self, uuid: str) -> None:
        self._write()
        row = self._row(uuid)
        self.documents[uuid] = dataclasses.replace(row, deleted_at=None, deletion_cause=None)

    def document(self, uuid: str) -> DocumentRow | None:
        return self.documents.get(uuid)

    def recent(self, since: datetime, cursor: str | None, limit: int) -> Page:
        """Rows with ``last_modified >= since``, newest first then by UUID; cursor = offset."""
        if not 1 <= limit <= 1000:
            raise InvalidArgumentError("limit must be between 1 and 1000")
        self.recent_calls += 1
        rows = sorted(
            (r for r in self.documents.values() if r.last_modified >= since),
            key=lambda r: (-r.last_modified.timestamp(), r.uuid),
        )
        start = int(cursor) if cursor is not None else 0
        chunk = rows[start : start + limit]
        more = start + limit < len(rows)
        return Page(
            [ChangeRow(r.uuid, r.last_modified, r.deleted_at) for r in chunk],
            str(start + limit) if more else None,
        )

    # ---- history and state --------------------------------------------------------------

    def append_history(self, uuid: str, generation: int, hash_root: str, seen_at: datetime) -> None:
        self._write()
        if any(h[0] == uuid and h[1] == generation for h in self.history):
            return  # ON CONFLICT(uuid, generation) DO NOTHING
        self.history.append((uuid, generation, hash_root, seen_at))

    def history_of(self, uuid: str) -> list[tuple[int, str]]:
        return [(gen, hash_root) for u, gen, hash_root, _ in self.history if u == uuid]

    def get_state(self, key: str) -> str | None:
        return self.state.get(key)

    def set_state(self, key: str, value: str | None) -> None:
        self._write()
        if value is None:
            self.state.pop(key, None)
        else:
            self.state[key] = value

    # ---- helpers ------------------------------------------------------------------------

    def _row(self, uuid: str) -> DocumentRow:
        row = self.documents.get(uuid)
        if row is None:
            raise NotFoundError("Document not found.")
        return row

    def _write(self) -> None:
        if self.depth == 0:
            self.writes_outside_transaction += 1
