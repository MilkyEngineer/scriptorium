"""What the sync loop needs from the archive (Workstream A) and the index (Workstream B).

Plan §3.2 to §3.4 fix those interfaces. This module states the part of them that
``cloud/sync.py`` uses as structural protocols, so the real ``Archive`` and ``Index``
satisfy them without the cloud package importing the library package's internals.

The values sync constructs or passes on, ``DocumentRow`` (B), ``Listing`` and
``TombstonePlan`` (A) and A's filename allowlist ``is_safe_name``, are re-exported here from
their owners so the cloud package names one seam.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from scriptorium.library.index import DocumentRow
from scriptorium.library.layout import is_safe_name
from scriptorium.library.tombstones import Listing, TombstonePlan, plan

# ---- structural seams ---------------------------------------------------------------------


class DocumentView(Protocol):
    """The fields of ``Index.document()``'s row that sync reads."""

    @property
    def hash_root(self) -> str | None: ...
    @property
    def last_opened(self) -> datetime | None: ...
    @property
    def deleted_at(self) -> datetime | None: ...


class ChangeView(Protocol):
    """The fields of an ``Index.recent()`` row that sync reads."""

    @property
    def uuid(self) -> str: ...
    @property
    def deleted_at(self) -> datetime | None: ...


class ChangePage(Protocol):
    """One page of ``Index.recent()``."""

    @property
    def items(self) -> Sequence[ChangeView]: ...
    @property
    def next_cursor(self) -> str | None: ...


class ArchivePort(Protocol):
    """The subset of ``library/archive.py:Archive`` (plan §3.2) that sync calls."""

    def stage(self, uuid: str, job: str) -> AbstractContextManager[Any]: ...
    def put_blob(self, sha256: str, stream: BinaryIO) -> Path: ...
    def commit(
        self, staged: Any, files: Mapping[str, str], generation: int, hash_root: str
    ) -> object: ...
    def tombstone(self, uuid: str) -> None: ...
    def untombstone(self, uuid: str) -> None: ...
    def blob_path(self, sha256: str) -> Path: ...


class IndexPort(Protocol):
    """The subset of ``library/index.py:Index`` (plan §3.4) that sync calls.

    The planner needs the sets of live and tombstoned documents and §3.4 has no method that
    lists them, so sync pages through ``recent`` from the beginning of time, which includes
    tombstoned rows (see ``SyncOnce._library_sets``).
    """

    def transaction(self) -> AbstractContextManager[None]: ...
    def upsert_document(self, doc: DocumentRow) -> None: ...
    def mark_deleted(self, uuid: str, cause: str, at: datetime) -> None: ...
    def unmark_deleted(self, uuid: str) -> None: ...
    def document(self, uuid: str) -> DocumentView | None: ...
    def recent(self, since: datetime, cursor: str | None, limit: int) -> ChangePage: ...
    def append_history(
        self,
        uuid: str,
        generation: int,
        hash_root: str,
        seen_at: datetime,
        *,
        index_sha256: str | None = None,
    ) -> None: ...
    def get_state(self, key: str) -> str | None: ...
    def set_state(self, key: str, value: str | None) -> None: ...


class Planner(Protocol):
    """``library/tombstones.py:plan`` (plan §3.3), called positionally."""

    def __call__(
        self,
        previous: Listing | None,
        current: Listing,
        library: frozenset[str],
        tombstoned: frozenset[str],
        max_per_poll: int,
        /,
        *,
        allow_mass_delete: bool = False,
    ) -> TombstonePlan: ...


def default_planner() -> Planner:
    """A's pure planner (``library/tombstones.py:plan``), used when no planner is injected."""
    return plan


__all__ = [
    "ArchivePort",
    "ChangePage",
    "ChangeView",
    "DocumentRow",
    "DocumentView",
    "IndexPort",
    "Listing",
    "Planner",
    "TombstonePlan",
    "default_planner",
    "is_safe_name",
]
