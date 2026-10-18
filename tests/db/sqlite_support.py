"""What the local SQLite build supports, for skips (the image's build has FTS5)."""

from __future__ import annotations

import sqlite3

import pytest


def has_fts5() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return True


requires_fts5 = pytest.mark.skipif(not has_fts5(), reason="this SQLite build lacks FTS5")
