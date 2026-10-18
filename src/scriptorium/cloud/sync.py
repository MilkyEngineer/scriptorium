"""One poll of one account: Architecture §7 steps 1 to 4 and 7.

1. **Root.** Fetch the root pointer and index. A generation lower than the last one
   seen, or a root with no documents after a non-empty one, sets ``anomaly`` and stops:
   nothing is downloaded or moved. While ``anomaly`` is set every poll returns at once
   without a single cloud request (read-only safe mode) until an operator clears it.
2. **Listing.** Every root entry whose document index hash differs from the committed
   ``hash_root`` has its index fetched; an unchanged hash is an unchanged document
   (indexes are content-addressed), so it needs no fetch. A document whose index fails
   is left alone, the others proceed, and the listing is marked incomplete.
3. **Commit.** Per changed document: stage, download the blobs not yet in ``blobs/``
   (each streamed through SHA-256 verification and its declared size cap), then, in one
   ``Index.transaction()``, append ``history``, upsert the ``documents`` row and
   ``Archive.commit`` the files. File system work goes last inside the transaction, so a
   failure there rolls the rows back and the next poll retries.
4. **Tombstones.** The pure planner decides, from this listing and the previous one
   (``sync_state.previous_listing``), what to tombstone (absent from two consecutive
   complete listings, behind the circuit breaker) and what to un-tombstone (present
   again). Un-tombstoning runs before step 3's commits so a returning document's old
   files are back in place before a newer version replaces them.
7. **Failure classes.** 401/403 from the token endpoint is ``token_rejected``; 429, a
   network failure or three transient failures in a row end the poll with ``backoff``;
   an unparseable root is ``anomaly="protocol"``. ``needs_relink`` (three rejections in
   a row) and the backoff schedule are the worker's decisions, made from the report.

A root identical to the last fully processed one (same hash) is a complete listing of the
same documents, so the planner still runs on it: that is what lets a deletion on a quiet
account reach its second consecutive listing.

Rendering (step 5) and OCR (step 6) belong to the worker.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
import unicodedata
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from scriptorium.cloud.client import CloudClient
from scriptorium.cloud.errors import (
    CloudError,
    RateLimitedError,
    ResponseTooLargeError,
    TokenRejectedError,
    TransientCloudError,
)
from scriptorium.cloud.ports import (
    ArchivePort,
    DocumentRow,
    IndexPort,
    Listing,
    Planner,
    default_planner,
)
from scriptorium.cloud.protocol import (
    MAX_BLOB_BYTES,
    BlobRef,
    DocumentIndex,
    RootEntry,
    RootInfo,
    is_uuid,
)
from scriptorium.errors import PathEscapeError, ScriptoriumError
from scriptorium.logging import log_event

logger = logging.getLogger(__name__)

# sync_state keys (§6.1).
STATE_LAST_ROOT_HASH: Final = "last_root_hash"
STATE_LAST_GENERATION: Final = "last_generation"
STATE_LAST_FULL_LISTING_OK: Final = "last_full_listing_ok"
STATE_PREVIOUS_LISTING: Final = "previous_listing"
STATE_ANOMALY: Final = "anomaly"
STATE_ANOMALY_REASON: Final = "anomaly_reason"

ANOMALY_GENERATION_REGRESSED: Final = "generation_regressed"
ANOMALY_EMPTY_ROOT: Final = "empty_root"
ANOMALY_MASS_DELETE: Final = "mass_delete"
ANOMALY_PROTOCOL: Final = "protocol"

MAX_CONSECUTIVE_FAILURES: Final = 3
MAX_JSON_BYTES: Final = 8 * 1024 * 1024
"""Largest ``.metadata``/``.content`` parsed for the documents row; larger is left unparsed."""
MAX_NAME_CHARS: Final = 1024
MAX_PAGES: Final = 100_000
_DOC_TYPE_RE: Final = re.compile(r"[A-Za-z]{1,32}Type")
_FILE_TYPES: Final = frozenset({"notebook", "pdf", "epub"})
_BIDI_CONTROLS: Final = frozenset(
    "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)

SyncOutcome = Literal["ok", "partial", "backoff", "token_rejected", "anomaly"]


@dataclass(frozen=True)
class SyncSettings:
    """Per-account knobs; the worker builds this from ``Settings`` (§12)."""

    tombstone_max_per_poll: int = 50
    sync_workers: int = 4
    aged_deletion_cause: bool = False
    """S4-gated heuristic: ``deletion_cause="aged"`` for long-unopened documents. Off in M1."""
    aged_after: timedelta = timedelta(days=50)
    acct: str | None = None
    """Account ULID, for log lines only."""

    def __post_init__(self) -> None:
        if self.tombstone_max_per_poll < 0:
            raise ValueError("tombstone_max_per_poll must not be negative")
        if not 1 <= self.sync_workers <= 64:
            raise ValueError("sync_workers must be between 1 and 64")


@dataclass(frozen=True)
class SyncReport:
    """What one poll did. Identifiers only (document UUIDs); no names, no content."""

    outcome: SyncOutcome
    started_at: datetime
    finished_at: datetime
    reason: str | None = None
    """Short machine-readable cause when the outcome is not ``ok``."""
    retry_after_s: float | None = None
    root_hash: str | None = None
    generation: int | None = None
    root_unchanged: bool = False
    listing_complete: bool = False
    documents_listed: int = 0
    documents_unchanged: int = 0
    committed: tuple[str, ...] = ()
    """Documents with a new version on disk: the worker renders their pages."""
    failed: Mapping[str, str] = field(default_factory=dict)
    """Document UUID → reason code, for documents this poll could not list or commit."""
    tombstoned: tuple[str, ...] = ()
    untombstoned: tuple[str, ...] = ()
    rejected_entries: int = 0
    skipped_files: int = 0
    metadata_errors: int = 0
    blobs_downloaded: int = 0
    bytes_downloaded: int = 0
    anomaly: str | None = None
    anomaly_reason: str | None = None


@dataclass(frozen=True)
class _Stop:
    """Why a poll ended early: ``backoff`` or ``token_rejected``. Returned, never raised."""

    outcome: SyncOutcome
    reason: str
    retry_after_s: float | None = None

    @classmethod
    def from_error(cls, exc: CloudError) -> _Stop:
        if isinstance(exc, TokenRejectedError):
            return cls("token_rejected", exc.reason)
        retry = exc.retry_after_s if isinstance(exc, TransientCloudError) else None
        return cls("backoff", exc.reason, retry)


@dataclass
class _Run:
    started_at: datetime
    started_mono: float
    root_hash: str | None = None
    generation: int | None = None
    root_unchanged: bool = False
    listing_complete: bool = False
    documents_listed: int = 0
    documents_unchanged: int = 0
    committed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    tombstoned: list[str] = field(default_factory=list)
    untombstoned: list[str] = field(default_factory=list)
    rejected_entries: int = 0
    skipped_files: int = 0
    metadata_errors: int = 0
    blobs_downloaded: int = 0
    bytes_downloaded: int = 0
    anomaly: str | None = None
    anomaly_reason: str | None = None


class _Streak:
    """Consecutive failures of one kind; three in a row end the poll."""

    def __init__(self) -> None:
        self.count = 0

    def hit(self) -> bool:
        self.count += 1
        return self.count >= MAX_CONSECUTIVE_FAILURES

    def reset(self) -> None:
        self.count = 0


class SyncOnce:
    """Runs §7 steps 1 to 4 for one account against its archive and index."""

    def __init__(
        self,
        client: CloudClient,
        archive: ArchivePort,
        index: IndexPort,
        settings: SyncSettings,
        clock: Callable[[], datetime],
        *,
        planner: Planner | None = None,
    ) -> None:
        self._client = client
        self._archive = archive
        self._index = index
        self._settings = settings
        self._clock = clock
        self._planner = planner if planner is not None else default_planner()

    # ---- the poll ------------------------------------------------------------------------

    def run(self) -> SyncReport:
        run = _Run(started_at=self._clock(), started_mono=time.monotonic())
        anomaly = self._index.get_state(STATE_ANOMALY)
        if anomaly:
            run.anomaly = anomaly
            run.anomaly_reason = self._index.get_state(STATE_ANOMALY_REASON)
            return self._finish(run, "anomaly", reason=anomaly)

        # Step 1: the root.
        try:
            root = self._client.root()
        except TokenRejectedError as exc:
            return self._finish(run, "token_rejected", reason=exc.reason)
        except TransientCloudError as exc:
            return self._finish(run, "backoff", reason=exc.reason, retry=exc.retry_after_s)
        except CloudError as exc:
            return self._raise_anomaly(run, ANOMALY_PROTOCOL, f"root: {exc.reason}")
        run.root_hash, run.generation = root.hash, root.generation
        run.rejected_entries = root.rejected

        previous = _load_listing(self._index.get_state(STATE_PREVIOUS_LISTING))
        last_generation = _parse_int(self._index.get_state(STATE_LAST_GENERATION))
        library = self._index.document_uuids(deleted=False)
        tombstoned = self._index.document_uuids(deleted=True)
        if last_generation is not None and root.generation < last_generation:
            return self._raise_anomaly(
                run,
                ANOMALY_GENERATION_REGRESSED,
                f"root generation {root.generation} is lower than {last_generation}",
            )
        if not root.entries and (library or (previous is not None and previous.uuids)):
            return self._raise_anomaly(
                run,
                ANOMALY_EMPTY_ROOT,
                f"root lists no documents; the archive holds {len(library)}",
            )
        uuids = frozenset(entry.uuid for entry in root.entries)
        run.documents_listed = len(uuids)

        # Step 2: the listing.
        stop: _Stop | None = None
        indexes: dict[str, DocumentIndex] = {}
        if root.hash == self._index.get_state(STATE_LAST_ROOT_HASH):
            run.root_unchanged = True
            run.documents_unchanged = len(uuids)
            complete = True
        else:
            stop = self._list(root, indexes, run)
            complete = stop is None and not run.failed
        run.listing_complete = complete
        current = Listing(uuids=uuids, complete=complete, generation=root.generation)

        if stop is None:
            plan = self._planner(
                previous, current, library, tombstoned, self._settings.tombstone_max_per_poll
            )
            if plan.anomaly is not None:
                # The breaker (or a check the planner shares with step 1) tripped: nothing
                # is moved, downloaded or committed until an operator has looked.
                missing = len(library - uuids)
                self._set_anomaly(
                    run,
                    plan.anomaly,
                    f"{missing} of {len(library)} documents missing from the root",
                )
            else:
                # Step 4a, un-tombstone first; then step 3; then step 4b.
                blocked = self._untombstone(plan.to_untombstone, run)
                entries = {entry.uuid: entry for entry in root.entries}
                stop = self._commit_all(root, entries, indexes, blocked, run)
                if plan.to_tombstone:
                    self._tombstone(plan.to_tombstone, run)

        fully_done = complete and stop is None and not run.failed and run.anomaly is None
        with self._index.transaction():
            self._index.set_state(STATE_LAST_GENERATION, str(root.generation))
            self._index.set_state(STATE_PREVIOUS_LISTING, _dump_listing(current))
            self._index.set_state(STATE_LAST_FULL_LISTING_OK, "true" if complete else "false")
            self._index.set_state(STATE_LAST_ROOT_HASH, root.hash if fully_done else None)

        if run.anomaly is not None:
            return self._finish(run, "anomaly", reason=run.anomaly)
        if stop is not None:
            return self._finish(run, stop.outcome, reason=stop.reason, retry=stop.retry_after_s)
        if run.failed or run.skipped_files or run.rejected_entries or run.metadata_errors:
            return self._finish(run, "partial", reason=_first_reason(run))
        return self._finish(run, "ok")

    # ---- step 2 --------------------------------------------------------------------------

    def _list(self, root: RootInfo, indexes: dict[str, DocumentIndex], run: _Run) -> _Stop | None:
        transient = _Streak()
        for entry in sorted(root.entries, key=lambda e: e.uuid):
            row = self._index.document(entry.uuid)
            if row is not None and row.hash_root == entry.hash:
                run.documents_unchanged += 1
                continue
            try:
                doc_index = self._client.document_index(entry.uuid, entry.hash)
            except (TokenRejectedError, RateLimitedError) as exc:
                return _Stop.from_error(exc)
            except TransientCloudError as exc:
                self._fail(run, entry.uuid, exc.reason)
                if transient.hit():
                    return _Stop.from_error(exc)
                continue
            except CloudError as exc:
                self._fail(run, entry.uuid, exc.reason)
                transient.reset()
                continue
            transient.reset()
            indexes[entry.uuid] = doc_index
            run.skipped_files += doc_index.skipped
        return None

    # ---- step 3 --------------------------------------------------------------------------

    def _commit_all(
        self,
        root: RootInfo,
        entries: Mapping[str, RootEntry],
        indexes: Mapping[str, DocumentIndex],
        blocked: set[str],
        run: _Run,
    ) -> _Stop | None:
        transient, local = _Streak(), _Streak()
        for uuid in sorted(indexes):
            if uuid in blocked:
                continue
            try:
                self._commit_one(root, entries[uuid], indexes[uuid], run)
            except (TokenRejectedError, RateLimitedError) as exc:
                self._fail(run, uuid, exc.reason)
                return _Stop.from_error(exc)
            except TransientCloudError as exc:
                self._fail(run, uuid, exc.reason)
                if transient.hit():
                    return _Stop.from_error(exc)
            except CloudError as exc:
                self._fail(run, uuid, exc.reason)
            except ScriptoriumError as exc:
                self._fail(run, uuid, exc.code, refused=isinstance(exc, PathEscapeError))
            except OSError:
                self._fail(run, uuid, "local_io")
                if local.hit():
                    return _Stop("backoff", "local_io")
            else:
                transient.reset()
                local.reset()
                run.committed.append(uuid)
        return None

    def _commit_one(
        self, root: RootInfo, entry: RootEntry, doc_index: DocumentIndex, run: _Run
    ) -> None:
        files = doc_index.files
        with self._archive.stage(entry.uuid, f"sync-{secrets.token_hex(6)}") as staged:
            self._fetch_blobs(files, run)
            row = self._document_row(entry, doc_index, root.generation, run)
            seen_at = self._clock()
            with self._index.transaction():
                self._index.append_history(entry.uuid, root.generation, entry.hash, seen_at)
                self._index.upsert_document(row)
                self._archive.commit(
                    staged,
                    {name: ref.sha256 for name, ref in files.items()},
                    root.generation,
                    entry.hash,
                )

    def _fetch_blobs(self, files: Mapping[str, BlobRef], run: _Run) -> None:
        wanted: dict[str, tuple[str, int]] = {}
        for name, ref in sorted(files.items()):
            if ref.size > MAX_BLOB_BYTES:
                raise ResponseTooLargeError(f"blob is declared larger than {MAX_BLOB_BYTES}")
            known = wanted.get(ref.sha256)
            if known is None or ref.size > known[1]:
                wanted[ref.sha256] = (known[0] if known else name, ref.size)
        missing = [
            (sha, name, size)
            for sha, (name, size) in wanted.items()
            if not self._archive.blob_path(sha).is_file()
        ]
        workers = min(self._settings.sync_workers, len(missing))
        if workers <= 1:
            for sha, name, size in missing:
                self._count(run, self._fetch_one(sha, name, size))
            return
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sync-blob") as pool:
            futures = [pool.submit(self._fetch_one, sha, name, size) for sha, name, size in missing]
            try:
                for future in as_completed(futures):
                    self._count(run, future.result())
            except BaseException:
                for future in futures:
                    future.cancel()
                raise

    def _fetch_one(self, sha256: str, name: str, size: int) -> int:
        with self._client.open_blob(sha256, max_bytes=size, filename=name) as stream:
            self._archive.put_blob(sha256, stream)
        return size

    @staticmethod
    def _count(run: _Run, size: int) -> None:
        run.blobs_downloaded += 1
        run.bytes_downloaded += size

    # ---- the documents row -----------------------------------------------------------------

    def _document_row(
        self, entry: RootEntry, doc_index: DocumentIndex, generation: int, run: _Run
    ) -> DocumentRow:
        uuid = entry.uuid
        files = doc_index.files
        metadata = self._read_json(files.get(f"{uuid}.metadata"), run, required=True)
        content = self._read_json(files.get(f"{uuid}.content"), run, required=False)
        doc_type = metadata.get("type")
        if not isinstance(doc_type, str) or not _DOC_TYPE_RE.fullmatch(doc_type):
            doc_type = "DocumentType"
        source_kind, page_count = _content_facts(uuid, doc_type, content, files)
        return DocumentRow(
            uuid=uuid,
            type=doc_type,
            parent=_parent(metadata.get("parent")),
            name=_display_name(metadata.get("visibleName"), fallback=uuid),
            last_modified=_epoch_ms(metadata.get("lastModified")),
            last_opened=_epoch_ms(metadata.get("lastOpened")),
            deleted_at=None,
            deletion_cause=None,
            source_kind=source_kind,
            page_count=page_count,
            hash_root=entry.hash,
            generation=generation,
        )

    def _read_json(self, ref: BlobRef | None, run: _Run, *, required: bool) -> dict[str, Any]:
        """A small JSON blob already in ``blobs/``, parsed; ``{}`` if absent or unusable."""
        if ref is None:
            if required:
                run.metadata_errors += 1
            return {}
        try:
            with self._archive.blob_path(ref.sha256).open("rb") as fh:
                raw = fh.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise ValueError("too large")
            data = json.loads(raw)
        except (OSError, ValueError, RecursionError):
            run.metadata_errors += 1
            return {}
        if not isinstance(data, dict):
            run.metadata_errors += 1
            return {}
        return data

    # ---- step 4 --------------------------------------------------------------------------

    def _untombstone(self, uuids: frozenset[str], run: _Run) -> set[str]:
        """Bring returning documents back; returns those that could not be (not committed)."""
        blocked: set[str] = set()
        for uuid in sorted(uuids):
            try:
                with self._index.transaction():
                    self._index.unmark_deleted(uuid)
                    self._archive.untombstone(uuid)
            except (ScriptoriumError, OSError) as exc:
                code = exc.code if isinstance(exc, ScriptoriumError) else "local_io"
                self._fail(run, uuid, code, refused=isinstance(exc, PathEscapeError))
                blocked.add(uuid)
            else:
                run.untombstoned.append(uuid)
        return blocked

    def _tombstone(self, uuids: frozenset[str], run: _Run) -> None:
        now = self._clock()
        for uuid in sorted(uuids):
            cause = self._deletion_cause(uuid, now)
            try:
                with self._index.transaction():
                    self._index.mark_deleted(uuid, cause, now)
                    self._archive.tombstone(uuid)
            except (ScriptoriumError, OSError) as exc:
                code = exc.code if isinstance(exc, ScriptoriumError) else "local_io"
                self._fail(run, uuid, code, refused=isinstance(exc, PathEscapeError))
            else:
                run.tombstoned.append(uuid)

    def _deletion_cause(self, uuid: str, now: datetime) -> str:
        if not self._settings.aged_deletion_cause:
            return "unknown"
        row = self._index.document(uuid)
        opened = row.last_opened if row is not None else None
        if opened is not None and now - opened > self._settings.aged_after:
            return "aged"
        return "unknown"

    # ---- anomalies, failures, the report ---------------------------------------------------

    def _set_anomaly(self, run: _Run, kind: str, reason: str) -> None:
        with self._index.transaction():
            self._index.set_state(STATE_ANOMALY, kind)
            self._index.set_state(STATE_ANOMALY_REASON, reason)
        run.anomaly, run.anomaly_reason = kind, reason
        log_event(
            logger, "sync.anomaly", level=logging.WARNING, acct=self._settings.acct, reason=kind
        )

    def _raise_anomaly(self, run: _Run, kind: str, reason: str) -> SyncReport:
        self._set_anomaly(run, kind, reason)
        return self._finish(run, "anomaly", reason=kind)

    def _fail(self, run: _Run, uuid: str, reason: str, *, refused: bool = False) -> None:
        run.failed[uuid] = reason
        log_event(
            logger,
            "sync.document_failed",
            level=logging.ERROR if refused else logging.WARNING,
            acct=self._settings.acct,
            reason=reason,
        )

    def _finish(
        self,
        run: _Run,
        outcome: SyncOutcome,
        *,
        reason: str | None = None,
        retry: float | None = None,
    ) -> SyncReport:
        report = SyncReport(
            outcome=outcome,
            started_at=run.started_at,
            finished_at=self._clock(),
            reason=reason,
            retry_after_s=retry,
            root_hash=run.root_hash,
            generation=run.generation,
            root_unchanged=run.root_unchanged,
            listing_complete=run.listing_complete,
            documents_listed=run.documents_listed,
            documents_unchanged=run.documents_unchanged,
            committed=tuple(run.committed),
            failed=dict(run.failed),
            tombstoned=tuple(run.tombstoned),
            untombstoned=tuple(run.untombstoned),
            rejected_entries=run.rejected_entries,
            skipped_files=run.skipped_files,
            metadata_errors=run.metadata_errors,
            blobs_downloaded=run.blobs_downloaded,
            bytes_downloaded=run.bytes_downloaded,
            anomaly=run.anomaly,
            anomaly_reason=run.anomaly_reason,
        )
        log_event(
            logger,
            "sync.poll",
            acct=self._settings.acct,
            outcome=outcome,
            reason=reason,
            count=len(run.committed),
            bytes=run.bytes_downloaded,
            duration_ms=round((time.monotonic() - run.started_mono) * 1000, 1),
        )
        return report


# ---- helpers ------------------------------------------------------------------------------


def _first_reason(run: _Run) -> str:
    if run.failed:
        return sorted(run.failed.values())[0]
    if run.rejected_entries:
        return "rejected_entries"
    if run.skipped_files:
        return "skipped_files"
    return "metadata_errors"


def _parse_int(raw: str | None) -> int | None:
    if raw is None or not raw.isascii() or not raw.isdigit():
        return None
    return int(raw)


def _dump_listing(listing: Listing) -> str:
    return json.dumps(
        {
            "complete": listing.complete,
            "generation": listing.generation,
            "uuids": sorted(listing.uuids),
        },
        separators=(",", ":"),
    )


def _load_listing(raw: str | None) -> Listing | None:
    """The stored previous listing; None (so nothing can be tombstoned) if unusable."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    complete, generation, uuids = data.get("complete"), data.get("generation"), data.get("uuids")
    if not isinstance(complete, bool) or type(generation) is not int or generation < 0:
        return None
    if not isinstance(uuids, list) or not all(isinstance(u, str) and is_uuid(u) for u in uuids):
        return None
    return Listing(uuids=frozenset(uuids), complete=complete, generation=generation)


def _display_name(value: object, *, fallback: str) -> str:
    """A cloud name as display data, safe to store, print and encode.

    Drops control characters, lone surrogates (JSON ``"\\ud800"`` decodes to one, and it
    cannot be encoded as UTF-8 later) and the bidirectional overrides and isolates that
    can make a name display as something else; caps the length.
    """
    if not isinstance(value, str):
        return fallback
    clean = "".join(
        ch
        for ch in value
        if unicodedata.category(ch) not in ("Cc", "Cs") and ch not in _BIDI_CONTROLS
    )[:MAX_NAME_CHARS]
    return clean if clean.strip() else fallback


def _parent(value: object) -> str | None:
    """``None`` for the root, ``"trash"``, or a folder UUID; anything else is the root."""
    if isinstance(value, str) and (value == "trash" or is_uuid(value)):
        return value
    return None


def _epoch_ms(value: object) -> datetime | None:
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 16:
        millis = int(value)
    elif type(value) is int and value >= 0:
        millis = value
    else:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _content_facts(
    uuid: str, doc_type: str, content: Mapping[str, Any], files: Mapping[str, BlobRef]
) -> tuple[str | None, int | None]:
    """``(source_kind, page_count)`` from ``.content``, falling back on the file list."""
    if doc_type == "CollectionType":
        return None, None
    kind = content.get("fileType")
    if kind not in _FILE_TYPES:
        if f"{uuid}.pdf" in files:
            kind = "pdf"
        elif f"{uuid}.epub" in files:
            kind = "epub"
        else:
            kind = "notebook"
    count = content.get("pageCount")
    if type(count) is int and 0 <= count <= MAX_PAGES:
        return kind, count
    c_pages = content.get("cPages")
    pages = c_pages.get("pages") if isinstance(c_pages, dict) else None
    if pages is None:
        pages = content.get("pages")
    if isinstance(pages, list) and len(pages) <= MAX_PAGES:
        return kind, len(pages)
    return kind, sum(1 for name in files if name.startswith(f"{uuid}/") and name.endswith(".rm"))
