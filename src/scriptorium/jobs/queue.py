"""The durable job queue in ``scriptorium.sqlite`` (Architecture §6.2; M1 plan §3.5).

The server enqueues, the worker leases and runs. Life of a job::

    queued ──lease──▶ leased ──complete──▶ done
      ▲  │              │ └──fail (attempts < max)──▶ failed ──(next_run_at)──lease──▶ leased
      │  └─cancel─▶ cancelled                              └─cancel─▶ cancelled
      │                 ├──fail (attempts ≥ max)──▶ dead
      ├─requeue_expired─┤ (lease expired; ``dead`` instead once attempts ≥ max)
      └──────defer──────┘ (nothing could be done now; the lease's attempt is given back)

- ``attempts`` counts leases, so a job that crashes its worker (lease expires, never failed)
  still runs out of attempts instead of looping forever.
- ``fail`` backs off ``min(2**attempts * 30 s, 1 h)`` (:func:`backoff_seconds`).
- ``defer`` puts a leased job back to ``queued`` until a given time because the work cannot
  be done *now* for a reason outside the job (no OCR key, a cap reached, a key the API
  refused): it is not an attempt, so ``attempts`` is restored, and the job keeps its key and
  its place, so a deferral never mints an ``:after:`` re-run. The reason is kept in ``error``
  as ``deferred:<reason>`` (:func:`is_deferred`) until the job is leased and reported again;
  ``wake`` / ``wake_deferred`` make deferred jobs runnable at once when the situation
  changes (a key stored, a grant).
- **Dedupe convention.** ``dedupe_key`` is unique across the table for all time. ``enqueue``
  with a key that exists returns that row, whatever its state, and creates nothing. Keys
  therefore name *the work*, including the version it applies to (``ocr:<acct>:<page>:<hash>``,
  ``push:<acct>:<uuid>``, ``render:<acct>:<page>:<hash>``); a new version is a new key, and a
  caller that deliberately re-runs finished work passes a new key (e.g. with a ``:retry-<n>``
  suffix). A key held by another user is refused rather than returned.
- ``cancel`` succeeds only for the owning ``uid`` and only while the job is not running
  (``queued``, or ``failed`` waiting for its retry).
- ``record_dead`` writes a job straight into ``dead``, for a verdict reached without one
  (an OCR batch item the API refused), so its key names finished work like any other.
- **Priority** (M2, §6.2). ``lease`` takes the runnable job with the highest ``priority``,
  then the oldest: ``ORDER BY priority DESC, created_at, id``. Everything is enqueued at
  :data:`PRIORITY_NORMAL` (0) except renders of tombstoned, trashed or excluded documents
  (:data:`PRIORITY_LOW`, -1), and :meth:`JobQueue.deprioritise` lowers a document's waiting
  jobs when it is tombstoned, so a large deleted document never delays live pages. An
  ``enqueue`` that finds its job still waiting at a lower priority raises it (a user asking
  for a page of a deleted document is not kept behind the whole queue); nothing else ever
  raises one.

Times come from an injected clock (``Callable[[], datetime]``, timezone-aware) so tests are
deterministic. Nothing here logs payloads, results or errors: they may carry identifiers the
caller chose, and the ``error`` text is for ``*_status`` tools only.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from scriptorium.db.connect import from_db_time, open_sqlite, opt_from_db_time, to_db_time, utc_now
from scriptorium.db.migrations import SchemaMismatch, Step, check_version
from scriptorium.db.migrations import migrate as run_migrations
from scriptorium.db.schema_global import SCRIPTORIUM_STEPS
from scriptorium.errors import InvalidArgumentError, RefusedError
from scriptorium.jobs.kinds import (
    AFTER,
    GONE_RESULT,
    KEY_ERROR_PREFIX,
    RENDER_KINDS,
    RERUN_CAP,
    is_gone,
    is_render_verdict,
    rerun_family,
    rerun_key,
)

STATES: Final = frozenset({"queued", "leased", "done", "failed", "dead", "cancelled"})
BACKOFF_BASE_S: Final = 30
BACKOFF_CAP_S: Final = 3600
MAX_PAYLOAD_BYTES: Final = 64 * 1024
MAX_RESULT_BYTES: Final = 64 * 1024
MAX_ERROR_CHARS: Final = 1024
LEASE_EXPIRED_ERROR: Final = "lease expired"
PRIORITY_NORMAL: Final = 0
PRIORITY_LOW: Final = -1
"""Renders of tombstoned, trashed or excluded documents: leased after every normal job."""
_PRIORITIES: Final = range(-10, 11)
DEFERRED_PREFIX: Final = "deferred:"
"""``error`` of a job :meth:`JobQueue.defer` put back: ``deferred:<reason>``."""
_DEFER_REASON: Final = re.compile(r"[a-z_]{1,40}")


class LeaseLostError(RefusedError):
    """The caller no longer holds the lease (it expired and was re-queued, or the job moved on).

    The worker must stop working on the job and must not report a result for it.
    """


@dataclass(frozen=True, slots=True)
class JobRow:
    id: int
    uid: str
    acct: str | None
    kind: str
    dedupe_key: str
    payload: Mapping[str, Any]
    state: str
    lease_owner: str | None
    lease_expires_at: datetime | None
    attempts: int
    max_attempts: int
    next_run_at: datetime
    result: Mapping[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    priority: int = PRIORITY_NORMAL


def is_deferred(job: JobRow) -> bool:
    """A job :meth:`JobQueue.defer` put back, still waiting (``queued``, ``deferred:…``)."""
    return job.state == "queued" and (job.error or "").startswith(DEFERRED_PREFIX)


def deferral_reason(job: JobRow) -> str | None:
    """The reason code of a deferred job, None for any other job."""
    if not is_deferred(job):
        return None
    assert job.error is not None
    return job.error[len(DEFERRED_PREFIX) :]


def backoff_seconds(attempts: int) -> int:
    """``min(2**attempts * 30, 3600)`` seconds; ``attempts`` is the count after the failed try."""
    if attempts < 0:
        raise ValueError("attempts is negative")
    if attempts >= 7:  # 2**7 * 30 = 3840 > 3600; avoid huge powers for large counts
        return BACKOFF_CAP_S
    return min(BACKOFF_BASE_S << attempts, BACKOFF_CAP_S)


def _json(value: Mapping[str, Any], cap: int, what: str) -> str:
    text = json.dumps(dict(value), separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    if len(text.encode("utf-8")) > cap:
        raise InvalidArgumentError(f"The job {what} is too large.")
    return text


def _row(r: sqlite3.Row) -> JobRow:
    return JobRow(
        id=r["id"],
        uid=r["uid"],
        acct=r["acct"],
        kind=r["kind"],
        dedupe_key=r["dedupe_key"],
        payload=json.loads(r["payload"]),
        state=r["state"],
        lease_owner=r["lease_owner"],
        lease_expires_at=opt_from_db_time(r["lease_expires_at"]),
        attempts=r["attempts"],
        max_attempts=r["max_attempts"],
        next_run_at=from_db_time(r["next_run_at"]),
        result=None if r["result"] is None else json.loads(r["result"]),
        error=r["error"],
        created_at=from_db_time(r["created_at"]),
        updated_at=from_db_time(r["updated_at"]),
        priority=r["priority"],
    )


def _check_priority(priority: int) -> int:
    if isinstance(priority, bool) or not isinstance(priority, int) or priority not in _PRIORITIES:
        raise InvalidArgumentError("priority must be an integer from -10 to 10.")
    return priority


class JobQueue:
    """Jobs in ``scriptorium.sqlite``. Thread-safe; one connection per instance."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Callable[[], datetime] = utc_now):
        self._conn = conn
        self._clock = clock
        self._lock = threading.RLock()

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        migrate: bool,
        clock: Callable[[], datetime] = utc_now,
        steps: Sequence[Step] = SCRIPTORIUM_STEPS,
    ) -> JobQueue:
        """Open ``path`` (normally ``/data/scriptorium.sqlite``).

        ``steps`` is the schema sequence of the whole file, by default the composed
        ``scriptorium.sqlite`` sequence (users, accounts, audit and jobs: §6.2), so the file
        is at the same version whichever store opens it first. ``migrate=False`` raises
        :class:`SchemaMismatch` unless the version matches.
        """
        if not migrate and not path.is_file():
            raise SchemaMismatch(0, len(steps))
        conn = open_sqlite(path)
        try:
            if migrate:
                run_migrations(conn, steps)
            else:
                check_version(conn, steps)
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

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("the clock must return timezone-aware datetimes")
        return now

    # ---- server side ----------------------------------------------------------------------

    def enqueue(
        self,
        *,
        uid: str,
        acct: str | None,
        kind: str,
        dedupe_key: str,
        payload: Mapping[str, Any],
        max_attempts: int = 5,
        run_at: datetime | None = None,
        priority: int = PRIORITY_NORMAL,
    ) -> JobRow:
        """Queue a job, or return the existing job with the same ``dedupe_key``.

        See the module docstring for the dedupe convention and priorities. A key that belongs
        to another ``uid`` is ``refused``. An existing job of the same user that is still
        waiting (``queued`` or ``failed``) at a lower priority is raised to ``priority``.
        """
        if not uid or not kind or not dedupe_key:
            raise InvalidArgumentError("uid, kind and dedupe_key are required.")
        if max_attempts < 1:
            raise InvalidArgumentError("max_attempts must be at least 1.")
        _check_priority(priority)
        body = _json(payload, MAX_PAYLOAD_BYTES, "payload")
        now = to_db_time(self._now())
        next_run = now if run_at is None else to_db_time(run_at)
        with self._write() as conn:
            row = conn.execute(
                "INSERT INTO jobs(uid, acct, kind, dedupe_key, payload, state, attempts, "
                "max_attempts, next_run_at, created_at, updated_at, priority) "
                "VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?) "
                "ON CONFLICT(dedupe_key) DO NOTHING RETURNING *",
                (uid, acct, kind, dedupe_key, body, max_attempts, next_run, now, now, priority),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
                if row["uid"] != uid:
                    raise RefusedError("That job key is in use.")
                if row["state"] in ("queued", "failed") and row["priority"] < priority:
                    row = conn.execute(
                        "UPDATE jobs SET priority = ?, updated_at = ? WHERE id = ? RETURNING *",
                        (priority, now, row["id"]),
                    ).fetchone()
        if row["uid"] != uid:
            raise RefusedError("That job key is in use.")
        return _row(row)

    def record_dead(
        self,
        *,
        uid: str,
        acct: str | None,
        kind: str,
        dedupe_key: str,
        payload: Mapping[str, Any],
        error: str,
    ) -> JobRow:
        """Record work that already failed for good elsewhere as a ``dead`` job.

        For a verdict that arrived without a job of its own (a page the OCR API refused
        inside a batch): the key then names finished work, as for any dead job, and a
        deliberate re-run goes through :meth:`rerun`. Like :meth:`enqueue`, an existing key
        is returned unchanged, and a key held by another user is refused.
        """
        if not uid or not kind or not dedupe_key or not error:
            raise InvalidArgumentError("uid, kind, dedupe_key and error are required.")
        body = _json(payload, MAX_PAYLOAD_BYTES, "payload")
        now = to_db_time(self._now())
        with self._write() as conn:
            row = conn.execute(
                "INSERT INTO jobs(uid, acct, kind, dedupe_key, payload, state, attempts, "
                "max_attempts, error, next_run_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'dead', 0, 1, ?, ?, ?, ?) "
                "ON CONFLICT(dedupe_key) DO NOTHING RETURNING *",
                (uid, acct, kind, dedupe_key, body, error[:MAX_ERROR_CHARS], now, now, now),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
        if row["uid"] != uid:
            raise RefusedError("That job key is in use.")
        return _row(row)

    def deprioritise(self, *, uid: str, acct: str, document: str, kinds: Sequence[str]) -> int:
        """Lower to :data:`PRIORITY_LOW` the waiting jobs of ``document``; returns how many.

        Only jobs of ``uid`` and ``acct`` whose kind is in ``kinds``, whose ``payload.document``
        is ``document`` and that are ``queued`` or ``failed`` (a leased job keeps running). The
        worker calls this when it tombstones a document.
        """
        if not uid or not acct or not document:
            raise InvalidArgumentError("uid, acct and document are required.")
        if not kinds:
            return 0
        marks = ", ".join("?" * len(kinds))
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE jobs SET priority = ?, updated_at = ? "  # noqa: S608
                f"WHERE uid = ? AND acct = ? AND kind IN ({marks}) "
                "AND state IN ('queued', 'failed') AND priority > ? "
                "AND json_extract(payload, '$.document') = ?",
                (
                    PRIORITY_LOW,
                    to_db_time(self._now()),
                    uid,
                    acct,
                    *kinds,
                    PRIORITY_LOW,
                    document,
                ),
            )
            return int(cur.rowcount)

    def cancel(self, job_id: int, uid: str) -> bool:
        """Cancel a job of ``uid`` that is not running (``queued`` or ``failed``)."""
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE jobs SET state = 'cancelled', updated_at = ? "
                "WHERE id = ? AND uid = ? AND state IN ('queued', 'failed')",
                (to_db_time(self._now()), job_id, uid),
            )
            return cur.rowcount == 1

    def get(self, job_id: int, uid: str) -> JobRow | None:
        """The job, if it exists and belongs to ``uid``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ? AND uid = ?", (job_id, uid)
            ).fetchone()
        return None if row is None else _row(row)

    # ---- worker side ----------------------------------------------------------------------

    def lease(
        self,
        owner: str,
        *,
        kinds: Sequence[str] | None = None,
        ttl_s: int = 60,
        per_uid: Mapping[str, int] | None = None,
    ) -> JobRow | None:
        """Claim the next runnable job (``queued``, or ``failed`` past its backoff): the one
        with the highest priority, oldest first.

        One ``BEGIN IMMEDIATE`` transaction around ``UPDATE … WHERE id = (SELECT … LIMIT 1)
        RETURNING *``: two workers can never claim the same job. Increments ``attempts``.
        ``kinds`` restricts the claim to those kinds (``[]`` claims nothing). ``per_uid``
        maps a kind to the most jobs of that kind one user may hold leased at once
        (``{"ocr": OCR_USER_CONCURRENCY}``, §6.2): a user at the cap is skipped, so the next
        user's job is claimed instead and one backlog cannot starve the others.
        """
        if not owner:
            raise InvalidArgumentError("owner is required.")
        if ttl_s < 1:
            raise InvalidArgumentError("ttl_s must be positive.")
        if kinds is not None and not kinds:
            return None
        if per_uid is not None and any(limit < 1 for limit in per_uid.values()):
            raise InvalidArgumentError("per-user limits must be positive.")
        now = self._now()
        filters = ""
        stamp = to_db_time(now)
        params: list[Any] = [owner, to_db_time(now + timedelta(seconds=ttl_s)), stamp, stamp]
        if kinds is not None:
            filters += f" AND c.kind IN ({', '.join('?' * len(kinds))})"
            params.extend(kinds)
        for kind, limit in sorted((per_uid or {}).items()):
            filters += (
                " AND NOT (c.kind = ? AND (SELECT count(*) FROM jobs AS l "
                "WHERE l.uid = c.uid AND l.kind = ? AND l.state = 'leased') >= ?)"
            )
            params.extend((kind, kind, limit))
        sql = (
            "UPDATE jobs SET state = 'leased', lease_owner = ?, lease_expires_at = ?, "  # noqa: S608
            "attempts = attempts + 1, updated_at = ? "
            "WHERE id = (SELECT c.id FROM jobs AS c WHERE c.state IN ('queued', 'failed') "
            f"AND c.next_run_at <= ?{filters} "
            "ORDER BY c.priority DESC, c.created_at, c.id LIMIT 1) "
            "RETURNING *"
        )
        with self._write() as conn:
            row = conn.execute(sql, params).fetchone()
        return None if row is None else _row(row)

    def heartbeat(self, job_id: int, owner: str, ttl_s: int = 60) -> None:
        """Extend ``owner``'s lease to ``now + ttl_s``; :class:`LeaseLostError` if not held."""
        if ttl_s < 1:
            raise InvalidArgumentError("ttl_s must be positive.")
        now = self._now()
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE jobs SET lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND lease_owner = ? AND state = 'leased'",
                (to_db_time(now + timedelta(seconds=ttl_s)), to_db_time(now), job_id, owner),
            )
            if cur.rowcount != 1:
                raise LeaseLostError("The job lease is no longer held.")

    def complete(self, job_id: int, owner: str, result: Mapping[str, Any]) -> None:
        """Mark ``owner``'s leased job ``done`` with ``result``."""
        body = _json(result, MAX_RESULT_BYTES, "result")
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE jobs SET state = 'done', result = ?, error = NULL, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE id = ? AND lease_owner = ? AND state = 'leased'",
                (body, to_db_time(self._now()), job_id, owner),
            )
            if cur.rowcount != 1:
                raise LeaseLostError("The job lease is no longer held.")

    def fail(self, job_id: int, owner: str, error: str, *, final: bool = False) -> JobRow:
        """Record a failed attempt of ``owner``'s leased job.

        With attempts left: ``failed``, runnable again after :func:`backoff_seconds`. Otherwise,
        or when ``final`` (a verdict on the input that another attempt would repeat, such as
        an image the OCR API refuses), ``dead``. ``error`` is a short machine-oriented reason
        (truncated to 1024 characters); it must not carry note content or secrets.
        """
        now = self._now()
        with self._write() as conn:
            row = conn.execute(
                "SELECT attempts, max_attempts FROM jobs "
                "WHERE id = ? AND lease_owner = ? AND state = 'leased'",
                (job_id, owner),
            ).fetchone()
            if row is None:
                raise LeaseLostError("The job lease is no longer held.")
            attempts, max_attempts = int(row["attempts"]), int(row["max_attempts"])
            if final or attempts >= max_attempts:
                state, next_run = "dead", now
            else:
                state, next_run = "failed", now + timedelta(seconds=backoff_seconds(attempts))
            updated = conn.execute(
                "UPDATE jobs SET state = ?, error = ?, next_run_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE id = ? RETURNING *",
                (state, error[:MAX_ERROR_CHARS], to_db_time(next_run), to_db_time(now), job_id),
            ).fetchone()
        return _row(updated)

    def defer(self, job_id: int, owner: str, *, until: datetime, reason: str) -> JobRow:
        """Put ``owner``'s leased job back to ``queued`` until ``until``, for ``reason``.

        For work that cannot be done now for a reason outside the job (a missing key, a cap,
        a refused key): the lease's attempt is given back (``attempts - 1``), the job keeps
        its key, payload and place in line, and ``error`` records ``deferred:<reason>``
        (``reason`` is a ``[a-z_]`` code). It becomes leasable again at ``until`` (at once if
        that is not in the future), or earlier through :meth:`wake`.
        :class:`LeaseLostError` if the lease is not held.
        """
        if not isinstance(reason, str) or not _DEFER_REASON.fullmatch(reason):
            raise InvalidArgumentError("a deferral reason is a short [a-z_] code.")
        if until.tzinfo is None:
            raise InvalidArgumentError("until must carry a time zone.")
        now = self._now()
        with self._write() as conn:
            row = conn.execute(
                "UPDATE jobs SET state = 'queued', error = ?, next_run_at = ?, "
                "attempts = max(attempts - 1, 0), lease_owner = NULL, lease_expires_at = NULL, "
                "updated_at = ? WHERE id = ? AND lease_owner = ? AND state = 'leased' "
                "RETURNING *",
                (
                    DEFERRED_PREFIX + reason,
                    to_db_time(max(until, now)),
                    to_db_time(now),
                    job_id,
                    owner,
                ),
            ).fetchone()
        if row is None:
            raise LeaseLostError("The job lease is no longer held.")
        return _row(row)

    def wake(self, job_id: int, uid: str) -> JobRow | None:
        """Make ``uid``'s deferred job runnable now; the job as it is afterwards (None if it
        is not ``uid``'s). A job that is not deferred is returned unchanged."""
        now = to_db_time(self._now())
        with self._write() as conn:
            conn.execute(
                "UPDATE jobs SET next_run_at = ?, error = NULL, updated_at = ? "
                "WHERE id = ? AND uid = ? AND state = 'queued' AND substr(error, 1, ?) = ?",
                (now, now, job_id, uid, len(DEFERRED_PREFIX), DEFERRED_PREFIX),
            )
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ? AND uid = ?", (job_id, uid)
            ).fetchone()
        return None if row is None else _row(row)

    def wake_deferred(self, uid: str, *, acct: str | None = None, kinds: Sequence[str]) -> int:
        """Make every deferred job of ``uid`` (and ``acct``) of ``kinds`` runnable now, e.g.
        once the user stores a key; returns how many."""
        if not uid:
            raise InvalidArgumentError("uid is required.")
        if not kinds:
            return 0
        now = to_db_time(self._now())
        sql = (
            "UPDATE jobs SET next_run_at = ?, error = NULL, updated_at = ? "  # noqa: S608
            f"WHERE uid = ? AND kind IN ({', '.join('?' * len(kinds))}) "
            "AND state = 'queued' AND substr(error, 1, ?) = ?"
        )
        params: list[Any] = [now, now, uid, *kinds, len(DEFERRED_PREFIX), DEFERRED_PREFIX]
        if acct is not None:
            sql += " AND acct = ?"
            params.append(acct)
        with self._write() as conn:
            return int(conn.execute(sql, params).rowcount)

    def requeue_expired(self) -> int:
        """Return expired leases to the queue (or ``dead`` when out of attempts).

        Run on worker start and every minute. Returns how many jobs changed state.
        """
        now = to_db_time(self._now())
        with self._write() as conn:
            dead = conn.execute(
                "UPDATE jobs SET state = 'dead', error = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE state = 'leased' AND lease_expires_at < ? AND attempts >= max_attempts",
                (LEASE_EXPIRED_ERROR, now, now),
            ).rowcount
            requeued = conn.execute(
                "UPDATE jobs SET state = 'queued', error = ?, next_run_at = ?, "
                "lease_owner = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE state = 'leased' AND lease_expires_at < ?",
                (LEASE_EXPIRED_ERROR, now, now, now),
            ).rowcount
        return dead + requeued

    def rerun(self, job: JobRow, *, payload: Mapping[str, Any] | None = None) -> JobRow | None:
        """A fresh job re-running ``job`` under ``rerun_key``, or None when its family is capped.

        The new job copies ``job``'s user, account, kind and ``max_attempts``, and its payload
        unless ``payload`` is given. If the re-run already exists (another caller got there
        first) it is returned whatever its state. Otherwise it is created only while the key's
        family has fewer than :data:`~scriptorium.jobs.kinds.RERUN_CAP` ``:after:`` jobs,
        counted for all time (re-runs of jobs that died of a refused OCR key excepted, see
        :func:`_family_reruns`). A job that ended :data:`~scriptorium.jobs.kinds.GONE_RESULT` is
        not re-run with the payload it had (None): that page version was not there to render.
        Another version that holds the same page (a copy, a later generation) may re-run it
        by passing its own ``payload``.
        """
        body = _json(job.payload if payload is None else payload, MAX_PAYLOAD_BYTES, "payload")
        if job.state == "done" and is_gone(job.result):
            if body == _json(job.payload, MAX_PAYLOAD_BYTES, "payload"):
                return None
        key = rerun_key(job.dedupe_key, job.id)
        with self._write() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE dedupe_key = ?", (key,)).fetchone()
            if row is None:
                row = self._insert_rerun(
                    conn,
                    job.uid,
                    job.acct,
                    job.kind,
                    job.dedupe_key,
                    job.id,
                    body,
                    job.max_attempts,
                    max(job.priority, PRIORITY_NORMAL),
                )
        if row is None:
            return None
        if row["uid"] != job.uid:
            raise RefusedError("That job key is in use.")
        return _row(row)

    def revive_dead_renders(self) -> int:
        """Re-run ``dead`` render jobs whose run failed, not their page; returns how many.

        Only ``render`` and ``page_image`` jobs whose ``error`` is not a renderer verdict
        (:data:`~scriptorium.jobs.kinds.RENDER_VERDICTS`) and that have never been re-run:
        each gets a fresh ``:after:`` job (the dead row stays as it is, and its re-run marks
        it revived), within the family's :data:`~scriptorium.jobs.kinds.RERUN_CAP`. Every
        other job is untouched. The worker calls this once at start.
        """
        revived = 0
        with self._write() as conn:
            for row, reruns in self._dead_render_leaves(conn, "", []):
                if is_render_verdict(row["error"]) or reruns >= RERUN_CAP:
                    continue
                created = self._insert_rerun(
                    conn,
                    row["uid"],
                    row["acct"],
                    row["kind"],
                    row["dedupe_key"],
                    row["id"],
                    row["payload"],
                    row["max_attempts"],
                    row["priority"],
                )
                revived += created is not None
        return revived

    def _insert_rerun(
        self,
        conn: sqlite3.Connection,
        uid: str,
        acct: str | None,
        kind: str,
        key: str,
        job_id: int,
        payload: str,
        max_attempts: int,
        priority: int,
    ) -> sqlite3.Row | None:
        """Insert the re-run of job ``job_id`` unless its family is at its cap (in a write)."""
        if _family_reruns(conn, rerun_family(key)) >= RERUN_CAP:
            return None
        now = to_db_time(self._now())
        row: sqlite3.Row | None = conn.execute(
            "INSERT INTO jobs(uid, acct, kind, dedupe_key, payload, state, attempts, "
            "max_attempts, next_run_at, created_at, updated_at, priority) "
            "VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?) RETURNING *",
            (
                uid,
                acct,
                kind,
                rerun_key(key, job_id),
                payload,
                max_attempts,
                now,
                now,
                now,
                priority,
            ),
        ).fetchone()
        return row

    @staticmethod
    def _dead_render_leaves(
        conn: sqlite3.Connection, where: str, params: Sequence[Any]
    ) -> list[tuple[sqlite3.Row, int]]:
        """Dead render jobs never re-run, each with its family's re-run count; a family in
        which some job ended ``gone`` is left out (nothing of it is re-run or reported)."""
        kinds = ", ".join("?" * len(RENDER_KINDS))
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE state = 'dead' AND kind IN ({kinds}){where} ORDER BY id",  # noqa: S608
            (*RENDER_KINDS, *params),
        ).fetchall()
        out: list[tuple[sqlite3.Row, int]] = []
        for row in rows:
            child = rerun_key(row["dedupe_key"], row["id"])
            if conn.execute("SELECT 1 FROM jobs WHERE dedupe_key = ?", (child,)).fetchone():
                continue
            base = rerun_family(row["dedupe_key"])
            if _family_gone(conn, base):
                continue
            out.append((row, _family_reruns(conn, base)))
        return out

    # ---- lookups ----------------------------------------------------------------------------

    def family(self, uid: str, key: str) -> list[JobRow]:
        """The jobs of ``uid`` in ``key``'s family (its base key and every ``:after:`` re-run),
        oldest first; the last one is the current attempt at the work."""
        base = rerun_family(key)
        low = base + AFTER
        high = low[:-1] + chr(ord(AFTER[-1]) + 1)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE uid = ? AND (dedupe_key = ? OR "
                "(dedupe_key >= ? AND dedupe_key < ?)) ORDER BY id",
                (uid, base, low, high),
            ).fetchall()
        return [_row(r) for r in rows]

    def active(self, uid: str, *, acct: str | None, kind: str) -> list[JobRow]:
        """``uid``'s jobs of ``kind`` that are not finished (queued, leased or failed)."""
        sql = (
            "SELECT * FROM jobs WHERE uid = ? AND kind = ? "
            "AND state IN ('queued', 'leased', 'failed')"
        )
        params: list[Any] = [uid, kind]
        if acct is not None:
            sql += " AND acct = ?"
            params.append(acct)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [_row(r) for r in rows]

    def recent_errors(
        self, uid: str, *, acct: str | None, kinds: Sequence[str], limit: int = 10
    ) -> list[tuple[str, str, datetime]]:
        """``(kind, error, updated_at)`` of ``uid``'s latest ``failed`` and ``dead`` jobs of
        ``kinds``, newest first. Errors are the reason codes the worker recorded."""
        if not kinds or limit < 1:
            return []
        sql = (
            "SELECT kind, error, updated_at FROM jobs WHERE uid = ? "  # noqa: S608
            f"AND kind IN ({', '.join('?' * len(kinds))}) "
            "AND state IN ('failed', 'dead') AND error IS NOT NULL"
        )
        params: list[Any] = [uid, *kinds]
        if acct is not None:
            sql += " AND acct = ?"
            params.append(acct)
        with self._lock:
            rows = self._conn.execute(
                sql + " ORDER BY updated_at DESC, id DESC LIMIT ?", (*params, limit)
            ).fetchall()
        return [(str(r["kind"]), str(r["error"]), from_db_time(r["updated_at"])) for r in rows]

    # ---- reporting --------------------------------------------------------------------------

    def deferred_counts(
        self, uid: str, *, acct: str | None = None, kinds: Sequence[str]
    ) -> dict[str, dict[str, int]]:
        """``{kind: {reason: n}}`` over the deferred jobs of ``uid`` (and ``acct``); they are
        also counted as ``queued`` by :meth:`counts`."""
        if not kinds:
            return {}
        sql = (
            "SELECT kind, substr(error, ?) AS reason, count(*) AS n FROM jobs "  # noqa: S608
            f"WHERE uid = ? AND kind IN ({', '.join('?' * len(kinds))}) "
            "AND state = 'queued' AND substr(error, 1, ?) = ?"
        )
        params: list[Any] = [
            len(DEFERRED_PREFIX) + 1,
            uid,
            *kinds,
            len(DEFERRED_PREFIX),
            DEFERRED_PREFIX,
        ]
        if acct is not None:
            sql += " AND acct = ?"
            params.append(acct)
        with self._lock:
            rows = self._conn.execute(sql + " GROUP BY kind, reason", params).fetchall()
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            out.setdefault(str(row["kind"]), {})[str(row["reason"])] = int(row["n"])
        return out

    def counts(self, uid: str, *, acct: str | None = None) -> dict[str, dict[str, int]]:
        """``{kind: {state: n}}`` over the jobs of ``uid`` (and ``acct``), for status tools."""
        sql = "SELECT kind, state, count(*) AS n FROM jobs WHERE uid = ?"
        params: list[Any] = [uid]
        if acct is not None:
            sql += " AND acct = ?"
            params.append(acct)
        with self._lock:
            rows = self._conn.execute(sql + " GROUP BY kind, state", params).fetchall()
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            out.setdefault(str(row["kind"]), {})[str(row["state"])] = int(row["n"])
        return out

    def dead_renders(self, uid: str, *, acct: str | None = None) -> tuple[int, int]:
        """``(final, rerunnable)``: dead render jobs of ``uid`` that were never re-run.

        ``final`` holds the renderer's verdicts and the jobs whose family is at its re-run
        cap; ``rerunnable`` the rest, which ``page_png`` or the next worker start re-runs.
        """
        where, params = " AND uid = ?", [uid]
        if acct is not None:
            where += " AND acct = ?"
            params.append(acct)
        final = rerunnable = 0
        with self._lock:
            for row, reruns in self._dead_render_leaves(self._conn, where, params):
                if is_render_verdict(row["error"]) or reruns >= RERUN_CAP:
                    final += 1
                else:
                    rerunnable += 1
        return final, rerunnable


_GONE_TEXT: Final = _json(GONE_RESULT, MAX_RESULT_BYTES, "result")


def _family_gone(conn: sqlite3.Connection, base: str) -> bool:
    """Whether a job of the family ``base`` (the base key or one of its re-runs) ended with
    :data:`~scriptorium.jobs.kinds.GONE_RESULT`."""
    low = base + AFTER
    high = low[:-1] + chr(ord(AFTER[-1]) + 1)
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE (dedupe_key = ? OR (dedupe_key >= ? AND dedupe_key < ?)) "
        "AND state = 'done' AND result = ? LIMIT 1",
        (base, low, high, _GONE_TEXT),
    ).fetchone()
    return row is not None


def _family_reruns(conn: sqlite3.Connection, base: str) -> int:
    """How many ``:after:`` jobs of the family ``base`` count against ``RERUN_CAP`` (a range
    scan of the unique index). A re-run of a job that died because the OCR API refused the
    key (``error`` ``key_…``, :data:`~scriptorium.jobs.kinds.KEY_ERROR_PREFIX`) is not
    counted: the refusal was about the key, not the page."""
    low = base + AFTER
    high = low[:-1] + chr(ord(AFTER[-1]) + 1)  # the first string after every "<low>…"
    row = conn.execute(
        "SELECT count(*) FROM jobs AS r WHERE r.dedupe_key >= ? AND r.dedupe_key < ? "
        "AND NOT EXISTS (SELECT 1 FROM jobs AS p "
        "WHERE p.id = CAST(substr(r.dedupe_key, ?) AS INTEGER) AND p.uid = r.uid "
        "AND p.state = 'dead' AND substr(p.error, 1, ?) = ?)",
        (low, high, len(low) + 1, len(KEY_ERROR_PREFIX), KEY_ERROR_PREFIX),
    ).fetchone()
    return int(row[0])
