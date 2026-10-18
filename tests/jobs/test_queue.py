from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scriptorium.db.connect import open_sqlite
from scriptorium.db.migrations import SchemaMismatch
from scriptorium.db.schema_jobs import JOBS_STEPS
from scriptorium.errors import InvalidArgumentError, RefusedError
from scriptorium.jobs.queue import (
    LEASE_EXPIRED_ERROR,
    JobQueue,
    LeaseLostError,
    backoff_seconds,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
UID = "0123456789abcdef"
OTHER = "fedcba9876543210"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "scriptorium.sqlite"


@pytest.fixture
def queue(path: Path, clock: Clock) -> Iterator[JobQueue]:
    with JobQueue.open(path, migrate=True, clock=clock) as q:
        yield q


def put(q: JobQueue, key: str, **kw: Any) -> int:
    args: dict[str, Any] = {
        "uid": UID,
        "acct": "01J0000000000000000000000A",
        "kind": "render",
        "payload": {"page": key},
    }
    args.update(kw)
    return q.enqueue(dedupe_key=key, **args).id


# ---- open -----------------------------------------------------------------------------------


def test_open_without_migrate_checks_the_version(path: Path, clock: Clock) -> None:
    with pytest.raises(SchemaMismatch):
        JobQueue.open(path, migrate=False, clock=clock)
    assert not path.exists()
    open_sqlite(path).close()
    with pytest.raises(SchemaMismatch):
        JobQueue.open(path, migrate=False, clock=clock)
    JobQueue.open(path, migrate=True, clock=clock).close()
    JobQueue.open(path, migrate=False, clock=clock).close()
    with pytest.raises(SchemaMismatch):
        JobQueue.open(path, migrate=False, clock=clock, steps=(*JOBS_STEPS, *JOBS_STEPS))


# ---- enqueue and dedupe ---------------------------------------------------------------------


def test_enqueue_creates_a_queued_job(queue: JobQueue) -> None:
    job = queue.enqueue(
        uid=UID, acct=None, kind="render", dedupe_key="render:a", payload={"n": 1, "s": "x"}
    )
    assert job.state == "queued"
    assert (job.uid, job.acct, job.kind, job.dedupe_key) == (UID, None, "render", "render:a")
    assert job.payload == {"n": 1, "s": "x"}
    assert (job.attempts, job.max_attempts) == (0, 5)
    assert job.next_run_at == job.created_at == job.updated_at == T0
    assert (job.lease_owner, job.lease_expires_at, job.result, job.error) == (None,) * 4
    assert queue.get(job.id, UID) == job


def test_enqueue_is_idempotent_on_dedupe_key(queue: JobQueue, clock: Clock) -> None:
    first = queue.enqueue(uid=UID, acct=None, kind="render", dedupe_key="k", payload={"v": 1})
    clock.advance(10)
    again = queue.enqueue(uid=UID, acct=None, kind="render", dedupe_key="k", payload={"v": 2})
    assert again == first  # the existing row, payload unchanged
    leased = queue.lease("w1")
    assert leased is not None
    assert queue.enqueue(uid=UID, acct=None, kind="render", dedupe_key="k", payload={}).state == (
        "leased"
    )
    queue.complete(first.id, "w1", {"ok": True})
    done = queue.enqueue(uid=UID, acct=None, kind="render", dedupe_key="k", payload={})
    assert (done.id, done.state) == (first.id, "done")
    # Re-running finished work takes a new key, by convention.
    fresh = queue.enqueue(uid=UID, acct=None, kind="render", dedupe_key="k:retry-1", payload={})
    assert fresh.id != first.id and fresh.state == "queued"


def test_dedupe_key_of_another_user_is_refused(queue: JobQueue) -> None:
    put(queue, "shared")
    with pytest.raises(RefusedError):
        put(queue, "shared", uid=OTHER)


def test_enqueue_validates(queue: JobQueue) -> None:
    with pytest.raises(InvalidArgumentError):
        put(queue, "")
    with pytest.raises(InvalidArgumentError):
        put(queue, "k", max_attempts=0)
    with pytest.raises(InvalidArgumentError):
        put(queue, "k", payload={"blob": "x" * (64 * 1024)})
    with pytest.raises(InvalidArgumentError):
        put(queue, "k", run_at=datetime(2026, 1, 1))  # naive


def test_run_at_delays_the_job(queue: JobQueue, clock: Clock) -> None:
    put(queue, "later", run_at=T0 + timedelta(minutes=5))
    assert queue.lease("w") is None
    clock.advance(299)
    assert queue.lease("w") is None
    clock.advance(1)
    job = queue.lease("w")
    assert job is not None and job.dedupe_key == "later"


# ---- lease ----------------------------------------------------------------------------------


def test_lease_takes_the_oldest_and_sets_the_lease(queue: JobQueue, clock: Clock) -> None:
    a = put(queue, "a")
    clock.advance(1)
    b = put(queue, "b")
    job = queue.lease("worker-1", ttl_s=90)
    assert job is not None and job.id == a
    assert (job.state, job.lease_owner, job.attempts) == ("leased", "worker-1", 1)
    assert job.lease_expires_at == T0 + timedelta(seconds=1 + 90)
    second = queue.lease("worker-1")
    assert second is not None and second.id == b
    assert queue.lease("worker-1") is None


def test_lease_filters_by_kind(queue: JobQueue) -> None:
    put(queue, "r", kind="render")
    put(queue, "p", kind="page_image")
    assert queue.lease("w", kinds=[]) is None
    job = queue.lease("w", kinds=["page_image", "ocr"])
    assert job is not None and job.kind == "page_image"
    assert queue.lease("w", kinds=["page_image"]) is None
    job = queue.lease("w", kinds=None)
    assert job is not None and job.kind == "render"


def test_lease_validates(queue: JobQueue) -> None:
    with pytest.raises(InvalidArgumentError):
        queue.lease("")
    with pytest.raises(InvalidArgumentError):
        queue.lease("w", ttl_s=0)


def test_lease_contention_two_connections_two_threads(path: Path) -> None:
    """Two workers on two connections never claim the same job."""
    with JobQueue.open(path, migrate=True) as setup:
        for n in range(200):
            setup.enqueue(uid=UID, acct=None, kind="render", dedupe_key=f"k{n}", payload={})
    claimed: dict[str, list[int]] = {"a": [], "b": []}
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        q = JobQueue.open(path, migrate=False)
        try:
            barrier.wait()
            while (job := q.lease(name)) is not None:
                claimed[name].append(job.id)
                q.complete(job.id, name, {})
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            q.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in claimed]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    everything = claimed["a"] + claimed["b"]
    assert len(everything) == 200
    assert len(set(everything)) == 200  # no job leased twice


def test_same_job_is_not_leased_twice_across_connections(path: Path, clock: Clock) -> None:
    a = JobQueue.open(path, migrate=True, clock=clock)
    b = JobQueue.open(path, migrate=False, clock=clock)
    try:
        put(a, "only")
        assert a.lease("a") is not None
        assert b.lease("b") is None
    finally:
        a.close()
        b.close()


# ---- heartbeat, complete --------------------------------------------------------------------


def test_heartbeat_extends_only_the_owners_lease(queue: JobQueue, clock: Clock) -> None:
    job_id = put(queue, "k")
    queue.lease("w", ttl_s=60)
    clock.advance(50)
    queue.heartbeat(job_id, "w", ttl_s=60)
    got = queue.get(job_id, UID)
    assert got is not None and got.lease_expires_at == T0 + timedelta(seconds=110)
    with pytest.raises(LeaseLostError):
        queue.heartbeat(job_id, "intruder")
    with pytest.raises(InvalidArgumentError):
        queue.heartbeat(job_id, "w", ttl_s=0)
    clock.advance(59)
    assert queue.requeue_expired() == 0  # extended, so not yet expired


def test_complete(queue: JobQueue, clock: Clock) -> None:
    job_id = put(queue, "k")
    queue.lease("w")
    with pytest.raises(LeaseLostError):
        queue.complete(job_id, "other", {})
    clock.advance(3)
    queue.complete(job_id, "w", {"png": "pages/x.png", "bytes": 12})
    got = queue.get(job_id, UID)
    assert got is not None
    assert (got.state, got.result, got.lease_owner, got.lease_expires_at) == (
        "done",
        {"png": "pages/x.png", "bytes": 12},
        None,
        None,
    )
    assert got.updated_at == T0 + timedelta(seconds=3)
    with pytest.raises(LeaseLostError):
        queue.complete(job_id, "w", {})  # already done
    with pytest.raises(LeaseLostError):
        queue.heartbeat(job_id, "w")


# ---- fail, backoff, dead --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempts", "seconds"),
    [(0, 30), (1, 60), (2, 120), (5, 960), (6, 1920), (7, 3600), (8, 3600), (10_000, 3600)],
)
def test_backoff_arithmetic(attempts: int, seconds: int) -> None:
    assert backoff_seconds(attempts) == seconds == min(2**attempts * 30, 3600)


def test_backoff_rejects_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        backoff_seconds(-1)


def test_fail_backs_off_then_dies_after_max_attempts(queue: JobQueue, clock: Clock) -> None:
    job_id = put(queue, "k", max_attempts=3)
    for attempt in (1, 2):
        job = queue.lease("w")
        assert job is not None and job.attempts == attempt
        failed = queue.fail(job_id, "w", "render_error")
        delay = backoff_seconds(attempt)
        assert (failed.state, failed.error, failed.lease_owner) == ("failed", "render_error", None)
        assert failed.next_run_at == clock.now + timedelta(seconds=delay)
        clock.advance(delay - 1)
        assert queue.lease("w") is None  # still backing off
        clock.advance(1)
    job = queue.lease("w")
    assert job is not None and job.attempts == 3
    dead = queue.fail(job_id, "w", "render_error")
    assert dead.state == "dead"
    clock.advance(10_000)
    assert queue.lease("w") is None
    assert queue.cancel(job_id, UID) is False


def test_fail_with_max_attempts_one_is_dead_at_once(queue: JobQueue) -> None:
    job_id = put(queue, "k", max_attempts=1)
    queue.lease("w")
    assert queue.fail(job_id, "w", "x").state == "dead"


def test_fail_requires_the_lease_and_truncates_error(queue: JobQueue) -> None:
    job_id = put(queue, "k")
    with pytest.raises(LeaseLostError):
        queue.fail(job_id, "w", "e")  # not leased
    queue.lease("w")
    with pytest.raises(LeaseLostError):
        queue.fail(job_id, "other", "e")
    failed = queue.fail(job_id, "w", "e" * 5000)
    assert failed.error is not None and len(failed.error) == 1024


def test_failed_job_waiting_for_retry_keeps_its_place(queue: JobQueue, clock: Clock) -> None:
    old = put(queue, "old")
    queue.lease("w")
    queue.fail(old, "w", "e")
    clock.advance(1)
    put(queue, "new")
    clock.advance(backoff_seconds(1))
    job = queue.lease("w")
    assert job is not None and job.id == old  # ordered by created_at


# ---- expiry and requeue ---------------------------------------------------------------------


def test_requeue_expired(queue: JobQueue, clock: Clock) -> None:
    job_id = put(queue, "k")
    queue.lease("w1", ttl_s=60)
    clock.advance(60)
    assert queue.requeue_expired() == 0  # expires strictly after the deadline
    clock.advance(1)
    assert queue.requeue_expired() == 1
    got = queue.get(job_id, UID)
    assert got is not None
    assert (got.state, got.lease_owner, got.error, got.attempts) == (
        "queued",
        None,
        LEASE_EXPIRED_ERROR,
        1,
    )
    with pytest.raises(LeaseLostError):
        queue.complete(job_id, "w1", {})  # the old owner lost it
    again = queue.lease("w2")
    assert again is not None and (again.id, again.attempts) == (job_id, 2)
    with pytest.raises(LeaseLostError):
        queue.heartbeat(job_id, "w1")


def test_requeue_expired_kills_jobs_out_of_attempts(queue: JobQueue, clock: Clock) -> None:
    job_id = put(queue, "k", max_attempts=2)
    for _ in range(2):
        assert queue.lease("w", ttl_s=10) is not None
        clock.advance(11)
        assert queue.requeue_expired() == 1
    got = queue.get(job_id, UID)
    assert got is not None and (got.state, got.error) == ("dead", LEASE_EXPIRED_ERROR)
    assert queue.lease("w") is None


def test_requeue_ignores_live_leases_and_other_states(queue: JobQueue, clock: Clock) -> None:
    stale = put(queue, "stale")
    live = put(queue, "live")
    done = put(queue, "done")
    waiting = put(queue, "waiting")
    for _ in range(3):
        queue.lease("w")  # stale, live, done (oldest first)
    queue.complete(done, "w", {})
    clock.advance(30)
    queue.heartbeat(live, "w", ttl_s=600)
    clock.advance(31)
    assert queue.requeue_expired() == 1
    states = {j: queue.get(j, UID).state for j in (stale, live, done, waiting)}  # type: ignore[union-attr]
    assert states == {stale: "queued", live: "leased", done: "done", waiting: "queued"}


# ---- cancel and get -------------------------------------------------------------------------


def test_cancel_only_while_not_running_and_only_by_owner(queue: JobQueue) -> None:
    a = put(queue, "a")
    assert queue.cancel(a, OTHER) is False
    assert queue.cancel(a, UID) is True
    got = queue.get(a, UID)
    assert got is not None and got.state == "cancelled"
    assert queue.cancel(a, UID) is False
    assert queue.lease("w") is None  # cancelled jobs are never leased

    b = put(queue, "b")
    queue.lease("w")
    assert queue.cancel(b, UID) is False  # running
    queue.fail(b, "w", "e")
    assert queue.cancel(b, UID) is True  # waiting for its retry
    assert queue.cancel(999, UID) is False


def test_get_is_scoped_to_the_user(queue: JobQueue) -> None:
    job_id = put(queue, "k")
    assert queue.get(job_id, UID) is not None
    assert queue.get(job_id, OTHER) is None
    assert queue.get(999, UID) is None


def test_naive_clock_is_refused(path: Path) -> None:
    with JobQueue.open(path, migrate=True, clock=lambda: datetime(2026, 1, 1)) as q:
        with pytest.raises(ValueError, match="timezone"):
            q.lease("w")
