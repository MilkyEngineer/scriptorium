"""Schema migrations as ordered SQL steps keyed by ``PRAGMA user_version`` (§6.1, §6.2).

Step ``i`` of a list takes a database from ``user_version = i`` to ``i + 1``; a database is
current when its version equals the length of its list. Steps are append-only: a released
step is never edited, a change is a new step. The **server** applies them at start (the
global database) or on first open (an account index); the worker only checks the version and
waits for the server (§6.1).

Each step runs in one ``BEGIN IMMEDIATE`` transaction together with its version bump, so a
crash leaves the database at the previous version, never half-migrated.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Final

__all__ = ["GLOBAL_STEPS", "SchemaMismatch", "check_version", "migrate", "schema_version"]


class SchemaMismatch(RuntimeError):  # noqa: N818 - name fixed by the M1 contract (plan §3.4)
    """The database's ``user_version`` is not the one this code was built for."""


# ---- global database (scriptorium.sqlite) ---------------------------------------------------
#
# Workstream A owns users / accounts / audit (users/store.py:GlobalStore). Workstream B
# appends its steps (jobs, ...) to this same list: there is one user_version per database.

GLOBAL_STEPS: Final[list[str]] = [
    # 1: users, accounts, audit (Architecture §6.2). Timestamps are ISO 8601 UTC text.
    """
    CREATE TABLE users (
        uid                TEXT PRIMARY KEY CHECK (length(uid) = 16),
        iss                TEXT NOT NULL,
        sub                TEXT NOT NULL,
        display_name       TEXT NOT NULL,
        created_at         TEXT NOT NULL,
        disabled_at        TEXT,
        quota_gb           REAL,
        server_key_allowed INTEGER NOT NULL DEFAULT 0 CHECK (server_key_allowed IN (0, 1)),
        UNIQUE (iss, sub)
    ) STRICT;

    CREATE TABLE accounts (
        acct                TEXT PRIMARY KEY CHECK (length(acct) = 26),
        uid                 TEXT NOT NULL REFERENCES users (uid),
        rm_account_identity TEXT,
        active              INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        linked_at           TEXT NOT NULL,
        device_desc         TEXT NOT NULL
    ) STRICT;
    -- At most one active account per user (connect_remarkable refuses without replace=true),
    -- and a reMarkable account is actively linked to at most one user.
    CREATE UNIQUE INDEX accounts_one_active_per_user ON accounts (uid) WHERE active = 1;
    CREATE UNIQUE INDEX accounts_identity_active ON accounts (rm_account_identity)
        WHERE active = 1 AND rm_account_identity IS NOT NULL;
    CREATE INDEX accounts_by_identity ON accounts (rm_account_identity);

    -- Never arguments, content or secrets: identifiers is JSON of allowlisted keys only.
    CREATE TABLE audit (
        id          INTEGER PRIMARY KEY,
        ts          TEXT NOT NULL,
        uid         TEXT NOT NULL,
        tool        TEXT NOT NULL,
        duration_ms REAL NOT NULL,
        outcome     TEXT NOT NULL,
        identifiers TEXT NOT NULL
    ) STRICT;
    CREATE INDEX audit_by_ts ON audit (ts);
    CREATE INDEX audit_by_uid_ts ON audit (uid, ts);
    """,
]


def schema_version(conn: sqlite3.Connection) -> int:
    """The database's ``PRAGMA user_version``."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def check_version(conn: sqlite3.Connection, steps: Sequence[str]) -> None:
    """Raise :class:`SchemaMismatch` unless the database is exactly at ``len(steps)``."""
    have, want = schema_version(conn), len(steps)
    if have != want:
        raise SchemaMismatch(f"schema version is {have}, this code needs {want}")


def migrate(conn: sqlite3.Connection, steps: Sequence[str]) -> int:
    """Apply the steps the database has not seen; returns how many ran.

    ``conn`` must be in autocommit mode (``isolation_level=None``). A database newer than the
    code is refused rather than touched.
    """
    if conn.isolation_level is not None:
        raise ValueError("migrate() needs a connection with isolation_level=None")
    ran = 0
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = schema_version(conn)
            if version > len(steps):
                raise SchemaMismatch(
                    f"schema version is {version}, newer than this code ({len(steps)})"
                )
            if version == len(steps):
                conn.execute("COMMIT")
                return ran
            for statement in _statements(steps[version]):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version + 1:d}")
            conn.execute("COMMIT")
            ran += 1
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def _statements(script: str) -> list[str]:
    """Split a step into complete statements (``executescript`` would commit implicitly)."""
    out: list[str] = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            if pending.strip():
                out.append(pending.strip())
            pending = ""
    if pending.strip() and not _only_comments(pending):
        raise ValueError("migration step ends with an incomplete statement")
    return out


def _only_comments(text: str) -> bool:
    return all(not line.strip() or line.strip().startswith("--") for line in text.splitlines())
