"""The durable job queue in ``scriptorium.sqlite`` (Architecture §6.2; M1 plan §3.5).

The server enqueues, the worker leases and runs. Life of a job::

    queued ──lease──▶ leased ──complete──▶ done
      ▲  │              │ └──fail (attempts < max)──▶ failed ──(next_run_at)──lease──▶ leased
      │  └─cancel─▶ cancelled                              └─cancel─▶ cancelled
      │                 ├──fail (attempts ≥ max)──▶ dead
      └─requeue_expired─┘ (lease expired; ``dead`` instead once attempts ≥ max)

- ``attempts`` counts leases, so a job that crashes its worker (lease expires, never failed)
  still runs out of attempts instead of looping forever.
- ``fail`` backs off ``min(2**attempts * 30 s, 1 h)`` (:func:`backoff_seconds`).
- **Dedupe convention.** ``dedupe_key`` is unique across the table for all time. ``enqueue``
  with a key that exists returns that row, whatever its state, and creates nothing. Keys
  therefore name *the work*, including the version it applies to (``ocr:<acct>:<page>:<hash>``,
  ``push:<acct>:<uuid>``, ``render:<acct>:<page>:<hash>``); a new version is a new key, and a
  caller that deliberately re-runs finished work passes a new key (e.g. with a ``:retry-<n>``
  suffix). A key held by another user is refused rather than returned.
- ``cancel`` succeeds only for the owning ``uid`` and only while the job is not running
  (``queued``, or ``failed`` waiting for its retry).

Times come from an injected clock (``Callable[[], datetime]``, timezone-aware) so tests are
deterministic. Nothing here logs payloads, results or errors: they may carry identifiers the
caller chose, and the ``error`` text is for ``*_status`` tools only.
"""

from __future__ import annotations

import json
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
from scriptorium.db.schema_jobs import JOBS_STEPS
from scriptorium.errors import InvalidArgumentError, RefusedError

STATES: Final = frozenset({"queued", "leased", "done", "failed", "dead", "cancelled"})
BACKOFF_BASE_S: Final = 30
BACKOFF_CAP_S: Final = 3600
MAX_PAYLOAD_BYTES: Final = 64 * 1024
MAX_RESULT_BYTES: Final = 64 * 1024
MAX_ERROR_CHARS: Final = 1024
LEASE_EXPIRED_ERROR: Final = "lease expired"


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
    )


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
        steps: Sequence[Step] = JOBS_STEPS,
    ) -> JobQueue:
        """Open ``path`` (normally ``/data/scriptorium.sqlite``).

        ``steps`` is the schema sequence of the whole file. ``scriptorium.sqlite`` also holds
        users, accounts and audit, so the server passes the composed sequence for that file;
        the default (jobs only) suits a database that holds nothing else, as in tests.
        ``migrate=False`` raises :class:`SchemaMismatch` unless the version matches.
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
    ) -> JobRow:
        """Queue a job, or return the existing job with the same ``dedupe_key``.

        See the module docstring for the dedupe convention. A key that belongs to another
        ``uid`` is ``refused``.
        """
        if not uid or not kind or not dedupe_key:
            raise InvalidArgumentError("uid, kind and dedupe_key are required.")
        if max_attempts < 1:
            raise InvalidArgumentError("max_attempts must be at least 1.")
        body = _json(payload, MAX_PAYLOAD_BYTES, "payload")
        now = to_db_time(self._now())
        next_run = now if run_at is None else to_db_time(run_at)
        with self._write() as conn:
            row = conn.execute(
                "INSERT INTO jobs(uid, acct, kind, dedupe_key, payload, state, attempts, "
                "max_attempts, next_run_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?) "
                "ON CONFLICT(dedupe_key) DO NOTHING RETURNING *",
                (uid, acct, kind, dedupe_key, body, max_attempts, next_run, now, now),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
        if row["uid"] != uid:
            raise RefusedError("That job key is in use.")
        return _row(row)

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
        self, owner: str, *, kinds: Sequence[str] | None = None, ttl_s: int = 60
    ) -> JobRow | None:
        """Claim the oldest runnable job (``queued``, or ``failed`` past its backoff).

        One ``BEGIN IMMEDIATE`` transaction around ``UPDATE … WHERE id = (SELECT … LIMIT 1)
        RETURNING *``: two workers can never claim the same job. Increments ``attempts``.
        ``kinds`` restricts the claim to those kinds (``[]`` claims nothing).
        """
        if not owner:
            raise InvalidArgumentError("owner is required.")
        if ttl_s < 1:
            raise InvalidArgumentError("ttl_s must be positive.")
        if kinds is not None and not kinds:
            return None
        now = self._now()
        kind_filter = ""
        stamp = to_db_time(now)
        params: list[Any] = [owner, to_db_time(now + timedelta(seconds=ttl_s)), stamp, stamp]
        if kinds is not None:
            kind_filter = f" AND kind IN ({', '.join('?' * len(kinds))})"
            params.extend(kinds)
        sql = (
            "UPDATE jobs SET state = 'leased', lease_owner = ?, lease_expires_at = ?, "  # noqa: S608
            "attempts = attempts + 1, updated_at = ? "
            "WHERE id = (SELECT id FROM jobs WHERE state IN ('queued', 'failed') "
            f"AND next_run_at <= ?{kind_filter} ORDER BY created_at, id LIMIT 1) "
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

    def fail(self, job_id: int, owner: str, error: str) -> JobRow:
        """Record a failed attempt of ``owner``'s leased job.

        With attempts left: ``failed``, runnable again after :func:`backoff_seconds`. Otherwise
        ``dead``. ``error`` is a short machine-oriented reason (truncated to 1024 characters);
        it must not carry note content or secrets.
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
            if attempts >= max_attempts:
                state, next_run = "dead", now
            else:
                state, next_run = "failed", now + timedelta(seconds=backoff_seconds(attempts))
            updated = conn.execute(
                "UPDATE jobs SET state = ?, error = ?, next_run_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE id = ? RETURNING *",
                (state, error[:MAX_ERROR_CHARS], to_db_time(next_run), to_db_time(now), job_id),
            ).fetchone()
        return _row(updated)

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
