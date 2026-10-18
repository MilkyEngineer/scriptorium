"""The users, accounts and audit tables of ``scriptorium.sqlite`` (Architecture §6.2).

``scriptorium.sqlite`` has one ``user_version`` for all of its tables, so this module also
defines :data:`SCRIPTORIUM_STEPS`, the single composed, append-only sequence for that file:
the steps here followed by the jobs steps (``db/schema_jobs.py``). Every opener of the file
(the server's ``GlobalStore``, the ``JobQueue``) migrates or checks against that composed
sequence, never against one part of it. New steps are appended to ``SCRIPTORIUM_STEPS``;
the per-table modules only keep the history readable.

Timestamps are fixed-width ISO 8601 UTC text (``db.connect.to_db_time``).
"""

from __future__ import annotations

from typing import Final

from scriptorium.db.migrations import Step
from scriptorium.db.schema_jobs import JOBS_STEPS

V1: Final = Step(
    name="users, accounts, audit v1",
    sql="""
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
)

GLOBAL_STEPS: Final[tuple[Step, ...]] = (V1,)
"""The users/accounts/audit part of the sequence; read-only history."""

SCRIPTORIUM_STEPS: Final[tuple[Step, ...]] = (*GLOBAL_STEPS, *JOBS_STEPS)
"""The whole ``scriptorium.sqlite`` sequence. Append-only; ``len`` is the file's version."""
