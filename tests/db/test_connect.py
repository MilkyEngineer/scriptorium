from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from scriptorium.db.connect import (
    DEFAULT_PRAGMAS,
    DatabaseSetupError,
    from_db_time,
    immediate,
    open_sqlite,
    to_db_time,
)
from scriptorium.errors import InvalidArgumentError


def test_default_pragmas_are_the_s5_settings(tmp_path: Path) -> None:
    conn = open_sqlite(tmp_path / "x.sqlite")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.isolation_level is None  # explicit transactions only
        assert isinstance(conn.execute("SELECT 1 AS one").fetchone()["one"], int)
    finally:
        conn.close()


def test_pragmas_are_validated_before_use(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="refusing pragma"):
        open_sqlite(tmp_path / "x.sqlite", pragmas={"user_version": "1; DROP TABLE x"})
    with pytest.raises(ValueError, match="refusing pragma"):
        open_sqlite(tmp_path / "x.sqlite", pragmas={"Bad-Name": 1})


def test_refuses_a_journal_mode_it_did_not_get(tmp_path: Path) -> None:
    # An in-memory database cannot be WAL: SQLite answers "memory".
    with pytest.raises(DatabaseSetupError):
        open_sqlite(Path(":memory:"), pragmas=DEFAULT_PRAGMAS)


def test_immediate_commits_and_rolls_back(tmp_path: Path) -> None:
    conn = open_sqlite(tmp_path / "x.sqlite")
    try:
        conn.execute("CREATE TABLE t(x)")
        with immediate(conn):
            conn.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(RuntimeError), immediate(conn):
            conn.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError
        assert not conn.in_transaction
        assert [r[0] for r in conn.execute("SELECT x FROM t")] == [1]
    finally:
        conn.close()


def test_timestamps_are_fixed_width_and_sort_as_text() -> None:
    moments = [
        datetime(1, 1, 1, tzinfo=UTC),
        datetime(999, 12, 31, 23, 59, 59, tzinfo=UTC),
        datetime(2026, 10, 18, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 18, 9, 0, 0, 1, tzinfo=UTC),
        datetime(2026, 10, 18, 11, 0, tzinfo=timezone(timedelta(hours=1))),  # 10:00Z
    ]
    texts = [to_db_time(m) for m in moments]
    assert len({len(t) for t in texts}) == 1
    assert texts == sorted(texts)
    assert texts[2] == "2026-10-18T09:00:00.000000Z"
    assert [from_db_time(t) for t in texts] == moments


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(InvalidArgumentError):
        to_db_time(datetime(2026, 1, 1))


def test_two_connections_see_each_others_commits(tmp_path: Path) -> None:
    a = open_sqlite(tmp_path / "x.sqlite")
    b = open_sqlite(tmp_path / "x.sqlite")
    try:
        a.execute("CREATE TABLE t(x)")
        with immediate(a):
            a.execute("INSERT INTO t VALUES (1)")
        assert b.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    finally:
        a.close()
        b.close()
