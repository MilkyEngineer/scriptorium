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
   **Scope** (M2, §11.1, ``cloud/scope.py``). Unless the account's scope is ``all``, every
   fetched document then has its ``.metadata`` and ``.content`` read (into memory, budget
   counted) for its type, parent and tags, and is decided against the scope with the tags
   of the folders above it. A document outside the scope is not downloaded: its row is
   upserted from ``.metadata`` with ``excluded_at`` set, and ``sync_state.scope_excluded``
   remembers its root entry hash and tags so an unchanged excluded document is not fetched
   again. A document already archived that leaves the scope keeps its files and
   ``hash_root``; only ``excluded_at`` is set. One that enters the scope is committed like a
   new document (or just unmarked if its archived version is current). Folders are always
   mirrored, but only folder-shaped ones: an item typed ``CollectionType`` whose index (or,
   when it was not fetched, whose archived version) holds more than its ``.metadata`` and
   ``.content`` is decided like a document, and an excluded row keeps the cloud's type.
   Every listed
   document is decided again when the scope changed since the last fully processed poll
   (``sync_state.scope_applied``, set to a pending marker as soon as a change is seen so an
   interrupted transition is redone whole), when a folder changed, or when a document was
   un-tombstoned; otherwise only the fetched ones are. Nothing here
   tombstones or removes anything. An unreadable stored scope ends the poll with
   ``backoff`` and reason ``scope_malformed`` before any cloud request: guessing ``all``
   would download what the user excluded. The decision fails closed: a document whose own
   ``.content`` is absent or could not be read whole (``scope_tags_unknown``), or below a
   folder whose
   current version this poll does not know (``scope_ancestor_unknown``), and that no known
   tag settles, fails for this poll: nothing of it is downloaded and its row is left as it
   was.
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
   before planning (during the listing, or by an early anomaly) leaves it in place. When
   the breaker trips, its candidates are stored as ``sync_state.anomaly_candidates`` (a
   JSON list of UUIDs, logged only as a count) for ``admin_clear_anomaly`` to approve.
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
account reach its second consecutive listing. The shortcut is not taken when the scope
changed since that poll.

**Archive repair.** When the archive rolls an interrupted change forward and a file it was
placing has vanished (``archive_journal_source_missing``), ``library/`` may lack part of the
version ``hash_root`` names. Such documents (``Archive.take_damaged``) get ``hash_root``
nulled, at the start and at the end of each poll, so the next poll commits them again from
``blobs/``.

Rendering (step 5) and OCR (step 6) belong to the worker.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import logging
import secrets
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple

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
    is_sha256,
    is_uuid,
)
from scriptorium.cloud.scope import (
    ALL,
    MAX_TAGS_PER_DOCUMENT,
    NOT_A_FOLDER,
    Known,
    ScopeError,
    SyncScope,
    ancestor_tags,
    decide,
    load_scope,
    normalise_tag,
    scope_fingerprint,
    scope_tags,
)
from scriptorium.db.connect import to_db_time
from scriptorium.errors import PathEscapeError, ScriptoriumError
from scriptorium.library.tags import TagSet, parse_tags
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
STATE_ANOMALY_CANDIDATES: Final = "anomaly_candidates"
"""With ``anomaly="mass_delete"``: JSON list of the UUIDs that tripped the breaker. Never
logged; ``admin_clear_anomaly`` turns it into an approval."""
STATE_SCOPE_APPLIED: Final = "scope_applied"
"""Fingerprint of the scope the last fully processed poll applied (absent means ``all``), or
:data:`SCOPE_PENDING` while a change is being applied."""
SCOPE_PENDING: Final = "pending"
"""``scope_applied`` while a scope change is under way: equal to no fingerprint, so every poll
until one finishes cleanly decides every listed document again."""
STATE_SCOPE_EXCLUDED: Final = "scope_excluded"
"""JSON ``{uuid: [entry_hash, [tag, ...]]}`` for excluded documents: their root entry hash and
document tags when last looked at, so an unchanged one is not fetched on every poll. Kept only
for a document whose ``.metadata`` and whole tag list were read (at most
``MAX_TAGS_PER_DOCUMENT`` tags); any other is fetched again."""

ANOMALY_GENERATION_REGRESSED: Final = "generation_regressed"
ANOMALY_EMPTY_ROOT: Final = "empty_root"
ANOMALY_MASS_DELETE: Final = "mass_delete"
ANOMALY_PROTOCOL: Final = "protocol"

MAX_CONSECUTIVE_FAILURES: Final = 3
MAX_JSON_BYTES: Final = 8 * 1024 * 1024
"""Largest ``.metadata``/``.content`` parsed for the documents row; larger is left unparsed."""
MAX_NAME_CHARS: Final = 1024
MAX_PAGES: Final = 100_000
MAX_SEEN_CHARS: Final = 64 * 1024 * 1024
"""Largest ``scope_excluded`` value read back; a larger one is ignored (and rebuilt)."""
MAX_SKIPPED_SHAPES: Final = 8
"""Most distinct skipped-name shapes one poll logs (``sync.skipped_files``)."""
_DOCUMENT_TYPES: Final = frozenset({"DocumentType", "CollectionType"})
_ALL_FINGERPRINT: Final = scope_fingerprint(ALL)
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
    excluded: int = 0
    """Listed documents outside the sync scope after this poll (rows with ``excluded_at``)."""
    newly_excluded: tuple[str, ...] = ()
    """Documents already in the index that this poll moved out of the scope: the worker
    lowers their waiting renders."""
    included: tuple[str, ...] = ()
    """Excluded documents this poll let back in by unmarking them (their archived version is
    current, so nothing was committed): the worker refreshes their pages and tags."""


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
    excluded: int = 0
    newly_excluded: list[str] = field(default_factory=list)
    included: list[str] = field(default_factory=list)
    #: protocol.name_shape -> documents whose index left out a name of that shape (bounded).
    skipped_shapes: Counter[str] = field(default_factory=Counter)


class _Seen(NamedTuple):
    """An excluded document as last looked at: its root entry hash and all its document tags
    (its parent is the row's, from the same ``.metadata``)."""

    hash: str
    tags: frozenset[str]


@dataclass(frozen=True)
class _Facts:
    """What the scope needs from a fetched document's ``.metadata`` and ``.content``."""

    metadata: Mapping[str, Any]
    metadata_ok: bool
    tags: frozenset[str] | None  # None: ``.content`` absent or not readable whole
    content: Mapping[str, Any] | None = None  # the parsed ``.content``, for the tag index

    @property
    def folder(self) -> bool:
        return self.metadata.get("type") == "CollectionType"

    @property
    def parent(self) -> str | None:
        return _parent(self.metadata.get("parent"))


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
        self._repair_damaged()  # local only: recovery at worker start may have left damage
        anomaly = self._index.get_state(STATE_ANOMALY)
        if anomaly:
            run.anomaly = anomaly
            run.anomaly_reason = self._index.get_state(STATE_ANOMALY_REASON)
            return self._finish(run, "anomaly", reason=anomaly)
        try:
            scope = load_scope(self._index)
        except ScopeError:
            log_event(logger, "sync.scope_malformed", level=logging.ERROR, acct=self._settings.acct)
            return self._finish(run, "backoff", reason="scope_malformed")
        fingerprint = scope_fingerprint(scope)
        applied = self._index.get_state(STATE_SCOPE_APPLIED) or _ALL_FINGERPRINT
        scope_changed = fingerprint != applied
        if scope_changed and applied != SCOPE_PENDING:
            # Before anything is decided: a poll that ends part way leaves the marker, so the
            # next one decides every listed document again even if the scope changes back.
            with self._index.transaction():
                self._index.set_state(STATE_SCOPE_APPLIED, SCOPE_PENDING)

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
        marks = _load_seen(self._index.get_state(STATE_SCOPE_EXCLUDED))
        if root.hash == self._index.get_state(STATE_LAST_ROOT_HASH) and not scope_changed:
            run.root_unchanged = True
            run.documents_unchanged = len(uuids)
            complete = True
        else:
            stop = self._list(root, indexes, run, scope, marks)
            complete = stop is None and not run.failed
        run.listing_complete = complete
        current = Listing(uuids=uuids, complete=complete, generation=root.generation)
        # The listing ran to its end and is incomplete only because documents failed.
        listing_failures = len(run.failed) if stop is None and not complete else 0

        spent: str | None = None
        if stop is None:
            max_per_poll = self._settings.tombstone_max_per_poll
            plan = self._planner(previous, current, library, tombstoned, max_per_poll)
            # The planner ran: an approval is used now or never (step 4 in the docstring).
            spent = approval
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
                    candidates=plan.tripped if plan.anomaly == ANOMALY_MASS_DELETE else None,
                )
            else:
                # Step 4a, un-tombstone first; then the scope; then step 3; then step 4b.
                blocked = self._untombstone(plan.to_untombstone, run)
                entries = {entry.uuid: entry for entry in root.entries}
                stop = self._apply_scope(
                    scope, scope_changed, marks, entries, indexes, blocked, run
                )
                if stop is None:
                    stop = self._commit_all(root, entries, indexes, blocked, run)
                if plan.to_tombstone and stop is None:
                    self._tombstone(plan.to_tombstone, run)

        fully_done = complete and stop is None and not run.failed and run.anomaly is None
        run.excluded = len(self._index.document_uuids(deleted=False, excluded=True) & uuids)
        self._save_listing(
            current,
            root_hash=root.hash if fully_done else None,
            listing_failures=listing_failures,
            started_at=run.started_at,
            consume_approval=spent,
            scope_applied=fingerprint if fully_done else None,
        )
        # After the listing is saved, so a repair also clears the root hash just stored.
        self._repair_damaged(skip=frozenset(run.committed))

        if run.anomaly is not None:
            return self._finish(run, "anomaly", reason=run.anomaly)
        if stop is not None:
            return self._finish(run, stop.outcome, reason=stop.reason, retry=stop.retry_after_s)
        if run.failed or run.skipped_files or run.rejected_entries or run.metadata_errors:
            return self._finish(run, "partial", reason=_first_reason(run))
        return self._finish(run, "ok")

    def _library_sets(self) -> tuple[frozenset[str], frozenset[str]]:
        """``(library, tombstoned)``: every document row, split on ``deleted_at``. Excluded
        rows are in ``library``: a document deleted in the cloud is tombstoned whether or not
        it was in scope (its row, and any version archived before it left, are kept)."""
        return (
            self._index.document_uuids(deleted=False),
            self._index.document_uuids(deleted=True),
        )

    def _save_listing(
        self,
        listing: Listing,
        *,
        root_hash: str | None,
        listing_failures: int = 0,
        started_at: datetime | None = None,
        consume_approval: str | None = None,
        scope_applied: str | None = None,
    ) -> None:
        """Record this poll's listing for the next one; consume the approval if told to.

        ``consume_approval`` is the approval this poll read and planned with: it is removed
        only if it is still the stored one, so an approval an administrator wrote while this
        poll ran is left for the next poll.

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
            if scope_applied is not None:
                self._index.set_state(STATE_SCOPE_APPLIED, scope_applied)
            if (
                consume_approval is not None
                and self._index.get_state(STATE_ALLOW_MASS_DELETE) == consume_approval
            ):
                self._index.set_state(STATE_ALLOW_MASS_DELETE, None)

    # ---- step 2 --------------------------------------------------------------------------

    def _list(
        self,
        root: RootInfo,
        indexes: dict[str, DocumentIndex],
        run: _Run,
        scope: SyncScope,
        seen: Mapping[str, _Seen],
    ) -> _Stop | None:
        wanted: list[RootEntry] = []
        for entry in sorted(root.entries, key=lambda e: e.uuid):
            row = self._index.document(entry.uuid)
            if row is not None and row.hash_root == entry.hash:
                run.documents_unchanged += 1
                continue
            mark = seen.get(entry.uuid)
            if (
                scope.mode != "all"
                and row is not None
                and row.excluded_at is not None
                and mark is not None
                and mark.hash == entry.hash
            ):
                # Outside the scope and unchanged since it was last looked at.
                run.documents_unchanged += 1
                continue
            wanted.append(entry)
        return self._fetch_indexes(wanted, indexes, run)

    def _fetch_indexes(
        self, wanted: list[RootEntry], indexes: dict[str, DocumentIndex], run: _Run
    ) -> _Stop | None:
        transient = _Streak()
        for entry in wanted:
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
            for shape in doc_index.skipped_shapes:
                if shape in run.skipped_shapes or len(run.skipped_shapes) < MAX_SKIPPED_SHAPES:
                    run.skipped_shapes[shape] += 1
        return None

    # ---- step 2, the scope -----------------------------------------------------------------

    def _apply_scope(
        self,
        scope: SyncScope,
        scope_changed: bool,
        seen: dict[str, _Seen],
        entries: Mapping[str, RootEntry],
        indexes: dict[str, DocumentIndex],
        blocked: set[str],
        run: _Run,
    ) -> _Stop | None:
        """Mark what is outside the scope and bring back what entered it (§11.1).

        A fetched document outside the scope leaves ``indexes``, so step 3 downloads nothing
        of it. An archived one that leaves keeps its files and ``hash_root``. An excluded one
        that enters is unmarked when its archived version is current, else its index is
        fetched so step 3 commits it. Nothing is tombstoned or removed.
        """
        facts: dict[str, _Facts] = {}
        try:
            if scope.mode != "all":
                stop = self._fetch_facts(indexes, blocked, facts, run)
                if stop is not None:
                    return stop
            listed = frozenset(entries)
            if scope.mode == "all":
                candidates = self._index.document_uuids(deleted=False, excluded=True) & listed
            elif scope_changed or run.untombstoned or any(f.folder for f in facts.values()):
                candidates = listed  # a folder's tags or the scope reach every document below
            else:
                candidates = frozenset(facts)
            skip = frozenset(blocked | set(run.failed))
            late = self._decide(
                scope, sorted(candidates - skip), facts, seen, entries, indexes, skip, run
            )
            return self._fetch_indexes([entries[uuid] for uuid in late], indexes, run)
        finally:
            self._save_seen(seen, frozenset(entries))

    def _decide(
        self,
        scope: SyncScope,
        candidates: list[str],
        facts: Mapping[str, _Facts],
        seen: dict[str, _Seen],
        entries: Mapping[str, RootEntry],
        indexes: dict[str, DocumentIndex],
        skip: frozenset[str],
        run: _Run,
    ) -> list[str]:
        """Apply the scope to ``candidates``; returns the excluded documents that entered the
        scope with a version newer than the one archived (their index is still to fetch).

        A candidate that the known tags do not settle fails for this poll: it leaves
        ``indexes`` (nothing of it is downloaded) and its row is left as it was."""
        rows: dict[str, DocumentRow | None] = {}
        known: dict[str, Known | None] = {}

        def row_of(uuid: str) -> DocumentRow | None:
            if uuid not in rows:
                rows[uuid] = self._index.document(uuid)
            return rows[uuid]

        def lookup(uuid: str) -> Known | None:
            if uuid not in known:
                known[uuid] = self._known(uuid, row_of, facts, seen, entries, skip, run)
            return known[uuid]

        now = self._clock()
        late: list[str] = []
        for uuid in candidates:
            row, fact, entry = row_of(uuid), facts.get(uuid), entries[uuid]
            if row is not None and row.deleted_at is not None:
                continue  # blocked from returning: left as it is
            if fact is not None:
                folder = fact.folder
            else:
                folder = row is not None and row.type == "CollectionType"
            folder = folder and self._scope_exempt_folder(uuid, row, indexes.get(uuid))
            me: Known | None = None
            inside: bool | None = True
            if scope.mode != "all" and not folder:
                me = lookup(uuid)
                own = me.tags if me is not None else None
                if me is not None and me.parent_known:
                    above, complete = ancestor_tags(me.parent, lookup)
                else:
                    above, complete = frozenset(), False
                inside = decide(scope, own, above, complete=complete)
                if inside is None:
                    indexes.pop(uuid, None)
                    unknown = "scope_tags_unknown" if own is None else "scope_ancestor_unknown"
                    self._fail(run, uuid, unknown)
                    continue
            try:
                if inside:
                    seen.pop(uuid, None)
                    if uuid in indexes or row is None or row.excluded_at is None:
                        continue  # step 3 commits it, or it is archived and stays so
                    if row.hash_root == entry.hash:
                        self._put(dataclasses.replace(row, excluded_at=None))
                        run.included.append(uuid)  # the worker refreshes its pages and tags
                    else:
                        late.append(uuid)
                    continue
                if fact is not None:
                    indexes.pop(uuid, None)  # before the upsert: never commit it on a failure
                    self._put(self._excluded_row(entry, fact, row, now, run), _own_tags(fact))
                    if row is not None and row.excluded_at is None:
                        run.newly_excluded.append(uuid)
                elif row is not None and row.excluded_at is None:
                    self._put(dataclasses.replace(row, excluded_at=now))
                    run.newly_excluded.append(uuid)
                if (
                    me is not None
                    and me.parent_known
                    and me.tags is not None
                    and len(me.tags) <= MAX_TAGS_PER_DOCUMENT
                ):
                    seen[uuid] = _Seen(entry.hash, me.tags)
                else:
                    seen.pop(uuid, None)  # fetched again next poll
            except (ScriptoriumError, OSError) as exc:
                code = exc.code if isinstance(exc, ScriptoriumError) else "local_io"
                self._fail(run, uuid, code)
        return late

    def _known(
        self,
        uuid: str,
        row_of: Callable[[str], DocumentRow | None],
        facts: Mapping[str, _Facts],
        seen: Mapping[str, _Seen],
        entries: Mapping[str, RootEntry],
        skip: frozenset[str],
        run: _Run,
    ) -> Known | None:
        """What this poll knows of ``uuid``'s current version (its tags and parent), or None.

        Its ``.metadata``/``.content`` read this poll; else, for an excluded document not
        changed since it was last looked at, the remembered tags; else, when the root lists
        the version that is archived, the archived ``.content``. A document that failed or is
        blocked, or whose current version is none of these, is unknown. One the root does not
        list is no folder at all, unless the root had entries it could not read.
        """
        entry = entries.get(uuid)
        if entry is None:
            return NOT_A_FOLDER if run.rejected_entries == 0 else None
        if uuid in skip:
            return None
        fact = facts.get(uuid)
        if fact is not None:
            return Known(fact.tags, fact.parent, parent_known=fact.metadata_ok)
        row = row_of(uuid)
        if row is None or row.deleted_at is not None:
            return None
        mark = seen.get(uuid)
        if row.excluded_at is not None and mark is not None and mark.hash == entry.hash:
            return Known(mark.tags, row.parent)
        if row.hash_root == entry.hash:
            return Known(self._archived_tags(uuid), row.parent)
        return None

    def _put(self, row: DocumentRow, tags: TagSet | None = None) -> None:
        """Upsert ``row`` and, unless ``tags`` is None, replace its tags, in one transaction.

        Only an excluded row brings tags here: a committed or un-excluded document has its
        tags (and pages) replaced by the worker's refresh after the poll (§7 step 3)."""
        with self._index.transaction():
            self._index.upsert_document(row)
            if tags is not None:
                self._index.replace_tags(row.uuid, tags)

    def _fetch_facts(
        self,
        indexes: dict[str, DocumentIndex],
        blocked: set[str],
        facts: dict[str, _Facts],
        run: _Run,
    ) -> _Stop | None:
        """Read each fetched document's ``.metadata`` and ``.content`` into ``facts``.

        The bytes are held in memory only, so a document found outside the scope leaves
        nothing in ``blobs/``; one inside downloads them again with the rest in step 3. A
        document whose files cannot be fetched leaves ``indexes`` and fails.
        """
        transient = _Streak()
        for uuid in sorted(indexes):
            if uuid in blocked:
                continue
            files = indexes[uuid].files
            try:
                self._check_budget(run)
                meta = self._fetch_small(files.get(f"{uuid}.metadata"), f"{uuid}.metadata", run)
                content_ref = files.get(f"{uuid}.content")
                content = self._fetch_small(content_ref, f"{uuid}.content", run)
            except _BudgetSpentError as exc:
                return _Stop("partial", exc.reason)
            except (TokenRejectedError, RateLimitedError) as exc:
                indexes.pop(uuid)
                self._fail(run, uuid, exc.reason)
                return _Stop.from_error(exc)
            except TransientCloudError as exc:
                indexes.pop(uuid)
                self._fail(run, uuid, exc.reason)
                if transient.hit():
                    return _Stop.from_error(exc)
                continue
            except CloudError as exc:
                indexes.pop(uuid)
                self._fail(run, uuid, exc.reason)
                transient.reset()
                continue
            transient.reset()
            metadata = _parse_json(meta)
            parsed = _parse_json(content)
            # A ``.content`` absent from the index, too large or unparseable: the tags are
            # unknown (fail closed). Every real document and folder has one, so an index
            # without it is malformed or hostile, and "no tags" would let a deny scope in.
            facts[uuid] = _Facts(
                metadata=metadata or {},
                metadata_ok=metadata is not None,
                tags=scope_tags(parsed),
                content=parsed,
            )
        return None

    def _fetch_small(self, ref: BlobRef | None, name: str, run: _Run) -> bytes | None:
        """One small JSON file of a document, verified, into memory; None if absent or too
        large to parse (the caller treats that as unusable)."""
        if ref is None or ref.size > MAX_JSON_BYTES:
            return None
        self._check_budget(run)
        with self._client.open_blob(ref.sha256, max_bytes=ref.size, filename=name) as stream:
            data = stream.read()
        with self._lock:
            run.bytes_downloaded += len(data)
        return data

    def _archived_tags(self, uuid: str) -> frozenset[str] | None:
        """Document tags of the version in ``library/``; None if nothing usable is there (no
        ``.metadata``, no ``.content``, or one that cannot be read whole): as for a fetched
        index, a missing ``.content`` makes the tags unknown, not empty."""
        try:
            files = self._archive.current_files(uuid)
        except (ScriptoriumError, OSError):
            return None
        if f"{uuid}.metadata" not in files:
            return None
        path = files.get(f"{uuid}.content")
        if path is None:
            return None
        return scope_tags(_read_json_file(path))

    def _scope_exempt_folder(
        self, uuid: str, row: DocumentRow | None, index: DocumentIndex | None
    ) -> bool:
        """Whether an item whose ``.metadata`` (or row) says folder is mirrored whatever the
        scope. The type is the cloud's word, so only a folder-shaped one is: a fetched index
        that lists nothing but ``<uuid>.metadata`` and ``<uuid>.content``, or, unfetched, a
        row that was never excluded whose archived version holds nothing else. The archive is
        what counts, not the stored type: an item archived under ``all`` with pages is decided
        like a document once a scope is set. Anything else, or an archive that cannot be
        read, is decided like a document, so a "folder" cannot carry pages past the scope."""
        if index is not None:
            return set(index.files) <= _folder_files(uuid)
        if row is None or row.excluded_at is not None:
            return False
        try:
            files = self._archive.current_files(uuid)
        except (ScriptoriumError, OSError):
            return False
        return set(files) <= _folder_files(uuid)

    def _excluded_row(
        self,
        entry: RootEntry,
        fact: _Facts,
        row: DocumentRow | None,
        now: datetime,
        run: _Run,
    ) -> DocumentRow:
        """The row of a fetched document outside the scope: names and dates from its
        ``.metadata``; what describes an archived version (``hash_root``, ``generation``,
        ``source_kind``, ``page_count``) kept from the row, as that version stays."""
        excluded_at = row.excluded_at if row is not None and row.excluded_at else now
        if not fact.metadata_ok:
            run.metadata_errors += 1
            if row is not None:
                return dataclasses.replace(row, excluded_at=excluded_at)
        metadata = fact.metadata
        # The cloud's type, as a commit stores it: an item that says folder but carries files
        # keeps CollectionType (and is still decided like a document, see
        # _scope_exempt_folder), so the type does not flip when it enters the scope.
        doc_type = metadata.get("type")
        if not isinstance(doc_type, str) or doc_type not in _DOCUMENT_TYPES:
            doc_type = "DocumentType"
        return DocumentRow(
            uuid=entry.uuid,
            type=doc_type,
            parent=fact.parent,
            name=_display_name(metadata.get("visibleName"), fallback=entry.uuid),
            last_modified=_epoch_ms(metadata.get("lastModified")) or now,
            last_opened=_epoch_ms(metadata.get("lastOpened")),
            deleted_at=None,
            deletion_cause=None,
            source_kind=row.source_kind if row is not None else None,
            page_count=row.page_count if row is not None else 0,
            hash_root=row.hash_root if row is not None else None,
            generation=row.generation if row is not None else None,
            excluded_at=excluded_at,
        )

    def _save_seen(self, seen: dict[str, _Seen], listed: frozenset[str]) -> None:
        """Store ``scope_excluded``, keeping only listed documents that are still excluded."""
        excluded = self._index.document_uuids(deleted=False, excluded=True)
        keep = {u: seen[u] for u in sorted(seen) if u in listed and u in excluded}
        with self._index.transaction():
            self._index.set_state(STATE_SCOPE_EXCLUDED, _dump_seen(keep) if keep else None)

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
        data = _read_json_file(self._archive.blob_path(ref.sha256))
        if data is None:
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

    def _set_anomaly(
        self, run: _Run, kind: str, reason: str, *, candidates: frozenset[str] | None = None
    ) -> None:
        """Enter safe mode. ``candidates`` (a tripped breaker's) replace any stored ones;
        any other anomaly clears them, so an approval is only ever built from the latest
        trip. ``reason`` is returned to users and administrators, so it must be a code or a
        count (``accounts.is_anomaly_reason``), never cloud-supplied text: anything else is
        a programming error (:class:`ValueError`, raised before anything is stored)."""
        from scriptorium.accounts import is_anomaly_reason

        if not is_anomaly_reason(reason):
            raise ValueError("an anomaly reason must be a code or a count")
        stored = json.dumps(sorted(candidates), separators=(",", ":")) if candidates else None
        with self._index.transaction():
            self._index.set_state(STATE_ANOMALY, kind)
            self._index.set_state(STATE_ANOMALY_REASON, reason)
            self._index.set_state(STATE_ANOMALY_CANDIDATES, stored)
        run.anomaly, run.anomaly_reason = kind, reason
        log_event(
            logger,
            "sync.anomaly",
            level=logging.WARNING,
            acct=self._settings.acct,
            reason=kind,
            count=len(candidates) if candidates else None,
        )

    def _repair_damaged(self, *, skip: frozenset[str] = frozenset()) -> None:
        """Null ``hash_root`` of documents the archive reports damaged (module docstring),
        except those this poll has just committed whole again, and forget the root hash so
        the next poll lists every document instead of taking the unchanged-root shortcut."""
        damaged = self._archive.take_damaged() - skip
        if not damaged:
            return
        repaired = 0
        try:
            with self._index.transaction():
                for uuid in sorted(damaged):
                    row = self._index.document(uuid)
                    if row is not None and row.hash_root is not None:
                        self._index.upsert_document(dataclasses.replace(row, hash_root=None))
                        repaired += 1
                self._index.set_state(STATE_LAST_ROOT_HASH, None)
        except Exception as exc:
            log_event(
                logger,
                "sync.archive_repair_failed",
                level=logging.ERROR,
                acct=self._settings.acct,
                reason=type(exc).__name__,
                count=len(damaged),
            )
            return
        log_event(
            logger,
            "sync.archive_repaired",
            level=logging.WARNING,
            acct=self._settings.acct,
            count=repaired,
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
            excluded=run.excluded,
            newly_excluded=tuple(run.newly_excluded),
            included=tuple(run.included),
        )
        for shape, documents in sorted(run.skipped_shapes.items()):
            # The shape of names the allowlist left out (protocol.name_shape), never a name:
            # how a new file kind on the tablet shows up. count = documents with that shape.
            log_event(
                logger,
                "sync.skipped_files",
                level=logging.WARNING,
                acct=self._settings.acct,
                reason=f"suffix_{shape}",
                count=documents,
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


def _parse_json(raw: bytes | None) -> dict[str, Any] | None:
    """A JSON object from bytes; None if absent, too large or not an object."""
    if raw is None or len(raw) > MAX_JSON_BYTES:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _read_json_file(path: Path) -> dict[str, Any] | None:
    """A JSON object from a local file, read with the same bound; None if unusable."""
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_JSON_BYTES + 1)
    except OSError:
        return None
    return _parse_json(raw)


def _load_seen(raw: str | None) -> dict[str, _Seen]:
    """``scope_excluded`` as stored; malformed entries (or a malformed whole) are dropped,
    which only costs a fetch of those documents' small files."""
    if raw is None or len(raw) > MAX_SEEN_CHARS:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, _Seen] = {}
    for uuid, value in data.items():
        if not (isinstance(uuid, str) and is_uuid(uuid)) or not isinstance(value, list):
            continue
        if len(value) != 2 or not isinstance(value[0], str) or not is_sha256(value[0]):
            continue
        tags = value[1]
        if not isinstance(tags, list) or len(tags) > MAX_TAGS_PER_DOCUMENT:
            continue
        if not all(isinstance(t, str) and normalise_tag(t) == t for t in tags):
            continue  # an entry with a dropped tag could let a document in: drop it whole
        out[uuid] = _Seen(value[0], frozenset(tags))
    return out


def _dump_seen(seen: Mapping[str, _Seen]) -> str:
    return json.dumps(
        {uuid: [mark.hash, sorted(mark.tags)] for uuid, mark in sorted(seen.items())},
        ensure_ascii=False,
        separators=(",", ":"),
    )


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


def _own_tags(fact: _Facts) -> TagSet | None:
    """The tag index rows of an excluded document: its document tags from the ``.content``
    read for the scope (bounded and normalised by ``parse_tags``). Its page tags are dropped,
    since its pages are not known; None (leave the stored tags) when the ``.content`` was not
    read."""
    if fact.content is None:
        return None
    return TagSet(document=parse_tags(fact.content).document)


def _folder_files(uuid: str) -> frozenset[str]:
    """The only files a folder-shaped item has."""
    return frozenset({f"{uuid}.metadata", f"{uuid}.content"})


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
