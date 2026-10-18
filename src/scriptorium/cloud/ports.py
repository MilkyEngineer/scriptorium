"""What the sync loop needs from the archive (Workstream A) and the index (Workstream B).

Plan §3.2 to §3.4 fix those interfaces. This module states the part of them that
``cloud/sync.py`` uses as structural protocols, so the real ``Archive`` and ``Index``
satisfy them without the cloud package importing the library package's internals.

Until ``library/`` is on main it also **mirrors** the values sync constructs or passes on:
``DocumentRow`` (B), ``Listing`` and ``TombstonePlan`` (A) and A's filename allowlist
``is_safe_name``. At merge each mirror below the marked line becomes an import from its
owner (``scriptorium.library.index`` / ``.tombstones`` / ``.layout``); the protocols stay.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Final, Protocol, cast

# ---- structural seams ---------------------------------------------------------------------


class DocumentView(Protocol):
    """The fields of ``Index.document()``'s row that sync reads."""

    @property
    def hash_root(self) -> str: ...
    @property
    def last_opened(self) -> datetime | None: ...
    @property
    def deleted_at(self) -> datetime | None: ...


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

    ``document_uuids`` is **not** in plan §3.4: the tombstone planner needs the sets of
    live and tombstoned documents, and §3.4 offers no way to enumerate them (``children``
    walks one folder). Proposed to B in the hand-back.
    """

    def transaction(self) -> AbstractContextManager[None]: ...
    def upsert_document(self, doc: DocumentRow) -> None: ...
    def mark_deleted(self, uuid: str, cause: str, at: datetime) -> None: ...
    def unmark_deleted(self, uuid: str) -> None: ...
    def document(self, uuid: str) -> DocumentView | None: ...
    def document_uuids(self, *, deleted: bool) -> frozenset[str]: ...
    def append_history(
        self, uuid: str, generation: int, hash_root: str, seen_at: datetime
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
    ) -> TombstonePlan: ...


def default_planner() -> Planner:
    """A's pure planner, resolved when a ``SyncOnce`` is built without one.

    Resolved by name so this package has no import-time dependency on ``library/``
    while A's branch is unmerged; becomes a plain import at merge.
    """
    try:
        module = importlib.import_module("scriptorium.library.tombstones")
    except ModuleNotFoundError:
        raise RuntimeError("scriptorium.library.tombstones is not available") from None
    return cast(Planner, module.plan)


# ---- mirrors until library/ is merged (replace with imports) -------------------------------


@dataclass(frozen=True)
class DocumentRow:
    """Mirror of ``library/index.py:DocumentRow``: the ``documents`` columns of §6.1."""

    uuid: str
    type: str
    parent: str | None
    name: str
    last_modified: datetime | None
    last_opened: datetime | None
    deleted_at: datetime | None
    deletion_cause: str | None
    source_kind: str | None
    page_count: int | None
    hash_root: str
    generation: int


@dataclass(frozen=True)
class Listing:
    """Mirror of ``library/tombstones.py:Listing``."""

    uuids: frozenset[str]
    complete: bool
    generation: int


@dataclass(frozen=True)
class TombstonePlan:
    """Mirror of ``library/tombstones.py:TombstonePlan``."""

    to_tombstone: frozenset[str]
    to_untombstone: frozenset[str]
    anomaly: str | None


_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_SAFE_NAME: Final = re.compile(
    rf"{_UUID}(?:\.(?:metadata|content|pagedata|pdf|epub))?"  # the §11.0 allowlist
    rf"|{_UUID}/{_UUID}(?:\.rm|-metadata\.json)"  # a page inside a document directory
)


def is_safe_name(name: str) -> bool:
    """Mirror of ``library/layout.py:is_safe_name``: the §11.0 allowlist plus page files."""
    return _SAFE_NAME.fullmatch(name) is not None
