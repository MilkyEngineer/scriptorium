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
from scriptorium.jobs.kinds import GONE_RESULT, RERUN_CAP, is_gone
from scriptorium.jobs.queue import (
    LEASE_EXPIRED_ERROR,
    JobQueue,
    LeaseLostError,
    backoff_seconds,
    deferral_reason,
    is_deferred,
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
    with pytest.raises(SchemaMismatch):  # a sequence of another length is another schema
        JobQueue.open(path, migrate=False, clock=clock, steps=JOBS_STEPS)


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


def test_counts_group_by_kind_and_state(queue: JobQueue) -> None:
    put(queue, "a")
    put(queue, "b")
    put(queue, "c", kind="page_image")
    put(queue, "d", acct="01J0000000000000000000000B")
    put(queue, "e", uid=OTHER)
    job = queue.lease("w", kinds=["page_image"])
    assert job is not None
    queue.complete(job.id, "w", {})
    assert queue.counts(UID) == {"render": {"queued": 3}, "page_image": {"done": 1}}
    assert queue.counts(UID, acct="01J0000000000000000000000B") == {"render": {"queued": 1}}
    assert queue.counts("0000000000000000") == {}


# ---- dead render jobs and re-runs -------------------------------------------------------------


def kill(q: JobQueue, key: str, error: str, **kw: Any) -> int:
    """Enqueue ``key`` with one attempt and fail it: dead with ``error``."""
    job_id = put(q, key, max_attempts=1, **kw)
    job = q.lease("w", kinds=[kw.get("kind", "render")])
    assert job is not None and job.id == job_id
    assert q.fail(job_id, "w", error).state == "dead"
    return job_id


def keys(q: JobQueue) -> dict[str, str]:
    """Every job's key → state."""
    with q._lock:
        rows = q._conn.execute("SELECT dedupe_key, state FROM jobs").fetchall()
    return {row["dedupe_key"]: row["state"] for row in rows}


def test_revive_dead_renders_only_touches_run_failures(queue: JobQueue, clock: Clock) -> None:
    runs = {
        kill(queue, "render:a", "internal"),
        kill(queue, "render:b", LEASE_EXPIRED_ERROR),
        kill(queue, "page_image:c", "local_io", kind="page_image"),
    }
    verdicts = {
        kill(queue, "render:d", "invalid_argument"),
        kill(queue, "page_image:e", "refused", kind="page_image"),
    }
    other = kill(queue, "ocr:f", "internal", kind="ocr")
    done = put(queue, "render:g")
    job = queue.lease("w", kinds=["render"])
    assert job is not None and job.id == done
    queue.complete(done, "w", {})
    assert queue.dead_renders(UID) == (2, 3)
    before = keys(queue)
    clock.advance(10)

    assert queue.revive_dead_renders() == 3

    after = keys(queue)
    assert {k: v for k, v in after.items() if k in before} == before  # dead rows stay dead
    for job_id in runs:
        dead = queue.get(job_id, UID)
        assert dead is not None and dead.state == "dead"
        fresh = queue.enqueue(
            uid=UID,
            acct=dead.acct,
            kind=dead.kind,
            dedupe_key=f"{dead.dedupe_key}:after:{job_id}",
            payload={},
        )
        assert (fresh.state, fresh.attempts, fresh.error) == ("queued", 0, None)
        assert fresh.payload == dead.payload and fresh.max_attempts == dead.max_attempts
        assert fresh.next_run_at == clock.now
    assert set(after) - set(before) == {
        f"{queue.get(j, UID).dedupe_key}:after:{j}"  # type: ignore[union-attr]
        for j in runs
    }
    assert all(queue.get(j, UID).state == "dead" for j in verdicts)  # type: ignore[union-attr]
    ocr = queue.get(other, UID)
    assert ocr is not None and (ocr.state, ocr.error) == ("dead", "internal")
    assert queue.dead_renders(UID) == (2, 0)  # the revived ones are superseded
    assert queue.revive_dead_renders() == 0  # once per job


def test_revival_is_capped_per_page_version_across_restarts(queue: JobQueue) -> None:
    job_id = kill(queue, "render:p", "internal")
    for n in range(RERUN_CAP):
        assert queue.revive_dead_renders() == 1, n  # one worker start
        job = queue.lease("w", kinds=["render"])
        assert job is not None and job.dedupe_key == f"render:p:after:{job_id}"
        assert queue.fail(job.id, "w", "internal").state == "dead"
        job_id = job.id
    assert queue.revive_dead_renders() == 0  # the family has had its three re-runs
    assert queue.dead_renders(UID) == (1, 0)  # and its last job counts as final
    assert len(keys(queue)) == 1 + RERUN_CAP


def test_rerun_is_idempotent_and_capped_per_family(queue: JobQueue) -> None:
    put(queue, "page_image:q")
    put(queue, "pageXimage:q:after:1")  # "_" must not act as a wildcard in the family count
    first = queue.get(1, UID)
    assert first is not None
    child = queue.rerun(first)
    assert child is not None and child.dedupe_key == "page_image:q:after:1"
    assert child.state == "queued" and child.payload == first.payload
    again = queue.rerun(first)
    assert again is not None and again.id == child.id  # the existing re-run, not a new one
    chain = child
    for _ in range(RERUN_CAP - 1):
        nxt = queue.rerun(chain)
        assert nxt is not None
        chain = nxt
    assert queue.rerun(chain) is None
    assert sum(k.startswith("page_image:q:after:") for k in keys(queue)) == RERUN_CAP
    other = put(queue, "render:z", uid=OTHER)
    row = queue.get(other, OTHER)
    assert row is not None
    assert queue.rerun(row) is not None  # another family has its own budget


def test_a_rerun_key_held_by_another_user_is_refused(queue: JobQueue) -> None:
    put(queue, "render:k:after:1", uid=OTHER)
    put(queue, "render:k")
    first = queue.get(2, UID)
    assert first is not None
    put(queue, "render:k:after:2", uid=OTHER)
    with pytest.raises(RefusedError):
        queue.rerun(first)


def test_dead_renders_is_scoped_to_the_user_and_account(queue: JobQueue) -> None:
    kill(queue, "a", "invalid_argument")
    kill(queue, "b", "internal", acct="01J0000000000000000000000B")
    kill(queue, "c", "internal", uid=OTHER)
    assert queue.dead_renders(UID) == (1, 1)
    assert queue.dead_renders(UID, acct="01J0000000000000000000000B") == (0, 1)
    assert queue.dead_renders(OTHER) == (0, 1)
    assert queue.dead_renders("0000000000000000") == (0, 0)


# ---- M2: per-user lease cap, final failures, families, lookups ---------------------------------


def test_per_uid_cap_skips_a_user_at_the_cap(queue: JobQueue, clock: Clock) -> None:
    """At most N leased jobs of a kind per user: the next user's job is claimed instead."""
    for n in range(3):
        put(queue, f"ocr:a{n}", kind="ocr")
        clock.advance(1)
    put(queue, "ocr:b0", kind="ocr", uid=OTHER)
    clock.advance(1)
    put(queue, "render:a", kind="render")
    cap = {"ocr": 2}
    leased = [queue.lease("w", per_uid=cap) for _ in range(4)]
    assert [(j.uid, j.dedupe_key) for j in leased if j is not None] == [
        (UID, "ocr:a0"),
        (UID, "ocr:a1"),
        (OTHER, "ocr:b0"),  # UID is at its cap of 2 leased ocr jobs
        (UID, "render:a"),  # other kinds are not capped
    ]
    assert queue.lease("w", per_uid=cap) is None
    first = leased[0]
    assert first is not None
    queue.complete(first.id, "w", {})
    third = queue.lease("w", per_uid=cap)
    assert third is not None and third.dedupe_key == "ocr:a2"
    with pytest.raises(InvalidArgumentError):
        queue.lease("w", per_uid={"ocr": 0})


def test_fail_final_is_dead_at_once(queue: JobQueue) -> None:
    put(queue, "ocr:x", kind="ocr")
    job = queue.lease("w")
    assert job is not None
    dead = queue.fail(job.id, "w", "refused", final=True)
    assert (dead.state, dead.error, dead.attempts) == ("dead", "refused", 1)


def test_rerun_takes_a_new_payload(queue: JobQueue) -> None:
    put(queue, "ocr:y", kind="ocr", payload={"force": False})
    job = queue.lease("w")
    assert job is not None
    queue.complete(job.id, "w", {"state": "done"})
    row = queue.get(job.id, UID)
    assert row is not None
    forced = queue.rerun(row, payload={"force": True})
    assert forced is not None and forced.payload == {"force": True}
    assert forced.dedupe_key == f"ocr:y:after:{job.id}"


def test_family_lists_the_base_and_its_reruns_only(queue: JobQueue) -> None:
    base = put(queue, "ocr:f")
    put(queue, "ocr:fg")  # a longer key sharing the prefix is another family
    put(queue, "ocr:f:after:x", uid=OTHER)  # another user's rows are never listed
    row = queue.get(base, UID)
    assert row is not None
    child = queue.rerun(row)
    assert child is not None
    assert [j.id for j in queue.family(UID, "ocr:f")] == [base, child.id]
    assert [j.id for j in queue.family(UID, child.dedupe_key)] == [base, child.id]
    assert queue.family(UID, "ocr:none") == []


def test_active_and_recent_errors(queue: JobQueue, clock: Clock) -> None:
    acct_b = "01J0000000000000000000000B"
    put(queue, "ocr_batch:1", kind="ocr_batch")
    put(queue, "ocr_batch:2", kind="ocr_batch", acct=acct_b)
    put(queue, "ocr:1", kind="ocr")
    assert [j.dedupe_key for j in queue.active(UID, acct=None, kind="ocr_batch")] == [
        "ocr_batch:1",
        "ocr_batch:2",
    ]
    assert [j.dedupe_key for j in queue.active(UID, acct=acct_b, kind="ocr_batch")] == [
        "ocr_batch:2"
    ]
    job = queue.lease("w", kinds=["ocr"])
    assert job is not None
    queue.fail(job.id, "w", "unavailable")
    clock.advance(5)
    put(queue, "ocr:2", kind="ocr")
    clock.advance(3600)
    job = queue.lease("w", kinds=["ocr"])
    assert job is not None and job.dedupe_key == "ocr:1"
    queue.fail(job.id, "w", "refused", final=True)
    errors = queue.recent_errors(UID, acct=None, kinds=["ocr", "ocr_batch"])
    assert [(kind, error) for kind, error, _at in errors] == [("ocr", "refused")]
    assert queue.recent_errors(UID, acct=None, kinds=[]) == []
    assert queue.recent_errors(OTHER, acct=None, kinds=["ocr"]) == []
    done = queue.lease("w", kinds=["ocr_batch"])
    assert done is not None
    queue.complete(done.id, "w", {})
    assert [j.dedupe_key for j in queue.active(UID, acct=None, kind="ocr_batch")] == ["ocr_batch:2"]


# ---- record_dead ------------------------------------------------------------------------------


def test_record_dead_names_finished_work(queue: JobQueue) -> None:
    acct = "01J0000000000000000000000A"
    dead = queue.record_dead(
        uid=UID, acct=acct, kind="ocr", dedupe_key="ocr:x", payload={"page": "p"}, error="refused"
    )
    assert (dead.state, dead.error, dead.attempts, dead.max_attempts) == ("dead", "refused", 0, 1)
    assert queue.lease("w") is None
    # An existing key is returned unchanged, and enqueue finds the dead job.
    again = queue.record_dead(
        uid=UID, acct=acct, kind="ocr", dedupe_key="ocr:x", payload={}, error="too_large"
    )
    assert (again.id, again.error) == (dead.id, "refused")
    assert queue.enqueue(uid=UID, acct=acct, kind="ocr", dedupe_key="ocr:x", payload={}).id == (
        dead.id
    )
    with pytest.raises(RefusedError):
        queue.record_dead(
            uid="fedcba9876543210",
            acct=acct,
            kind="ocr",
            dedupe_key="ocr:x",
            payload={},
            error="refused",
        )
    with pytest.raises(InvalidArgumentError):
        queue.record_dead(uid=UID, acct=acct, kind="ocr", dedupe_key="ocr:y", payload={}, error="")


# ---- priority (M2) ----------------------------------------------------------------------------

DOC_A = "00000000-0000-4000-8000-00000000000a"
DOC_B = "00000000-0000-4000-8000-00000000000b"
ACCT = "01J0000000000000000000000A"


def doc_job(q: JobQueue, key: str, document: str, **kw: Any) -> int:
    payload = {"document": document, "page": key}
    return put(q, key, payload=payload, **kw)


def priority_of(q: JobQueue, job_id: int, uid: str = UID) -> int:
    row = q.get(job_id, uid)
    assert row is not None
    return row.priority


def test_lease_takes_higher_priority_first_then_the_oldest(queue: JobQueue, clock: Clock) -> None:
    low = put(queue, "low", priority=-1)
    clock.advance(1)
    first = put(queue, "first")
    clock.advance(1)
    second = put(queue, "second")
    order = []
    while (job := queue.lease("w")) is not None:
        order.append(job.id)
    assert order == [first, second, low]


def test_a_failed_low_priority_job_still_waits_behind_normal_ones(
    queue: JobQueue, clock: Clock
) -> None:
    low = put(queue, "low", priority=-1)
    job = queue.lease("w")
    assert job is not None and job.id == low
    queue.fail(low, "w", "internal")
    clock.advance(3600)
    normal = put(queue, "normal")
    leased = queue.lease("w")
    assert leased is not None and leased.id == normal


def test_deprioritise_lowers_only_the_documents_waiting_jobs(queue: JobQueue) -> None:
    running = doc_job(queue, "running", DOC_A)
    assert queue.lease("w") is not None  # leased: keeps running at its priority
    waiting = doc_job(queue, "waiting", DOC_A)
    image = doc_job(queue, "image", DOC_A, kind="page_image")
    other_doc = doc_job(queue, "other", DOC_B)
    other_user = doc_job(queue, "theirs", DOC_A, uid=OTHER)
    other_acct = doc_job(queue, "acct-b", DOC_A, acct="01J0000000000000000000000B")
    ocr = doc_job(queue, "ocr", DOC_A, kind="ocr")
    kinds = ("render", "page_image")
    assert queue.deprioritise(uid=UID, acct=ACCT, document=DOC_A, kinds=kinds) == 2
    assert (priority_of(queue, waiting), priority_of(queue, image)) == (-1, -1)
    assert priority_of(queue, running) == 0
    assert priority_of(queue, other_doc) == priority_of(queue, other_acct) == 0
    assert priority_of(queue, ocr) == 0
    assert priority_of(queue, other_user, OTHER) == 0
    # Idempotent, and an empty kinds list touches nothing.
    assert queue.deprioritise(uid=UID, acct=ACCT, document=DOC_A, kinds=kinds) == 0
    assert queue.deprioritise(uid=UID, acct=ACCT, document=DOC_B, kinds=()) == 0
    with pytest.raises(InvalidArgumentError):
        queue.deprioritise(uid=UID, acct=ACCT, document="", kinds=kinds)


def test_enqueue_raises_a_waiting_jobs_priority_but_never_lowers_it(queue: JobQueue) -> None:
    job_id = put(queue, "k", priority=-1)
    same = queue.enqueue(uid=UID, acct=ACCT, kind="render", dedupe_key="k", payload={}, priority=-1)
    assert same.priority == -1
    raised = queue.enqueue(uid=UID, acct=ACCT, kind="render", dedupe_key="k", payload={})
    assert (raised.id, raised.priority) == (job_id, 0)
    again = queue.enqueue(
        uid=UID, acct=ACCT, kind="render", dedupe_key="k", payload={}, priority=-1
    )
    assert again.priority == 0
    with pytest.raises(RefusedError):  # another user's key is refused, not raised
        queue.enqueue(uid=OTHER, acct=ACCT, kind="render", dedupe_key="k", payload={})
    assert priority_of(queue, job_id) == 0


def test_enqueue_leaves_a_finished_jobs_priority_alone(queue: JobQueue) -> None:
    job_id = put(queue, "k", priority=-1)
    job = queue.lease("w")
    assert job is not None and job.id == job_id
    queue.complete(job_id, "w", {})
    row = queue.enqueue(uid=UID, acct=ACCT, kind="render", dedupe_key="k", payload={})
    assert (row.state, row.priority) == ("done", -1)


@pytest.mark.parametrize("bad", [11, -11, True, 0.5])
def test_priority_is_bounded(queue: JobQueue, bad: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        put(queue, "k", priority=bad)


def test_reruns_run_at_normal_priority_and_revivals_keep_theirs(queue: JobQueue) -> None:
    kill(queue, "render:revive", "internal")
    low = put(queue, "render:low", priority=-1, max_attempts=1)
    job = queue.lease("w")
    assert job is not None and job.id == low
    dead = queue.fail(low, "w", "internal")
    rerun = queue.rerun(dead)
    assert rerun is not None and rerun.priority == 0
    with queue._lock:
        queue._conn.execute("UPDATE jobs SET priority = -1 WHERE dedupe_key = 'render:revive'")
    assert queue.revive_dead_renders() == 1  # render:low was re-run already
    with queue._lock:
        rows = queue._conn.execute(
            "SELECT priority FROM jobs WHERE dedupe_key LIKE 'render:revive:after:%'"
        ).fetchall()
    assert [r["priority"] for r in rows] == [-1]


# ---- gone renders (M2) ------------------------------------------------------------------------


def finish(q: JobQueue, key: str, result: dict[str, Any]) -> int:
    job_id = put(q, key)
    job = q.lease("w", kinds=["render"])
    assert job is not None and job.id == job_id
    q.complete(job_id, "w", result)
    return job_id


def test_a_gone_render_is_not_re_run_for_the_same_version(queue: JobQueue) -> None:
    gone = queue.get(finish(queue, "render:gone", dict(GONE_RESULT)), UID)
    assert gone is not None and is_gone(gone.result)
    assert queue.rerun(gone) is None
    assert queue.rerun(gone, payload=dict(gone.payload)) is None  # the same version
    assert "render:gone:after:1" not in keys(queue)
    rendered = queue.get(finish(queue, "render:ok", {"png": "p", "bytes": 1}), UID)
    assert rendered is not None and not is_gone(rendered.result)
    assert queue.rerun(rendered) is not None  # a finished job whose PNG was lost re-runs
    other = {**gone.payload, "generation": 2}
    again = queue.rerun(gone, payload=other)  # another version holding the same page
    assert again is not None and again.payload == other
    assert again.dedupe_key == f"render:gone:after:{gone.id}"


def test_a_family_that_went_gone_is_not_revived_or_reported(queue: JobQueue) -> None:
    # An older run of the family died; a later one found the page version gone.
    kill(queue, "render:fam", "internal")
    first = queue.get(1, UID)
    assert first is not None
    child = queue.rerun(first)
    assert child is not None
    leased = queue.lease("w", kinds=["render"])
    assert leased is not None and leased.id == child.id
    queue.complete(child.id, "w", dict(GONE_RESULT))
    kill(queue, "render:fam:after:99", "internal")  # a dead leaf of the same family
    kill(queue, "render:other", "internal")
    assert queue.dead_renders(UID) == (0, 1)

    assert queue.revive_dead_renders() == 1  # render:other only

    assert not any(k.startswith("render:fam:after:99:") for k in keys(queue))
    assert "render:fam:after:99:after:4" not in keys(queue)


# ---- deferral ---------------------------------------------------------------------------------


def test_defer_puts_the_job_back_with_its_attempt_and_reason(queue: JobQueue, clock: Clock) -> None:
    job_id = put(queue, "ocr:a", kind="ocr")
    leased = queue.lease("w")
    assert leased is not None and leased.attempts == 1
    later = T0 + timedelta(hours=1)
    back = queue.defer(job_id, "w", until=later, reason="no_key")
    assert (back.state, back.attempts, back.error) == ("queued", 0, "deferred:no_key")
    assert (back.lease_owner, back.lease_expires_at, back.next_run_at) == (None, None, later)
    assert is_deferred(back) and deferral_reason(back) == "no_key"
    assert keys(queue) == {"ocr:a": "queued"}  # no new job, no re-run key
    assert queue.lease("w") is None
    # Deferred again and again, it never runs out of attempts.
    for _ in range(10):
        clock.advance(3600)
        again = queue.lease("w")
        assert again is not None and again.id == job_id and again.attempts == 1
        queue.defer(job_id, "w", until=clock.now + timedelta(hours=1), reason="no_key")
    final = queue.get(job_id, UID)
    assert final is not None and final.attempts == 0
    # Leased once more and finished, it is an ordinary done job.
    clock.advance(3600)
    assert queue.lease("w") is not None
    queue.complete(job_id, "w", {"state": "stored"})
    done = queue.get(job_id, UID)
    assert done is not None and done.error is None and not is_deferred(done)
    assert deferral_reason(done) is None


def test_defer_requires_the_lease_and_a_reason_code(queue: JobQueue) -> None:
    job_id = put(queue, "ocr:b", kind="ocr")
    with pytest.raises(LeaseLostError):
        queue.defer(job_id, "w", until=T0, reason="no_key")
    assert queue.lease("w") is not None
    for bad in ("", "No Key", "deferred:x", "x" * 41):
        with pytest.raises(InvalidArgumentError):
            queue.defer(job_id, "w", until=T0, reason=bad)
    with pytest.raises(InvalidArgumentError):
        queue.defer(job_id, "w", until=datetime(2026, 1, 1), reason="no_key")  # naive
    with pytest.raises(LeaseLostError):
        queue.defer(job_id, "other", until=T0, reason="no_key")
    # A time in the past makes it runnable at once.
    back = queue.defer(job_id, "w", until=T0 - timedelta(days=1), reason="no_key")
    assert back.next_run_at == T0
    assert queue.lease("w") is not None


def test_wake_makes_deferred_jobs_runnable(queue: JobQueue) -> None:
    acct = "01J0000000000000000000000A"
    ids = [put(queue, f"ocr:{n}", kind="ocr") for n in range(3)]
    other = put(queue, "ocr:other", kind="ocr", uid=OTHER)
    render = put(queue, "render:r")
    week = T0 + timedelta(days=7)
    for _ in range(5):
        job = queue.lease("w")
        assert job is not None
        queue.defer(job.id, "w", until=week, reason="cap_reached_user")
    assert queue.lease("w") is None
    counts = queue.deferred_counts(UID, acct=acct, kinds=("ocr", "render"))
    assert counts == {"ocr": {"cap_reached_user": 3}, "render": {"cap_reached_user": 1}}
    assert queue.deferred_counts(OTHER, kinds=("ocr",)) == {"ocr": {"cap_reached_user": 1}}
    assert queue.deferred_counts(UID, kinds=()) == {}
    assert queue.counts(UID)["ocr"] == {"queued": 3}  # deferred jobs are still queued
    # One job, by its owner only.
    assert queue.wake(ids[0], OTHER) is None
    woken = queue.wake(ids[0], UID)
    assert woken is not None and woken.next_run_at == T0 and woken.error is None
    assert not is_deferred(woken)
    # Every deferred job of one user and kind.
    assert queue.wake_deferred(UID, kinds=("ocr",)) == 2
    assert queue.wake_deferred(UID, acct="01J000000000000000000000ZZ", kinds=("ocr",)) == 0
    assert queue.wake_deferred(UID, kinds=()) == 0
    leased = set()
    for _ in range(3):
        job = queue.lease("w")
        assert job is not None
        leased.add(job.id)
    assert leased == set(ids)
    assert queue.lease("w") is None  # the other user's job and the render still wait
    for job_id, uid in ((other, OTHER), (render, UID)):
        row = queue.get(job_id, uid)
        assert row is not None and is_deferred(row)
    # A job that is not deferred is returned unchanged.
    plain = put(queue, "ocr:plain", kind="ocr", run_at=week)
    row = queue.wake(plain, UID)
    assert row is not None and row.next_run_at == week


def test_reruns_of_a_refused_key_do_not_count_against_the_cap(queue: JobQueue) -> None:
    put(queue, "ocr:k", kind="ocr")
    job = queue.lease("w")
    assert job is not None
    for _ in range(RERUN_CAP + 2):  # more refusals of the key than the cap
        dead = queue.fail(job.id, "w", "key_billing", final=True)
        nxt = queue.rerun(dead)
        assert nxt is not None
        job = queue.lease("w")
        assert job is not None and job.id == nxt.id
    # Any other death counts: the cap applies to those re-runs as before.
    for _ in range(RERUN_CAP):
        dead = queue.fail(job.id, "w", "refused", final=True)
        nxt = queue.rerun(dead)
        assert nxt is not None
        job = queue.lease("w")
        assert job is not None
    dead = queue.fail(job.id, "w", "refused", final=True)
    assert queue.rerun(dead) is None
