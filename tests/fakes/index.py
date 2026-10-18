"""In-memory fake of ``library/index.py:Index`` (plan §3.4), the part sync uses.

``transaction()`` snapshots the tables and restores them if the block raises anything,
``BaseException`` included, so a crash inside the commit step leaves no row behind, as
SQLite's rollback would. Transactions nest as savepoints. ``document_uuids`` is the
enumeration D proposes adding to §3.4 (see ``cloud/ports.py``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from scriptorium.cloud.ports import DocumentRow
from scriptorium.errors import NotFoundError


@dataclass
class FakeIndex:
    documents: dict[str, DocumentRow] = field(default_factory=dict)
    history: list[tuple[str, int, str, datetime]] = field(default_factory=list)
    state: dict[str, str] = field(default_factory=dict)
    depth: int = 0
    rollbacks: int = 0
    writes_outside_transaction: int = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        snapshot = (
            dict(self.documents),
            list(self.history),
            dict(self.state),
        )
        self.depth += 1
        try:
            yield
        except BaseException:
            self.documents, self.history, self.state = snapshot
            self.rollbacks += 1
            raise
        finally:
            self.depth -= 1

    # ---- documents ----------------------------------------------------------------------

    def upsert_document(self, doc: DocumentRow) -> None:
        self._write()
        self.documents[doc.uuid] = doc

    def mark_deleted(self, uuid: str, cause: str, at: datetime) -> None:
        self._write()
        row = self._row(uuid)
        self.documents[uuid] = dataclasses.replace(row, deleted_at=at, deletion_cause=cause)

    def unmark_deleted(self, uuid: str) -> None:
        self._write()
        row = self._row(uuid)
        self.documents[uuid] = dataclasses.replace(row, deleted_at=None, deletion_cause=None)

    def document(self, uuid: str) -> DocumentRow | None:
        return self.documents.get(uuid)

    def document_uuids(self, *, deleted: bool) -> frozenset[str]:
        return frozenset(
            uuid for uuid, row in self.documents.items() if (row.deleted_at is not None) == deleted
        )

    # ---- history and state --------------------------------------------------------------

    def append_history(self, uuid: str, generation: int, hash_root: str, seen_at: datetime) -> None:
        self._write()
        if any(h[0] == uuid and h[1] == generation for h in self.history):
            raise ValueError("history primary key (uuid, generation) violated")
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
            raise NotFoundError("no such document")
        return row

    def _write(self) -> None:
        if self.depth == 0:
            self.writes_outside_transaction += 1
