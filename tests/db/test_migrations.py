from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from scriptorium.db.migrations import (
    GLOBAL_STEPS,
    SchemaMismatch,
    check_version,
    migrate,
    schema_version,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(tmp_path / "test.sqlite", isolation_level=None)
    yield c
    c.close()


def tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_global_steps_apply_in_order_and_once(conn: sqlite3.Connection) -> None:
    assert migrate(conn, GLOBAL_STEPS) == len(GLOBAL_STEPS)
    assert schema_version(conn) == len(GLOBAL_STEPS)
    assert {"users", "accounts", "audit"} <= tables(conn)
    assert migrate(conn, GLOBAL_STEPS) == 0
    check_version(conn, GLOBAL_STEPS)


def test_steps_are_incremental(conn: sqlite3.Connection) -> None:
    steps = ["CREATE TABLE a (x);", "CREATE TABLE b (x);\n-- trailing comment\n"]
    assert migrate(conn, steps[:1]) == 1
    assert migrate(conn, steps) == 1
    assert tables(conn) == {"a", "b"}
    assert schema_version(conn) == 2


def test_check_version_refuses_a_mismatch(conn: sqlite3.Connection) -> None:
    with pytest.raises(SchemaMismatch):
        check_version(conn, GLOBAL_STEPS)


def test_a_database_newer_than_the_code_is_left_alone(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(SchemaMismatch):
        migrate(conn, GLOBAL_STEPS)
    assert schema_version(conn) == 99
    assert tables(conn) == set()


def test_a_failing_step_rolls_back_with_its_version(conn: sqlite3.Connection) -> None:
    steps = ["CREATE TABLE a (x);", "CREATE TABLE b (x);\nCREATE TABLE b (y);"]
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, steps)
    assert schema_version(conn) == 1
    assert tables(conn) == {"a"}
    assert not conn.in_transaction


def test_an_incomplete_step_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        migrate(conn, ["CREATE TABLE a (x"])
    assert schema_version(conn) == 0


def test_migrate_needs_autocommit_mode(tmp_path: Path) -> None:
    c = sqlite3.connect(tmp_path / "x.sqlite")
    with pytest.raises(ValueError):
        migrate(c, GLOBAL_STEPS)
    c.close()
