"""Per-account index schema, ``users/<uid>/accounts/<acct>/index.sqlite`` (Architecture §6.1).

Conventions shared by every table:

- Timestamps are fixed-width ISO 8601 UTC text (``db.connect.to_db_time``), so ``<`` on text is
  ``<`` in time and keyset cursors can compare them directly.
- ``documents.parent`` is ``NULL`` for the root, ``'trash'`` for the top of the trash, otherwise
  the parent folder's UUID, exactly as the cloud says (not a foreign key: cloud metadata may
  name a parent we have not seen yet).
- Page indices are 0-based here; tools add 1 (§11.0).
- Foreign keys are ``DEFERRABLE INITIALLY DEFERRED`` so one commit transaction (§7 step 3) can
  write history, pages and the document row in any order.
- ``transcripts`` has an explicit ``INTEGER PRIMARY KEY`` because it is the external content of
  ``transcripts_fts``: implicit rowids may be renumbered by ``VACUUM``, which would silently
  desynchronise the full-text index. ``doc_name`` and ``doc_path`` are denormalised from
  ``documents`` so the FTS triggers always delete exactly the values they indexed; the index
  module refreshes them when a document is renamed or moved.
- A page is identified by ``(document, page uuid)``, never by its UUID alone: page UUIDs
  collide whenever the same ``.rm`` files are uploaded more than once (open question 8,
  answered by a live library in V2; Scriptorium's own push and restore do this), so two
  documents can list the same page id with the same or different content. Transcripts are
  keyed the same way, plus the content hash.
"""

from __future__ import annotations

from typing import Final

from scriptorium.db.migrations import Step

V1: Final = Step(
    name="account index v1",
    sql="""
CREATE TABLE documents(
    uuid            TEXT PRIMARY KEY,
    type            TEXT NOT NULL CHECK (type IN ('DocumentType', 'CollectionType')),
    parent          TEXT,
    name            TEXT NOT NULL,
    last_modified   TEXT NOT NULL,
    last_opened     TEXT,
    deleted_at      TEXT,
    deletion_cause  TEXT CHECK (deletion_cause IN ('user', 'aged', 'unknown')),
    source_kind     TEXT,
    page_count      INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
    hash_root       TEXT,
    generation      INTEGER,
    CHECK ((deleted_at IS NULL) = (deletion_cause IS NULL))
);
CREATE INDEX documents_children ON documents(parent, type, name COLLATE NOCASE, uuid);
CREATE INDEX documents_recent ON documents(last_modified DESC, uuid);

CREATE TABLE history(
    uuid        TEXT NOT NULL REFERENCES documents(uuid) DEFERRABLE INITIALLY DEFERRED,
    generation  INTEGER NOT NULL,
    hash_root   TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    -- SHA-256 of this version's document index body, the blob that lists its files. It is
    -- not hash_root (a sync 1.5 index is addressed by the hash of its entries), so it is
    -- recorded here to make the version's file set recoverable (§6).
    index_sha256 TEXT,
    PRIMARY KEY (uuid, generation)
) WITHOUT ROWID;

CREATE TABLE pages(
    uuid          TEXT PRIMARY KEY,
    document      TEXT NOT NULL REFERENCES documents(uuid) DEFERRABLE INITIALLY DEFERRED,
    idx           INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    stable_since  TEXT,
    rendered_at   TEXT,
    png_path      TEXT,
    UNIQUE (document, idx)
);

CREATE TABLE transcripts(
    id             INTEGER PRIMARY KEY,
    page           TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    document       TEXT NOT NULL,
    text           TEXT NOT NULL,
    model          TEXT NOT NULL,
    mode           TEXT NOT NULL CHECK (mode IN ('user', 'server', 'lazy')),
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    cost_usd       REAL,
    created_at     TEXT NOT NULL,
    doc_name       TEXT NOT NULL,
    doc_path       TEXT NOT NULL,
    UNIQUE (page, content_hash)
);
CREATE INDEX transcripts_document ON transcripts(document);

CREATE VIRTUAL TABLE transcripts_fts USING fts5(
    text, doc_name, doc_path,
    content='transcripts', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER transcripts_ai AFTER INSERT ON transcripts BEGIN
    INSERT INTO transcripts_fts(rowid, text, doc_name, doc_path)
    VALUES (new.id, new.text, new.doc_name, new.doc_path);
END;
CREATE TRIGGER transcripts_ad AFTER DELETE ON transcripts BEGIN
    INSERT INTO transcripts_fts(transcripts_fts, rowid, text, doc_name, doc_path)
    VALUES ('delete', old.id, old.text, old.doc_name, old.doc_path);
END;
CREATE TRIGGER transcripts_au AFTER UPDATE OF text, doc_name, doc_path ON transcripts BEGIN
    INSERT INTO transcripts_fts(transcripts_fts, rowid, text, doc_name, doc_path)
    VALUES ('delete', old.id, old.text, old.doc_name, old.doc_path);
    INSERT INTO transcripts_fts(rowid, text, doc_name, doc_path)
    VALUES (new.id, new.text, new.doc_name, new.doc_path);
END;

CREATE TABLE links(
    id          INTEGER PRIMARY KEY,
    page        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    ref         TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX links_page ON links(page);

CREATE TABLE sync_state(
    k  TEXT PRIMARY KEY,
    v  TEXT NOT NULL
) WITHOUT ROWID;
""",
)

V2: Final = Step(
    name="pages and transcripts keyed by document",
    # SQLite cannot change a table's key in place, so both tables are rebuilt (create the new
    # table, copy, drop the old, rename) inside the runner's transaction. V1's keys are
    # narrower than V2's, so every row carries over unchanged. Transcript ids are copied as
    # they are: they are the FTS external-content rowids, so the full-text index stays in
    # step without a rebuild. The triggers are dropped with the old table and recreated.
    sql="""
CREATE TABLE pages_v2(
    uuid          TEXT NOT NULL,
    document      TEXT NOT NULL REFERENCES documents(uuid) DEFERRABLE INITIALLY DEFERRED,
    idx           INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    stable_since  TEXT,
    rendered_at   TEXT,
    png_path      TEXT,
    PRIMARY KEY (document, uuid),
    UNIQUE (document, idx)
) WITHOUT ROWID;
INSERT INTO pages_v2(uuid, document, idx, content_hash, stable_since, rendered_at, png_path)
    SELECT uuid, document, idx, content_hash, stable_since, rendered_at, png_path FROM pages;
DROP TABLE pages;
ALTER TABLE pages_v2 RENAME TO pages;
-- set_rendered marks one page version on every document that lists it (copies share a PNG).
CREATE INDEX pages_version ON pages(uuid, content_hash);

DROP TRIGGER transcripts_ai;
DROP TRIGGER transcripts_ad;
DROP TRIGGER transcripts_au;
CREATE TABLE transcripts_v2(
    id             INTEGER PRIMARY KEY,
    page           TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    document       TEXT NOT NULL,
    text           TEXT NOT NULL,
    model          TEXT NOT NULL,
    mode           TEXT NOT NULL CHECK (mode IN ('user', 'server', 'lazy')),
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    cost_usd       REAL,
    created_at     TEXT NOT NULL,
    doc_name       TEXT NOT NULL,
    doc_path       TEXT NOT NULL,
    UNIQUE (document, page, content_hash)
);
INSERT INTO transcripts_v2(id, page, content_hash, document, text, model, mode, input_tokens,
                           output_tokens, cost_usd, created_at, doc_name, doc_path)
    SELECT id, page, content_hash, document, text, model, mode, input_tokens, output_tokens,
           cost_usd, created_at, doc_name, doc_path FROM transcripts;
DROP TABLE transcripts;
ALTER TABLE transcripts_v2 RENAME TO transcripts;
-- No transcripts_document index any more: the UNIQUE key starts with document and serves it.
CREATE TRIGGER transcripts_ai AFTER INSERT ON transcripts BEGIN
    INSERT INTO transcripts_fts(rowid, text, doc_name, doc_path)
    VALUES (new.id, new.text, new.doc_name, new.doc_path);
END;
CREATE TRIGGER transcripts_ad AFTER DELETE ON transcripts BEGIN
    INSERT INTO transcripts_fts(transcripts_fts, rowid, text, doc_name, doc_path)
    VALUES ('delete', old.id, old.text, old.doc_name, old.doc_path);
END;
CREATE TRIGGER transcripts_au AFTER UPDATE OF text, doc_name, doc_path ON transcripts BEGIN
    INSERT INTO transcripts_fts(transcripts_fts, rowid, text, doc_name, doc_path)
    VALUES ('delete', old.id, old.text, old.doc_name, old.doc_path);
    INSERT INTO transcripts_fts(rowid, text, doc_name, doc_path)
    VALUES (new.id, new.text, new.doc_name, new.doc_path);
END;
""",
)

ACCOUNT_STEPS: Final[tuple[Step, ...]] = (V1, V2)
"""Append-only. ``len(ACCOUNT_STEPS)`` is the per-account schema version."""
