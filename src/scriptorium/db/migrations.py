"""Ordered schema migrations keyed by ``PRAGMA user_version`` (Architecture §6.1, §6.2).

A schema is a sequence of :class:`Step`; step ``i`` (0-based) takes a database from
``user_version == i`` to ``i + 1``, so a schema's version is ``len(steps)``. Sequences are
**append-only** once released: never edit or reorder a shipped step, add a new one.

The **server** owns migrations: it calls :func:`migrate` at start for ``scriptorium.sqlite`` and
on first open of each account's ``index.sqlite``. The worker never migrates; it calls
:func:`check_version`, which raises :class:`SchemaMismatch` until the server has caught up.

The schemas live in their own modules (``schema_account.ACCOUNT_STEPS``,
``schema_jobs.JOBS_STEPS``, and the users/accounts/audit steps); this module is the runner.
One file has one ``user_version``, so the tables that share ``scriptorium.sqlite`` (users,
accounts, audit, jobs) must be migrated by **one** composed, append-only sequence, never by two
independent ones (the second would see the first's version and skip its own steps).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from scriptorium.db.connect import immediate
from scriptorium.errors import ScriptoriumError
from scriptorium.logging import log_event

logger: Final = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Step:
    """One migration step: a short name for logs and the SQL it runs.

    ``sql`` holds one or more statements separated by ``;`` (trigger bodies are fine). It runs
    inside the runner's transaction, so it must not contain ``BEGIN``/``COMMIT`` or
    ``PRAGMA`` statements that are not transactional.
    """

    name: str
    sql: str


class SchemaMismatch(ScriptoriumError):  # noqa: N818 - name fixed by the M1 plan (§3.4)
    """The database's ``user_version`` is not the version this code expects.

    ``found < expected``: the server has not migrated yet (the worker waits and retries).
    ``found > expected``: the database was written by newer code; nothing here downgrades.
    """

    code = "internal"

    def __init__(self, found: int, expected: int) -> None:
        super().__init__(
            f"schema version {found}, code expects {expected}",
            hint="The server migrates at start; a newer database needs a newer image.",
        )
        self.found = found
        self.expected = expected


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def target_version(steps: Sequence[Step]) -> int:
    return len(steps)


def check_version(conn: sqlite3.Connection, steps: Sequence[Step]) -> int:
    """Return the version, or raise :class:`SchemaMismatch` if it is not ``len(steps)``."""
    found = current_version(conn)
    if found != target_version(steps):
        raise SchemaMismatch(found, target_version(steps))
    return found


def split_statements(sql: str) -> list[str]:
    """Split a script into complete statements (``CREATE TRIGGER … END;`` stays whole)."""
    statements: list[str] = []
    buffer = ""
    for piece in sql.split(";"):
        buffer += piece + ";"
        if sqlite3.complete_statement(buffer):
            if buffer.strip(" \t\r\n;"):
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip(" \t\r\n;"):
        raise ValueError("migration SQL ends with an incomplete statement")
    return statements


def migrate(conn: sqlite3.Connection, steps: Sequence[Step]) -> int:
    """Apply the steps the database has not seen, in one ``BEGIN IMMEDIATE`` transaction.

    Safe to call concurrently from two processes: the version is re-read under the write lock,
    so the second caller finds nothing to do. Any failure rolls back every step of this call
    (SQLite DDL and ``user_version`` are transactional). Returns the resulting version.
    """
    target = target_version(steps)
    with immediate(conn):
        found = current_version(conn)
        if found > target:
            raise SchemaMismatch(found, target)
        for version in range(found, target):
            for statement in split_statements(steps[version].sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={version + 1:d}")
    if found < target:
        log_event(logger, "schema_migrated", version=target, count=target - found)
    return target
