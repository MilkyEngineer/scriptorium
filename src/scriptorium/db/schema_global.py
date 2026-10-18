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

OCR_V1: Final = Step(
    name="ocr spend and caps v1",
    sql="""
-- The user's own monthly cap on their own key (set_claude_api_key); NULL = none (§8.1).
ALTER TABLE users ADD COLUMN ocr_cap_usd REAL CHECK (ocr_cap_usd IS NULL OR ocr_cap_usd >= 0);
-- SHA-256 of the user's verified, ASCII-lowered email, recorded at sign-in only while that
-- email is listed in SERVER_KEY_USERS, so the worker can apply an email entry without the
-- token; never the email itself.
ALTER TABLE users ADD COLUMN server_key_email_sha256 TEXT
    CHECK (server_key_email_sha256 IS NULL OR length(server_key_email_sha256) = 64);

-- One row per transcribed page through a key; the caps are sums over the UTC calendar month
-- of `at`. Kept when a transcript is replaced or a user clears their key (§6.2, §8.1).
CREATE TABLE ocr_spend (
    id            INTEGER PRIMARY KEY,
    uid           TEXT NOT NULL CHECK (length(uid) = 16),
    mode          TEXT NOT NULL CHECK (mode IN ('user', 'server')),
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    cost_usd      REAL NOT NULL CHECK (cost_usd >= 0),
    at            TEXT NOT NULL
) STRICT;
CREATE INDEX ocr_spend_by_uid_at ON ocr_spend (uid, at);
CREATE INDEX ocr_spend_by_at ON ocr_spend (at);
""",
)

GLOBAL_STEPS: Final[tuple[Step, ...]] = (V1,)
"""The M1 users/accounts/audit part of the sequence; read-only history."""

PRIORITY_CONFIRMATIONS: Final = Step(
    name="jobs.priority, confirmations, accounts.removed_at",
    sql="""
-- §6.2 (M2): 0 normal, -1 for renders of tombstoned, trashed or excluded documents. The
-- lease query orders runnable jobs by priority, then arrival.
ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
CREATE INDEX jobs_lease_order ON jobs (priority DESC, created_at, id)
    WHERE state IN ('queued', 'failed');

-- §11.0: spent confirmation tokens (SHA-256 of the decoded token), so one executes once.
CREATE TABLE confirmations (
    sha256  TEXT PRIMARY KEY CHECK (length(sha256) = 64),
    used_at TEXT NOT NULL
) STRICT, WITHOUT ROWID;
CREATE INDEX confirmations_by_used_at ON confirmations (used_at);

-- disconnect_remarkable(keep_archive=false) moved the account directory to
-- users/<uid>/removed/<acct>-<ts>/ at this time, and the account is never used again.
ALTER TABLE accounts ADD COLUMN removed_at TEXT;
""",
)
"""M2 workstream I: job priority, spent confirmation tokens, removed accounts."""

OCR_SPEND_BATCH_ITEMS: Final = Step(
    name="ocr_spend batch items",
    sql="""
-- A batch item's spend is recorded once, however many times its ocr_batch job reads the
-- results (a stream that fails half-way is read again from the start): rows for batch items
-- carry the batch id and the item's custom id, unique per user. NULL for single calls.
ALTER TABLE ocr_spend ADD COLUMN batch_id TEXT
    CHECK (batch_id IS NULL OR length(batch_id) BETWEEN 1 AND 128);
ALTER TABLE ocr_spend ADD COLUMN custom_id TEXT
    CHECK (custom_id IS NULL OR length(custom_id) BETWEEN 1 AND 64);
CREATE UNIQUE INDEX ocr_spend_batch_item ON ocr_spend (uid, batch_id, custom_id)
    WHERE batch_id IS NOT NULL;
""",
)
"""M2 review: a batch item's spend is recorded once (``SpendLedger.record`` with ids)."""

SCRIPTORIUM_STEPS: Final[tuple[Step, ...]] = (
    *GLOBAL_STEPS,
    *JOBS_STEPS,
    OCR_V1,
    PRIORITY_CONFIRMATIONS,
    OCR_SPEND_BATCH_ITEMS,
)
"""The whole ``scriptorium.sqlite`` sequence. Append-only; ``len`` is the file's version."""
