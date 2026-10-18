from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from scriptorium.db.connect import open_sqlite
from scriptorium.db.migrations import (
    SchemaMismatch,
    Step,
    check_version,
    current_version,
    migrate,
    split_statements,
)
from scriptorium.db.schema_account import ACCOUNT_STEPS
from scriptorium.db.schema_jobs import JOBS_STEPS
from scriptorium.errors import ScriptoriumError
from tests.db.sqlite_support import requires_fts5

TOY: tuple[Step, ...] = (
    Step("one", "CREATE TABLE a(x INTEGER); INSERT INTO a VALUES (1);"),
    Step("two", "ALTER TABLE a ADD COLUMN y TEXT DEFAULT 'semi;colon';"),
    Step(
        "three",
        """
        CREATE TABLE log(x);
        CREATE TRIGGER a_ai AFTER INSERT ON a BEGIN
            INSERT INTO log VALUES (new.x);
            INSERT INTO log VALUES (new.x + 1);
        END;
        """,
    ),
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_sqlite(tmp_path / "db.sqlite")
    yield c
    c.close()


def schema_of(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type, name"
        )
    ]


def test_split_keeps_triggers_and_literals_whole() -> None:
    statements = split_statements(TOY[2].sql)
    assert len(statements) == 2
    assert statements[1].startswith("CREATE TRIGGER") and statements[1].endswith("END;")
    assert split_statements(TOY[1].sql) == [TOY[1].sql.strip()]
    with pytest.raises(ValueError, match="incomplete"):
        split_statements("CREATE TABLE x(a); CREATE TRIGGER t AFTER INSERT ON x BEGIN SELECT 1")


def test_migrate_from_empty_applies_every_step(conn: sqlite3.Connection) -> None:
    assert current_version(conn) == 0
    assert migrate(conn, TOY) == 3
    assert current_version(conn) == 3
    conn.execute("INSERT INTO a(x) VALUES (5)")
    assert [r[0] for r in conn.execute("SELECT x FROM log ORDER BY x")] == [5, 6]
    assert conn.execute("SELECT y FROM a WHERE x = 1").fetchone()[0] == "semi;colon"


@pytest.mark.parametrize("start", range(len(TOY) + 1))
def test_migrate_from_each_prior_version(tmp_path: Path, start: int) -> None:
    fresh = open_sqlite(tmp_path / "fresh.sqlite")
    stepped = open_sqlite(tmp_path / "stepped.sqlite")
    try:
        migrate(fresh, TOY)
        migrate(stepped, TOY[:start])
        assert current_version(stepped) == start
        migrate(stepped, TOY)
        assert schema_of(stepped) == schema_of(fresh)
        assert current_version(stepped) == len(TOY)
    finally:
        fresh.close()
        stepped.close()


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    migrate(conn, TOY)
    before = schema_of(conn)
    assert migrate(conn, TOY) == 3
    assert schema_of(conn) == before
    assert conn.execute("SELECT count(*) FROM a").fetchone()[0] == 1


def test_a_failing_step_rolls_back_everything_of_that_call(conn: sqlite3.Connection) -> None:
    migrate(conn, TOY[:1])
    broken = (*TOY[:2], Step("broken", "CREATE TABLE ok(x); CREATE TABLE a(x);"))
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, broken)
    assert current_version(conn) == 1
    assert "ok" not in {name for _, name, _ in schema_of(conn)}
    columns = [r[1] for r in conn.execute("PRAGMA table_info(a)")]
    assert columns == ["x"]  # step two was rolled back too
    assert not conn.in_transaction


def test_newer_database_is_refused(conn: sqlite3.Connection) -> None:
    migrate(conn, TOY)
    with pytest.raises(SchemaMismatch) as caught:
        migrate(conn, TOY[:2])
    assert (caught.value.found, caught.value.expected) == (3, 2)
    assert isinstance(caught.value, ScriptoriumError)
    assert current_version(conn) == 3


def test_check_version(conn: sqlite3.Connection) -> None:
    with pytest.raises(SchemaMismatch) as caught:
        check_version(conn, TOY)
    assert (caught.value.found, caught.value.expected) == (0, 3)
    migrate(conn, TOY)
    assert check_version(conn, TOY) == 3


def test_concurrent_migrations_apply_each_step_once(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    open_sqlite(path).close()
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def run() -> None:
        c = open_sqlite(path)
        try:
            barrier.wait()
            migrate(c, TOY)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    c = open_sqlite(path)
    try:
        assert current_version(c) == 3
        assert c.execute("SELECT count(*) FROM a").fetchone()[0] == 1
    finally:
        c.close()


def _prefixes(steps: Sequence[Step]) -> list[int]:
    return list(range(len(steps) + 1))


@requires_fts5
@pytest.mark.parametrize("start", _prefixes(ACCOUNT_STEPS))
def test_account_schema_from_each_prior_version(tmp_path: Path, start: int) -> None:
    fresh = open_sqlite(tmp_path / "fresh.sqlite")
    stepped = open_sqlite(tmp_path / "stepped.sqlite")
    try:
        migrate(fresh, ACCOUNT_STEPS)
        migrate(stepped, ACCOUNT_STEPS[:start])
        migrate(stepped, ACCOUNT_STEPS)
        assert schema_of(stepped) == schema_of(fresh)
        names = {name for kind, name, _ in schema_of(fresh) if kind == "table"}
        assert {
            "documents",
            "history",
            "pages",
            "transcripts",
            "transcripts_fts",
            "links",
            "sync_state",
        } <= names
        assert fresh.execute("PRAGMA foreign_key_check").fetchall() == []
        assert fresh.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        fresh.close()
        stepped.close()


@pytest.mark.parametrize("start", _prefixes(JOBS_STEPS))
def test_jobs_schema_from_each_prior_version(tmp_path: Path, start: int) -> None:
    fresh = open_sqlite(tmp_path / "fresh.sqlite")
    stepped = open_sqlite(tmp_path / "stepped.sqlite")
    try:
        migrate(fresh, JOBS_STEPS)
        migrate(stepped, JOBS_STEPS[:start])
        migrate(stepped, JOBS_STEPS)
        assert schema_of(stepped) == schema_of(fresh)
        columns = {r[1] for r in fresh.execute("PRAGMA table_info(jobs)")}
        assert columns == {
            "id",
            "uid",
            "acct",
            "kind",
            "dedupe_key",
            "payload",
            "state",
            "lease_owner",
            "lease_expires_at",
            "attempts",
            "max_attempts",
            "next_run_at",
            "result",
            "error",
            "created_at",
            "updated_at",
        }
    finally:
        fresh.close()
        stepped.close()


def test_jobs_steps_compose_after_other_steps(conn: sqlite3.Connection) -> None:
    """scriptorium.sqlite runs one composed sequence; the jobs steps must not assume they
    are first."""
    composed = (*TOY, *JOBS_STEPS)
    assert migrate(conn, composed) == len(composed)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
