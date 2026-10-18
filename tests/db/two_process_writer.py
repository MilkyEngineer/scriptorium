"""The worker half of the two-process smoke test (the pattern of tests/soak, on the real stores).

Run as a script: ``two_process_writer.py <data dir> <seconds> <summary.json>``. The parent has
already migrated ``index.sqlite`` and ``scriptorium.sqlite``; this process opens both without
migrating, then until the time is up commits one short §7.3-style transaction per tick
(document row, pages, history) and leases and completes queued jobs. It writes a JSON summary
of transaction counts and errors. Synthetic rows only; no note content.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from scriptorium.jobs.queue import JobQueue
from scriptorium.library.index import DocumentRow, Index, PageRow

DOCUMENTS = 50
TICK_S = 0.02


def doc_uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012x}"


def page_uuid(n: int, i: int) -> str:
    return f"{n:08x}-0000-4000-9000-{i:012x}"


def is_busy(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    primary = code & 0xFF if isinstance(code, int) else None
    return primary in (5, 6) or "locked" in str(exc) or "busy" in str(exc)


def main(data: Path, seconds: float, summary: Path) -> int:
    rng = random.Random(7)
    index = Index.open(data / "index.sqlite", migrate=False)
    jobs = JobQueue.open(data / "scriptorium.sqlite", migrate=False)
    counts = {"commit": 0, "lease": 0, "complete": 0}
    busy = other = 0
    generation = 1000
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            generation += 1
            n = rng.randrange(DOCUMENTS)
            uuid = doc_uuid(n)
            now = datetime.now(UTC)
            try:
                with index.transaction():
                    index.upsert_document(
                        DocumentRow(
                            uuid=uuid,
                            type="DocumentType",
                            parent=None,
                            name=f"doc {n}",
                            last_modified=now,
                            page_count=3,
                            hash_root=f"{generation:064x}",
                            generation=generation,
                        )
                    )
                    index.replace_pages(
                        uuid,
                        [PageRow(page_uuid(n, i), uuid, i, f"{generation}-{i}") for i in range(3)],
                    )
                    index.append_history(uuid, generation, f"{generation:064x}", now)
                counts["commit"] += 1
                while (job := jobs.lease("writer")) is not None:
                    counts["lease"] += 1
                    jobs.complete(job.id, "writer", {"ok": True})
                    counts["complete"] += 1
            except sqlite3.Error as exc:
                if is_busy(exc):
                    busy += 1
                else:
                    other += 1
            time.sleep(TICK_S)
    finally:
        index.close()
        jobs.close()
    summary.write_text(json.dumps({"counts": counts, "busy": busy, "other": other}))
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), float(sys.argv[2]), Path(sys.argv[3])))
