"""One poll of one account: Architecture §7 steps 1 to 4 and 7.

1. **Root.** Fetch the root pointer and index. A generation lower than the last one
   seen, or a root with no documents after a non-empty one (the planner's rules 1 and
   2, asked before anything is listed), sets ``anomaly`` and stops: nothing is
   downloaded or moved. While ``anomaly`` is set every poll returns at once without a
   single cloud request (read-only safe mode) until an operator clears it.
2. **Listing.** Every root entry whose document index hash differs from the committed
   ``hash_root`` has its index fetched; an unchanged hash is an unchanged document
   (indexes are content-addressed), so it needs no fetch. A document whose index fails
   is left alone, the others proceed, and the listing is marked incomplete.
3. **Commit.** Per changed document: stage, download the blobs not yet in ``blobs/``
   (each streamed through SHA-256 verification and its declared size cap) and store the
   document index itself as a blob, then, in one
   ``Index.transaction()``, append ``history``, upsert the ``documents`` row and
   ``Archive.commit`` the files. File system work goes last inside the transaction, so a
   failure there rolls the rows back and the next poll retries.
4. **Tombstones.** The pure planner decides, from this listing and the previous one
   (``sync_state.previous_listing``), what to tombstone (absent from two consecutive
   complete listings, behind the circuit breaker) and what to un-tombstone (present
   again). Un-tombstoning runs before step 3's commits so a returning document's old
   files are back in place before a newer version replaces them. No plan is applied
   once an anomaly is set. An administrator's one-shot approval
   (``sync_state.allow_mass_delete``, written by ``admin_clear_anomaly``) is bound to the
   plan it was given for: JSON ``{"candidates": [...], "root_generation": n,
   "expires_at": iso}``. The planner always runs without it first. Only if that plan trips
   the breaker, the approval has not expired, the root generation has not gone below
   ``root_generation``, and every tripped candidate is in ``candidates``, does the planner
   run again with ``allow_mass_delete=True``. Any poll whose planner ran spends the
   approval, used or not (an expired, uncovered, unparseable or unneeded one is logged as
   ``sync.allow_mass_delete_rejected`` with a reason code and a count); a poll stopped
   before planning (during the listing, or by an early anomaly) leaves it in place.
7. **Failure classes.** 401/403 from the token endpoint is ``token_rejected``; 429, a
   network failure or three transient failures in a row end the poll with ``backoff``;
   an unparseable root (a ``CloudProtocolError`` or a hash mismatch) is
   ``anomaly="protocol"``. Any other non-retryable status on the root pointer or root
   index (404, 400, a refused redirect) is ``backoff`` with the error's reason: it says
   nothing about the data, and an anomaly would freeze the account until an operator
   clears it. ``needs_relink`` (three rejections in
   a row) and the backoff schedule are the worker's decisions, made from the report.

**Budgets.** A poll downloads at most ``sync_max_poll_bytes`` of blobs and keeps starting
work for at most ``sync_max_poll_seconds``. Both are checked before each document index,
document and blob is started; once either is spent the poll starts nothing new, keeps what
it has committed, and ends ``partial`` with reason ``byte_budget`` or ``time_budget``. The
document in progress is not committed, but the blobs it fetched stay (they are
content-addressed), so the next poll resumes where this one stopped. The byte check passes
while nothing has been downloaded, so a poll with time left always fetches at least one blob.

A poll that ends early for any reason (a budget, ``backoff``, ``token_rejected``) applies
no tombstones: deletions wait for a poll that ran to the end.

A listing in which some document index could not be fetched is incomplete, so one document
that fails on every poll suspends tombstoning for the whole account. That is deliberate,
and it is made visible: such a poll records ``sync_state.tombstoning_suspended_since`` (the
time of the first one) and ``tombstoning_suspended_failed`` (how many documents failed), and
the next complete listing clears both. ``account_status`` warns while they are set.

A root identical to the last fully processed one (same hash) is a complete listing of the
same documents, so the planner still runs on it: that is what lets a deletion on a quiet
account reach its second consecutive listing.

Rendering (step 5) and OCR (step 6) belong to the worker.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import secrets
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from scriptorium.cloud.client import CloudClient
from scriptorium.cloud.errors import (
    BlobVerificationError,
    CloudError,
    CloudProtocolError,
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
    TombstonePlan,
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
from scriptorium.db.connect import to_db_time
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
STATE_ALLOW_MASS_DELETE: Final = "allow_mass_delete"
"""One-shot administrator approval of one breaker trip (:func:`mass_delete_approval`)."""
STATE_TOMBSTONING_SUSPENDED_SINCE: Final = "tombstoning_suspended_since"
"""When the run of polls whose listing failed on some documents began (cleared when complete)."""
STATE_TOMBSTONING_SUSPENDED_FAILED: Final = "tombstoning_suspended_failed"
"""How many document indexes the latest such poll could not fetch."""

ANOMALY_GENERATION_REGRESSED: Final = "generation_regressed"
ANOMALY_EMPTY_ROOT: Final = "empty_root"
ANOMALY_MASS_DELETE: Final = "mass_delete"
ANOMALY_PROTOCOL: Final = "protocol"

MAX_CONSECUTIVE_FAILURES: Final = 3
MAX_JSON_BYTES: Final = 8 * 1024 * 1024
"""Largest ``.metadata``/``.content`` parsed for the documents row; larger is left unparsed."""
MAX_NAME_CHARS: Final = 1024
MAX_PAGES: Final = 100_000
_DOCUMENT_TYPES: Final = frozenset({"DocumentType", "CollectionType"})
_BEGINNING: Final = datetime(1, 1, 1, tzinfo=UTC)
_LIST_PAGE: Final = 1000
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
    sync_max_poll_bytes: int = 2 * 1024**3
    """``SCRIPTORIUM_SYNC_MAX_POLL_BYTES``: blob bytes one poll may download (2 GiB)."""
    sync_max_poll_seconds: int = 20 * 60
    """``SCRIPTORIUM_SYNC_MAX_POLL_SECONDS``: how long one poll may keep starting work."""

    def __post_init__(self) -> None:
        if self.tombstone_max_per_poll < 0:
            raise ValueError("tombstone_max_per_poll must not be negative")
        if not 1 <= self.sync_workers <= 64:
            raise ValueError("sync_workers must be between 1 and 64")
        if self.sync_max_poll_bytes <= 0:
            raise ValueError("sync_max_poll_bytes must be positive")
        if self.sync_max_poll_seconds <= 0:
            raise ValueError("sync_max_poll_seconds must be positive")


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
    mass_delete_approved: bool = False
    """This poll's plan used an administrator's ``allow_mass_delete`` approval to pass the
    breaker (the approval covered every tripped candidate and had not expired)."""


class _BudgetSpentError(Exception):
    """Raised before starting work once the poll's byte or time budget is spent."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Stop:
    """Why a poll ended early: ``backoff``, ``token_rejected``, or ``partial`` when a budget
    is spent. Returned, never raised."""

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
    mass_delete_approved: bool = False


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
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._archive = archive
        self._index = index
        self._settings = settings
        self._clock = clock
        self._planner = planner if planner is not None else default_planner()
        self._monotonic = monotonic
        self._lock = threading.Lock()  # the run's download counters, across blob workers

    # ---- the poll ------------------------------------------------------------------------

    def run(self) -> SyncReport:
        run = _Run(started_at=self._clock(), started_mono=self._monotonic())
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
        except (CloudProtocolError, BlobVerificationError) as exc:
            return self._raise_anomaly(run, ANOMALY_PROTOCOL, f"root: {exc.reason}")
        except CloudError as exc:  # CloudNotFoundError, CloudRequestError: not a parse failure
            return self._finish(run, "backoff", reason=exc.reason)
        run.root_hash, run.generation = root.hash, root.generation
        run.rejected_entries = root.rejected

        previous = _load_listing(self._index.get_state(STATE_PREVIOUS_LISTING))
        last_generation = _parse_int(self._index.get_state(STATE_LAST_GENERATION))
        approval = self._index.get_state(STATE_ALLOW_MASS_DELETE)
        library, tombstoned = self._library_sets()
        uuids = frozenset(entry.uuid for entry in root.entries)
        run.documents_listed = len(uuids)

        # The planner's rules 1 and 2 need no listing, so a regressed or emptied root is
        # refused before a single index is fetched. That root is saved as an incomplete
        # listing: after an administrator clears the anomaly the next poll compares against
        # it, and it can never count as one of the two complete listings a deletion needs.
        unlisted = Listing(uuids=uuids, complete=False, generation=root.generation)
        early: str | None = None
        if last_generation is not None and root.generation < last_generation:
            early = ANOMALY_GENERATION_REGRESSED
        else:
            early = self._planner(
                previous, unlisted, library, tombstoned, self._settings.tombstone_max_per_poll
            ).anomaly
        if early is not None:
            if early == ANOMALY_GENERATION_REGRESSED:
                seen = last_generation if last_generation is not None else _generation(previous)
                why = f"root generation {root.generation} is lower than {seen}"
            else:
                why = f"root lists {len(uuids)} documents; the archive holds {len(library)}"
            self._save_listing(unlisted, root_hash=None)
            return self._raise_anomaly(run, early, why)

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
        # The listing ran to its end and is incomplete only because documents failed.
        listing_failures = len(run.failed) if stop is None and not complete else 0

        spent = False
        if stop is None:
            max_per_poll = self._settings.tombstone_max_per_poll
            plan = self._planner(previous, current, library, tombstoned, max_per_poll)
            # The planner ran: an approval is used now or never (step 4 in the docstring).
            spent = approval is not None
            if approval is not None:
                rejected = _approval_rejected(
                    approval, plan, generation=root.generation, now=self._clock()
                )
                if rejected is None:
                    plan = self._planner(
                        previous, current, library, tombstoned, max_per_poll, allow_mass_delete=True
                    )
                    run.mass_delete_approved = plan.anomaly is None
                    log_event(
                        logger,
                        "sync.mass_delete_approved",
                        level=logging.WARNING,
                        acct=self._settings.acct,
                        count=len(plan.to_tombstone),
                    )
                else:
                    log_event(
                        logger,
                        "sync.allow_mass_delete_rejected",
                        level=logging.WARNING,
                        acct=self._settings.acct,
                        reason=rejected,
                        count=len(plan.tripped),
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
                if plan.to_tombstone and stop is None:
                    self._tombstone(plan.to_tombstone, run)

        fully_done = complete and stop is None and not run.failed and run.anomaly is None
        self._save_listing(
            current,
            root_hash=root.hash if fully_done else None,
            listing_failures=listing_failures,
            started_at=run.started_at,
            consume_approval=spent,
        )

        if run.anomaly is not None:
            return self._finish(run, "anomaly", reason=run.anomaly)
        if stop is not None:
            return self._finish(run, stop.outcome, reason=stop.reason, retry=stop.retry_after_s)
        if run.failed or run.skipped_files or run.rejected_entries or run.metadata_errors:
            return self._finish(run, "partial", reason=_first_reason(run))
        return self._finish(run, "ok")

    def _library_sets(self) -> tuple[frozenset[str], frozenset[str]]:
        """``(library, tombstoned)``: every document row, split on ``deleted_at``.

        Pages through ``Index.recent`` from the beginning of time, which lists tombstoned
        rows too; §3.4 has no other way to enumerate the documents.
        """
        live: set[str] = set()
        dead: set[str] = set()
        cursor: str | None = None
        while True:
            page = self._index.recent(_BEGINNING, cursor, _LIST_PAGE)
            for row in page.items:
                (live if row.deleted_at is None else dead).add(row.uuid)
            cursor = page.next_cursor
            if cursor is None:
                return frozenset(live), frozenset(dead)

    def _save_listing(
        self,
        listing: Listing,
        *,
        root_hash: str | None,
        listing_failures: int = 0,
        started_at: datetime | None = None,
        consume_approval: bool = False,
    ) -> None:
        """Record this poll's listing for the next one; consume the approval if told to.

        A complete listing clears the ``tombstoning_suspended_*`` keys; one that failed on
        ``listing_failures`` documents sets them (keeping the first poll's time).
        """
        with self._index.transaction():
            if listing.complete:
                self._index.set_state(STATE_TOMBSTONING_SUSPENDED_SINCE, None)
                self._index.set_state(STATE_TOMBSTONING_SUSPENDED_FAILED, None)
            elif listing_failures and started_at is not None:
                if self._index.get_state(STATE_TOMBSTONING_SUSPENDED_SINCE) is None:
                    since = to_db_time(started_at)
                    self._index.set_state(STATE_TOMBSTONING_SUSPENDED_SINCE, since)
                    log_event(
                        logger,
                        "sync.tombstoning_suspended",
                        level=logging.WARNING,
                        acct=self._settings.acct,
                        count=listing_failures,
                    )
                self._index.set_state(STATE_TOMBSTONING_SUSPENDED_FAILED, str(listing_failures))
            self._index.set_state(STATE_LAST_GENERATION, str(listing.generation))
            self._index.set_state(STATE_PREVIOUS_LISTING, _dump_listing(listing))
            ok = "true" if listing.complete else "false"
            self._index.set_state(STATE_LAST_FULL_LISTING_OK, ok)
            self._index.set_state(STATE_LAST_ROOT_HASH, root_hash)
            if consume_approval:
                self._index.set_state(STATE_ALLOW_MASS_DELETE, None)

    # ---- step 2 --------------------------------------------------------------------------

    def _list(self, root: RootInfo, indexes: dict[str, DocumentIndex], run: _Run) -> _Stop | None:
        transient = _Streak()
        for entry in sorted(root.entries, key=lambda e: e.uuid):
            row = self._index.document(entry.uuid)
            if row is not None and row.hash_root == entry.hash:
                run.documents_unchanged += 1
                continue
            try:
                self._check_budget(run)
            except _BudgetSpentError as exc:
                return _Stop("partial", exc.reason)
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
                self._check_budget(run)
                self._commit_one(root, entries[uuid], indexes[uuid], run)
            except _BudgetSpentError as exc:
                # Not a failure of this document: it is left for the next poll, and the
                # blobs it already fetched stay in blobs/.
                return _Stop("partial", exc.reason)
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
            except Exception as exc:
                # A bug, or cloud data no check anticipated: fail this document, not the
                # account. Without this one bad document would end every poll at the same
                # place and keep every document after it from ever committing.
                self._fail(run, uuid, "internal_error", refused=True)
                log_event(
                    logger,
                    "sync.document_error",
                    level=logging.ERROR,
                    acct=self._settings.acct,
                    reason=type(exc).__name__,
                )
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
            body_sha: str | None = None
            if doc_index.body:
                # This version's file list, kept like any blob (§6). Its content address is
                # SHA-256 of the body, not hash_root (a sync 1.5 index is addressed by the
                # hash of its entries), so the history row records it.
                body_sha = hashlib.sha256(doc_index.body).hexdigest()
                self._archive.put_blob(body_sha, io.BytesIO(doc_index.body))
            seen_at = self._clock()
            row = self._document_row(entry, doc_index, root.generation, seen_at, run)
            with self._index.transaction():
                self._index.append_history(
                    entry.uuid, root.generation, entry.hash, seen_at, index_sha256=body_sha
                )
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
                self._fetch_one(sha, name, size, run)
            return
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sync-blob") as pool:
            futures = [
                pool.submit(self._fetch_one, sha, name, size, run) for sha, name, size in missing
            ]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                for future in futures:
                    future.cancel()
                raise

    def _fetch_one(self, sha256: str, name: str, size: int, run: _Run) -> None:
        """Download one blob into ``blobs/`` (``size`` is a cap) and count what it took."""
        self._check_budget(run)
        with self._client.open_blob(sha256, max_bytes=size, filename=name) as stream:
            path = self._archive.put_blob(sha256, stream)
        actual = path.stat().st_size
        with self._lock:
            run.blobs_downloaded += 1
            run.bytes_downloaded += actual

    def _check_budget(self, run: _Run) -> None:
        """Raise :class:`_BudgetSpentError` once this poll's bytes or time are used up."""
        with self._lock:
            spent = run.bytes_downloaded
        if spent >= self._settings.sync_max_poll_bytes:
            raise _BudgetSpentError("byte_budget")
        if self._monotonic() - run.started_mono >= self._settings.sync_max_poll_seconds:
            raise _BudgetSpentError("time_budget")

    # ---- the documents row -----------------------------------------------------------------

    def _document_row(
        self,
        entry: RootEntry,
        doc_index: DocumentIndex,
        generation: int,
        seen_at: datetime,
        run: _Run,
    ) -> DocumentRow:
        uuid = entry.uuid
        files = doc_index.files
        metadata = self._read_json(files.get(f"{uuid}.metadata"), run, required=True)
        content = self._read_json(files.get(f"{uuid}.content"), run, required=False)
        doc_type = metadata.get("type")
        if not isinstance(doc_type, str) or doc_type not in _DOCUMENT_TYPES:
            doc_type = "DocumentType"
        source_kind, page_count = _content_facts(uuid, doc_type, content, files)
        return DocumentRow(
            uuid=uuid,
            type=doc_type,
            parent=_parent(metadata.get("parent")),
            name=_display_name(metadata.get("visibleName"), fallback=uuid),
            last_modified=_epoch_ms(metadata.get("lastModified")) or seen_at,
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
            mass_delete_approved=run.mass_delete_approved,
        )
        log_event(
            logger,
            "sync.poll",
            acct=self._settings.acct,
            outcome=outcome,
            reason=reason,
            count=len(run.committed),
            tombstoned=len(run.tombstoned),
            untombstoned=len(run.untombstoned),
            bytes=run.bytes_downloaded,
            duration_ms=round((self._monotonic() - run.started_mono) * 1000, 1),
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


def _generation(listing: Listing | None) -> int | None:
    return None if listing is None else listing.generation


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


def mass_delete_approval(
    candidates: frozenset[str], *, root_generation: int, expires_at: datetime
) -> str:
    """The ``sync_state.allow_mass_delete`` value approving one breaker trip.

    ``candidates`` are the tripped plan's candidates (``TombstonePlan.tripped``) and
    ``root_generation`` the generation of the root that tripped it; the approval passes the
    breaker only for a plan whose tripped candidates are all in ``candidates``, until
    ``expires_at``.
    """
    return json.dumps(
        {
            "candidates": sorted(candidates),
            "root_generation": root_generation,
            "expires_at": to_db_time(expires_at),
        },
        separators=(",", ":"),
    )


def _approval_rejected(
    raw: str, plan: TombstonePlan, *, generation: int, now: datetime
) -> str | None:
    """None if the stored approval lets ``plan``'s tripped candidates pass the breaker; else
    a reason code for the log (never the candidates themselves)."""
    if plan.anomaly != ANOMALY_MASS_DELETE:
        return "unused"
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return "malformed"
    if not isinstance(data, dict):
        return "malformed"
    candidates = data.get("candidates")
    root_generation = data.get("root_generation")
    expires_at = data.get("expires_at")
    if not isinstance(candidates, list) or not all(
        isinstance(u, str) and is_uuid(u) for u in candidates
    ):
        return "malformed"
    if type(root_generation) is not int or root_generation < 0:
        return "malformed"
    if not isinstance(expires_at, str):
        return "malformed"
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return "malformed"
    if expiry.tzinfo is None:
        return "malformed"
    if now >= expiry:
        return "expired"
    if generation < root_generation:
        return "generation"
    if not plan.tripped or not plan.tripped <= frozenset(candidates):
        return "not_covered"
    return None


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
) -> tuple[str | None, int]:
    """``(source_kind, page_count)`` from ``.content``, falling back on the file list."""
    if doc_type == "CollectionType":
        return None, 0
    kind = content.get("fileType")
    if not isinstance(kind, str) or kind not in _FILE_TYPES:
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
