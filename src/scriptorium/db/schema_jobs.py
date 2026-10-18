"""The ``jobs`` table of ``scriptorium.sqlite`` (Architecture §6.2).

States: ``queued`` → ``leased`` → ``done``; a failed attempt goes to ``failed`` (waiting out
its backoff, then leasable again) or, after ``max_attempts``, to ``dead``; ``queued`` and
``failed`` jobs can be ``cancelled``. Timestamps are fixed-width ISO 8601 UTC text, so the
lease query compares them as text. ``payload`` and ``result`` are JSON objects.

``scriptorium.sqlite`` has one ``user_version`` for all of its tables, so these steps are
composed with the users/accounts/audit steps into a single append-only sequence for that file
(see ``db/migrations.py``); this module only defines the jobs part.
"""

from __future__ import annotations

from typing import Final

from scriptorium.db.migrations import Step

V1: Final = Step(
    name="jobs v1",
    sql="""
CREATE TABLE jobs(
    id                INTEGER PRIMARY KEY,
    uid               TEXT NOT NULL,
    acct              TEXT,
    kind              TEXT NOT NULL,
    dedupe_key        TEXT NOT NULL UNIQUE,
    payload           TEXT NOT NULL DEFAULT '{}',
    state             TEXT NOT NULL DEFAULT 'queued'
                      CHECK (state IN ('queued', 'leased', 'done', 'failed', 'dead', 'cancelled')),
    lease_owner       TEXT,
    lease_expires_at  TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts      INTEGER NOT NULL CHECK (max_attempts >= 1),
    next_run_at       TEXT NOT NULL,
    result            TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CHECK ((state = 'leased') = (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))
);
CREATE INDEX jobs_ready ON jobs(state, next_run_at, created_at);
CREATE INDEX jobs_lease ON jobs(state, lease_expires_at);
CREATE INDEX jobs_uid ON jobs(uid, state);
""",
)

JOBS_STEPS: Final[tuple[Step, ...]] = (V1,)
"""Append-only. Composed into the ``scriptorium.sqlite`` sequence by the server."""
