"""Two processes on the same files for a few seconds: zero SQLITE_BUSY (spike S5's rule).

The subprocess plays the worker (index commits, job leases); this process plays the server
(index reads, searches, job enqueues). Both use the real stores and ``open_sqlite`` pragmas.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scriptorium.jobs.queue import JobQueue
from scriptorium.library.index import DocumentRow, Index, PageRow
from tests.db import two_process_writer as writer
from tests.db.sqlite_support import requires_fts5

SECONDS = 3.0


@requires_fts5
def test_writer_and_reader_processes_see_no_busy_errors(tmp_path: Path) -> None:
    index = Index.open(tmp_path / "index.sqlite", migrate=True)
    jobs = JobQueue.open(tmp_path / "scriptorium.sqlite", migrate=True)
    t0 = datetime.now(UTC)
    for n in range(writer.DOCUMENTS):
        uuid = writer.doc_uuid(n)
        with index.transaction():
            index.upsert_document(
                DocumentRow(uuid, "DocumentType", None, f"doc {n}", t0, page_count=1)
            )
            index.replace_pages(uuid, [PageRow(writer.page_uuid(n, 0), uuid, 0, "seed")])
            index.put_transcript(
                uuid, writer.page_uuid(n, 0), "seed", f"seed text {n}", "m", "lazy"
            )

    summary_path = tmp_path / "writer.json"
    child = subprocess.Popen(  # noqa: S603 - fixed argv, our own script
        [sys.executable, writer.__file__, str(tmp_path), str(SECONDS), str(summary_path)],
        stderr=subprocess.PIPE,
        text=True,
    )
    busy = other = reads = enqueued = 0
    deadline = time.monotonic() + SECONDS
    n = 0
    try:
        while time.monotonic() < deadline:
            n += 1
            try:
                page = index.children(
                    None, include_trash=False, include_deleted=False, cursor=None, limit=20
                )
                assert page.items
                index.document(writer.doc_uuid(n % writer.DOCUMENTS))
                index.pages(writer.doc_uuid(n % writer.DOCUMENTS))
                index.recent(t0 - timedelta(days=1), None, 10)
                index.search("seed", cursor=None, limit=5)
                reads += 1
                jobs.enqueue(
                    uid="0123456789abcdef", acct=None, kind="render", dedupe_key=f"k{n}", payload={}
                )
                enqueued += 1
            except sqlite3.Error as exc:
                if writer.is_busy(exc):
                    busy += 1
                else:
                    other += 1
            time.sleep(0.005)
        _, stderr = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
        index.close()
        jobs.close()

    assert child.returncode == 0, stderr
    summary = json.loads(summary_path.read_text())
    assert (busy, other) == (0, 0)
    assert (summary["busy"], summary["other"]) == (0, 0)
    assert reads >= 50 and enqueued >= 50
    assert summary["counts"]["commit"] >= 20
    assert summary["counts"]["lease"] == summary["counts"]["complete"] > 0

    check = Index.open(tmp_path / "index.sqlite", migrate=False)
    try:
        conn = check._conn
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        check.close()
