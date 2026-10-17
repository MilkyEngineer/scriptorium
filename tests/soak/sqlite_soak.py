#!/usr/bin/env python3
"""Two-process SQLite WAL soak for spike S5 step 2. Standard library only; Python 3.12.

Mimics the server and the worker sharing one SQLite file under /data (Architecture §4.1, §6):

* ``--role writer`` (the worker): every second, one short ``BEGIN IMMEDIATE`` transaction that
  rewrites one random document of 500 and its pages (<= 20 rows) and bumps ``generation``; then
  leases and completes queued jobs, one short transaction each. Waits for the schema version.
* ``--role reader`` (the server): owns the schema migration; every 250 ms reads one random
  document and its pages, and enqueues a job in its own short transaction; every
  ``--integrity-every`` seconds, and once at the end, runs ``PRAGMA integrity_check`` and
  ``PRAGMA wal_checkpoint(PASSIVE)``.
* ``--role both``: migrates, then runs the two roles as two child processes of this script and
  combines their summaries.

Connections use exactly the §6 settings: ``journal_mode=WAL``, ``synchronous=NORMAL``,
``busy_timeout=5000``, plus ``foreign_keys=ON``. A ``database is locked`` error (SQLITE_BUSY or
SQLITE_LOCKED surviving the 5 s busy timeout) is what S5 counts as a failure.

On SIGTERM/SIGINT or when the duration elapses, a JSON summary is written to
``<data>/soak-<role>-<UTC timestamp>.json`` and printed. Pass criterion, as the ``passed`` field:
``busy_errors == 0 and other_errors == 0 and every integrity check is "ok"`` (the reader must
have run at least one). The process exits 0 when passed, 1 otherwise.

Nothing here reads or writes note content; the database holds synthetic rows only.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import signal
import sqlite3
import subprocess
import sys
import time
from array import array
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

DB_NAME = "scriptorium-soak.sqlite"
SCHEMA_VERSION = 1
DOCUMENTS = 500
MAX_PAGES = 20
WRITER_INTERVAL_S = 1.0
READER_INTERVAL_S = 0.25
HEARTBEAT_INTERVAL_S = 5.0
JOBS_PER_WRITER_TICK = 8
DONE_JOBS_KEPT = 1000
MIGRATION_WAIT_S = 120.0
SLOW_MS = 1000.0
PASS_CRITERION = (
    "busy_errors == 0 and other_errors == 0 and every integrity_check == 'ok' "
    "(reader: at least one check)"
)

SQLITE_BUSY = 5
SQLITE_LOCKED = 6

SCHEMA = """
CREATE TABLE documents(
    uuid TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    generation INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE pages(
    doc_uuid TEXT NOT NULL REFERENCES documents(uuid),
    idx INTEGER NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (doc_uuid, idx)
);
CREATE TABLE jobs(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    lease_until REAL,
    payload_len INTEGER NOT NULL
);
CREATE INDEX jobs_state ON jobs(state, id);
CREATE TABLE heartbeat(role TEXT PRIMARY KEY, ts REAL NOT NULL);
"""


# ------------------------------------------------------------------------------------ helpers


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def doc_uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def random_hash(rng: random.Random) -> str:
    return f"{rng.getrandbits(256):064x}"


def connect(db_path: Path) -> sqlite3.Connection:
    """Open with the Architecture §6 settings; transactions are explicit (autocommit mode)."""
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        raise SystemExit(f"journal_mode is {mode!r}, not 'wal': refusing to soak")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def settings(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],  # 1 == NORMAL
        "busy_timeout_ms": conn.execute("PRAGMA busy_timeout").fetchone()[0],
        "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
        "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
    }


def migrate(conn: sqlite3.Connection, rng: random.Random) -> None:
    """Create and seed the schema once, under BEGIN IMMEDIATE so concurrent starts are safe."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            conn.execute("COMMIT")
            return
        for statement in SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        now = time.time()
        for n in range(DOCUMENTS):
            uuid = doc_uuid(n)
            conn.execute(
                "INSERT INTO documents(uuid, hash, generation, updated_at) VALUES (?, ?, 0, ?)",
                (uuid, random_hash(rng), now),
            )
            conn.executemany(
                "INSERT INTO pages(doc_uuid, idx, hash) VALUES (?, ?, ?)",
                [(uuid, i, random_hash(rng)) for i in range(rng.randint(1, MAX_PAGES))],
            )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def wait_for_schema(conn: sqlite3.Connection, stopper: Stopper) -> bool:
    """The worker waits until the server has migrated (Architecture §6.1)."""
    deadline = time.monotonic() + MIGRATION_WAIT_S
    while not stopper.stopped:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return True
        if time.monotonic() > deadline:
            return False
        stopper.sleep_until(time.monotonic() + 1.0)
    return False


def filesystem_of(path: Path) -> dict[str, str | None]:
    """Best-effort fstype from /proc/self/mountinfo (longest mount-point prefix), for evidence."""
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"fstype": None, "mount_point": None}
    target = path.resolve()
    best: tuple[int, str, str] | None = None
    for line in text.splitlines():
        fields = line.split()
        if "-" not in fields[6:]:
            continue
        sep = fields.index("-", 6)
        if len(fields) <= sep + 1:
            continue
        point = Path(fields[4].replace("\\040", " "))
        if target != point and point not in target.parents:
            continue
        depth = len(point.parts)
        if best is None or depth >= best[0]:
            best = (depth, str(point), fields[sep + 1])
    if best is None:
        return {"fstype": None, "mount_point": None}
    return {"fstype": best[2], "mount_point": best[1]}


class Stopper:
    """Turns SIGTERM/SIGINT into a flag; sleeps in short slices so a stop is prompt."""

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.signal: str | None = None
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        self.signal = signal.Signals(signum).name

    @property
    def stopped(self) -> bool:
        return self.signal is not None or time.monotonic() >= self.deadline

    def sleep_until(self, moment: float) -> None:
        while not self.stopped:
            remaining = min(moment, self.deadline) - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.2))


@dataclass
class Stats:
    role: str
    transactions: dict[str, int] = field(default_factory=dict)
    busy_errors: int = 0
    other_errors: int = 0
    errors_by_kind: dict[str, int] = field(default_factory=dict)
    error_samples: list[str] = field(default_factory=list)
    latencies_ms: array[float] = field(default_factory=lambda: array("d"))
    slow_transactions: int = 0
    integrity_checks: list[dict[str, Any]] = field(default_factory=list)

    def run[T](
        self, kind: str, conn: sqlite3.Connection, body: Callable[[sqlite3.Connection], T]
    ) -> T | None:
        """Run ``body(conn)`` as one transaction, timing it and classifying any error."""
        started = time.perf_counter()
        try:
            result = body(conn)
        except sqlite3.Error as exc:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            self._record_error(kind, exc)
            return None
        elapsed = (time.perf_counter() - started) * 1000.0
        self.latencies_ms.append(elapsed)
        if elapsed >= SLOW_MS:
            self.slow_transactions += 1
        self.transactions[kind] = self.transactions.get(kind, 0) + 1
        return result

    def _record_error(self, kind: str, exc: sqlite3.Error) -> None:
        code = getattr(exc, "sqlite_errorcode", None)
        primary = code & 0xFF if isinstance(code, int) else None
        message = str(exc)
        busy = primary in (SQLITE_BUSY, SQLITE_LOCKED) or "database is locked" in message
        if busy:
            self.busy_errors += 1
        else:
            self.other_errors += 1
        name = getattr(exc, "sqlite_errorname", None) or type(exc).__name__
        key = f"{kind}:{name}"
        self.errors_by_kind[key] = self.errors_by_kind.get(key, 0) + 1
        sample = f"{iso(utc_now())} {key} {message}"
        if len(self.error_samples) < 20:
            self.error_samples.append(sample)
        print(f"[soak {self.role}] error {sample}", file=sys.stderr, flush=True)

    def latency(self) -> dict[str, float | int | None]:
        values = sorted(self.latencies_ms)
        if not values:
            return {"count": 0, "p50": None, "p99": None, "max": None}

        def pct(q: float) -> float:
            return round(values[min(len(values) - 1, int(q * len(values)))], 3)

        return {"count": len(values), "p50": pct(0.50), "p99": pct(0.99), "max": pct(1.0)}


# --------------------------------------------------------------------------------------- roles


def heartbeat(role: str) -> Callable[[sqlite3.Connection], None]:
    def body(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO heartbeat(role, ts) VALUES (?, ?) "
            "ON CONFLICT(role) DO UPDATE SET ts = excluded.ts",
            (role, time.time()),
        )
        conn.execute("COMMIT")

    return body


def run_writer(conn: sqlite3.Connection, stats: Stats, stopper: Stopper, seed: int) -> None:
    rng = random.Random(seed)

    def update_document(c: sqlite3.Connection) -> None:
        uuid = doc_uuid(rng.randrange(DOCUMENTS))
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "UPDATE documents SET hash = ?, generation = generation + 1, updated_at = ? "
            "WHERE uuid = ?",
            (random_hash(rng), time.time(), uuid),
        )
        c.execute("DELETE FROM pages WHERE doc_uuid = ?", (uuid,))
        c.executemany(
            "INSERT INTO pages(doc_uuid, idx, hash) VALUES (?, ?, ?)",
            [(uuid, i, random_hash(rng)) for i in range(rng.randint(1, MAX_PAGES))],
        )
        c.execute("COMMIT")

    def lease_job(c: sqlite3.Connection) -> int | None:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "UPDATE jobs SET state = 'leased', lease_until = ? "
            "WHERE id = (SELECT id FROM jobs WHERE state = 'queued' ORDER BY id LIMIT 1) "
            "RETURNING id",
            (time.time() + 60.0,),
        ).fetchone()
        c.execute("COMMIT")
        return None if row is None else int(row[0])

    def complete_job(job_id: int) -> Callable[[sqlite3.Connection], None]:
        def body(c: sqlite3.Connection) -> None:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "UPDATE jobs SET state = 'done', lease_until = NULL WHERE id = ? "
                "AND state = 'leased'",
                (job_id,),
            )
            c.execute("COMMIT")

        return body

    def prune_jobs(c: sqlite3.Connection) -> None:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "DELETE FROM jobs WHERE state = 'done' AND id < "
            "(SELECT coalesce(max(id), 0) FROM jobs) - ?",
            (DONE_JOBS_KEPT * 10,),
        )
        c.execute("COMMIT")

    next_tick = time.monotonic()
    next_heartbeat = next_tick
    ticks = 0
    while not stopper.stopped:
        stats.run("document_update", conn, update_document)
        for _ in range(JOBS_PER_WRITER_TICK):
            job_id = stats.run("job_lease", conn, lease_job)
            if job_id is None:
                break
            stats.run("job_complete", conn, complete_job(job_id))
        ticks += 1
        if ticks % 60 == 0:
            stats.run("job_prune", conn, prune_jobs)
        if time.monotonic() >= next_heartbeat:
            stats.run("heartbeat", conn, heartbeat("writer"))
            next_heartbeat += HEARTBEAT_INTERVAL_S
        next_tick += WRITER_INTERVAL_S
        stopper.sleep_until(next_tick)


def integrity(conn: sqlite3.Connection, stats: Stats) -> None:
    started = time.perf_counter()
    entry: dict[str, Any] = {"at": iso(utc_now())}
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        entry["result"] = "ok" if rows == [("ok",)] else "; ".join(str(r[0]) for r in rows[:20])
        busy, log, checkpointed = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        entry["checkpoint"] = {"busy": busy, "wal_frames": log, "checkpointed": checkpointed}
    except sqlite3.Error as exc:
        entry["result"] = f"error: {exc}"
        stats._record_error("integrity_check", exc)
    entry["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    stats.integrity_checks.append(entry)
    print(f"[soak {stats.role}] integrity {json.dumps(entry)}", file=sys.stderr, flush=True)


def run_reader(
    conn: sqlite3.Connection, stats: Stats, stopper: Stopper, seed: int, integrity_every: float
) -> None:
    rng = random.Random(seed)

    def read_document(c: sqlite3.Connection) -> None:
        uuid = doc_uuid(rng.randrange(DOCUMENTS))
        c.execute("BEGIN")
        row = c.execute("SELECT hash, generation FROM documents WHERE uuid = ?", (uuid,)).fetchone()
        pages = c.execute(
            "SELECT idx, hash FROM pages WHERE doc_uuid = ? ORDER BY idx", (uuid,)
        ).fetchall()
        c.execute("COMMIT")
        if row is None or not pages:
            raise sqlite3.DatabaseError("soak invariant broken: document or pages missing")

    def enqueue(c: sqlite3.Connection) -> None:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "INSERT INTO jobs(kind, state, lease_until, payload_len) VALUES ('ocr', 'queued', "
            "NULL, ?)",
            (rng.randint(64, 4096),),
        )
        c.execute("COMMIT")

    now = time.monotonic()
    next_tick = now
    next_heartbeat = now
    next_integrity = now + integrity_every
    while not stopper.stopped:
        stats.run("document_read", conn, read_document)
        stats.run("job_enqueue", conn, enqueue)
        current = time.monotonic()
        if current >= next_heartbeat:
            stats.run("heartbeat", conn, heartbeat("reader"))
            next_heartbeat += HEARTBEAT_INTERVAL_S
        if current >= next_integrity:
            integrity(conn, stats)
            next_integrity += integrity_every
        next_tick += READER_INTERVAL_S
        stopper.sleep_until(next_tick)
    integrity(conn, stats)  # always end with one, so every run has a verdict


# ------------------------------------------------------------------------------------ summary


def passed(summary: dict[str, Any]) -> bool:
    checks = summary["integrity_checks"]
    ok_checks = all(check.get("result") == "ok" for check in checks)
    enough_checks = summary["role"] != "reader" or len(checks) > 0
    return (
        summary["busy_errors"] == 0 and summary["other_errors"] == 0 and ok_checks and enough_checks
    )


def default_summary_path(data: Path, role: str, started: datetime) -> Path:
    return data / f"soak-{role}-{started.strftime('%Y%m%dT%H%M%SZ')}.json"


def emit(summary: dict[str, Any], path: Path) -> None:
    text = json.dumps(summary, indent=2, sort_keys=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)
    print(text, flush=True)
    print(f"[soak] summary written to {path}", file=sys.stderr, flush=True)


def run_role(args: argparse.Namespace, duration: float) -> int:
    started = utc_now()
    stopper = Stopper(time.monotonic() + duration)
    data: Path = args.data
    data.mkdir(parents=True, exist_ok=True)
    db_path = data / DB_NAME
    stats = Stats(role=args.role)
    seed = args.seed if args.seed is not None else int.from_bytes(os.urandom(4), "big")
    conn = connect(db_path)
    try:
        if args.role == "reader":
            migrate(conn, random.Random(seed))
            ready = True
        else:
            ready = wait_for_schema(conn, stopper)
        if ready:
            if args.role == "writer":
                run_writer(conn, stats, stopper, seed)
            else:
                run_reader(conn, stats, stopper, seed, args.integrity_every)
        db_settings = settings(conn)
    finally:
        conn.close()
    ended = utc_now()
    summary: dict[str, Any] = {
        "role": args.role,
        "pid": os.getpid(),
        "started_at": iso(started),
        "ended_at": iso(ended),
        "planned_duration_s": duration,
        "duration_s": round((ended - started).total_seconds(), 3),
        "stopped_by": stopper.signal or ("duration" if ready else "schema_wait_timeout"),
        "seed": seed,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "data_dir": str(data.resolve()),
        "filesystem": filesystem_of(data),
        "settings": db_settings,
        "transactions": {"total": sum(stats.transactions.values()), **stats.transactions},
        "busy_errors": stats.busy_errors,
        "other_errors": stats.other_errors if ready else stats.other_errors + 1,
        "errors_by_kind": stats.errors_by_kind,
        "error_samples": stats.error_samples,
        "latency_ms": stats.latency(),
        "slow_transactions_over_1s": stats.slow_transactions,
        "integrity_checks": stats.integrity_checks,
        "pass_criterion": PASS_CRITERION,
    }
    summary["passed"] = passed(summary)
    emit(summary, args.summary or default_summary_path(data, args.role, started))
    return 0 if summary["passed"] else 1


def run_both(args: argparse.Namespace, duration: float) -> int:
    """Migrate as the server would, then run writer and reader as two child processes."""
    started = utc_now()
    data: Path = args.data
    data.mkdir(parents=True, exist_ok=True)
    conn = connect(data / DB_NAME)
    try:
        migrate(conn, random.Random(args.seed))
    finally:
        conn.close()

    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    children: dict[str, tuple[subprocess.Popen[bytes], Path]] = {}
    for role in ("writer", "reader"):
        summary_path = data / f"soak-{role}-{stamp}.json"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--data",
            str(data),
            "--seconds",
            str(duration),
            "--role",
            role,
            "--integrity-every",
            str(args.integrity_every),
            "--summary",
            str(summary_path),
        ]
        if args.seed is not None:
            command += ["--seed", str(args.seed + (1 if role == "reader" else 0))]
        children[role] = (
            subprocess.Popen(command, stdout=subprocess.DEVNULL),  # noqa: S603 - own argv
            summary_path,
        )

    def forward(signum: int, _frame: FrameType | None) -> None:
        for process, _ in children.values():
            if process.poll() is None:
                process.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    results: dict[str, Any] = {}
    exit_codes: dict[str, int] = {}
    for role, (process, summary_path) in children.items():
        exit_codes[role] = process.wait()
        try:
            results[role] = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            results[role] = None
    complete = all(result is not None for result in results.values())
    combined: dict[str, Any] = {
        "role": "both",
        "started_at": iso(started),
        "ended_at": iso(utc_now()),
        "exit_codes": exit_codes,
        "busy_errors": sum(r["busy_errors"] for r in results.values() if r),
        "other_errors": sum(r["other_errors"] for r in results.values() if r)
        + (0 if complete else 1),
        "integrity_checks": [c for r in results.values() if r for c in r["integrity_checks"]],
        "transactions": {
            role: (r["transactions"]["total"] if r else None) for role, r in results.items()
        },
        "children": results,
        "pass_criterion": PASS_CRITERION,
    }
    combined["passed"] = (
        complete
        and all(r["passed"] for r in results.values() if r)
        and len(combined["integrity_checks"]) > 0
    )
    emit(combined, args.summary or default_summary_path(data, "both", started))
    return 0 if combined["passed"] else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Two-process SQLite WAL soak (Scriptorium spike S5 step 2)."
    )
    parser.add_argument("--data", type=Path, required=True, help="directory on the target fs")
    parser.add_argument("--role", choices=("writer", "reader", "both"), required=True)
    duration = parser.add_mutually_exclusive_group(required=True)
    duration.add_argument("--hours", type=float)
    duration.add_argument("--minutes", type=float)
    duration.add_argument("--seconds", type=float)
    parser.add_argument(
        "--integrity-every",
        type=float,
        default=3600.0,
        help="seconds between integrity checks in the reader (default 3600)",
    )
    parser.add_argument("--seed", type=int, help="random seed (default: random)")
    parser.add_argument("--summary", type=Path, help="summary path (default: in --data)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.hours is not None:
        duration = args.hours * 3600.0
    elif args.minutes is not None:
        duration = args.minutes * 60.0
    else:
        duration = float(args.seconds)
    if duration <= 0 or args.integrity_every <= 0:
        raise SystemExit("durations must be positive")
    if args.role == "both":
        return run_both(args, duration)
    return run_role(args, duration)


if __name__ == "__main__":
    sys.exit(main())
