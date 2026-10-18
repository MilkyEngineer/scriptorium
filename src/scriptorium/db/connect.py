"""The one place SQLite connections are opened (Architecture §6.1 "Operation", spike S5).

Every module opens its database through :func:`open_sqlite`, so the pragmas the two-process
soak validated (WAL, ``synchronous=NORMAL``, ``busy_timeout=5000``, ``foreign_keys=ON``) cannot
drift between the server and the worker.

Connections are in autocommit mode (``isolation_level=None``): callers write in explicit, short
``BEGIN IMMEDIATE`` … ``COMMIT`` transactions, as the soak did, and plain reads run in their own
implicit read transaction. ``check_same_thread`` is off because the server and worker call the
stores from a thread pool; each store serialises use of its connection with a lock.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from scriptorium.errors import InvalidArgumentError, ScriptoriumError

type PragmaValue = str | int

DEFAULT_PRAGMAS: Final[Mapping[str, PragmaValue]] = {
    # busy_timeout first: switching to WAL takes a lock and must wait, not fail.
    "busy_timeout": 5000,
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
    "foreign_keys": "ON",
}
"""Architecture §6.1; validated by spike S5 (two processes, zero SQLITE_BUSY)."""

_PRAGMA_NAME: Final = re.compile(r"^[a-z_]{1,40}$")
_PRAGMA_VALUE: Final = re.compile(r"^[A-Za-z0-9_]{1,40}$")


class DatabaseSetupError(ScriptoriumError):
    """A database could not be opened with the required settings (e.g. WAL unavailable)."""

    code = "internal"


def open_sqlite(
    path: Path, *, pragmas: Mapping[str, PragmaValue] = DEFAULT_PRAGMAS
) -> sqlite3.Connection:
    """Open ``path`` in autocommit mode and apply ``pragmas`` in order.

    ``path`` is server-derived (``users/store.py`` or the data dir), never user input. If
    ``journal_mode`` is requested, the resulting mode is checked: a filesystem or URI that
    silently refuses WAL is an error, not a degraded mode.
    """
    for name, value in pragmas.items():
        if not _PRAGMA_NAME.fullmatch(name) or not _PRAGMA_VALUE.fullmatch(str(value)):
            raise ValueError(f"refusing pragma {name!r}")
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        for name, value in pragmas.items():
            row = conn.execute(f"PRAGMA {name}={value}").fetchone()  # validated above
            if name == "journal_mode" and str(row[0]).lower() != str(value).lower():
                raise DatabaseSetupError(
                    f"journal_mode is {row[0]!r}, not {value!r}",
                    hint="Put /data on a local POSIX filesystem (Architecture §6).",
                )
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One short write transaction: ``BEGIN IMMEDIATE``, ``COMMIT``, rollback on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def to_db_time(moment: datetime) -> str:
    """Fixed-width ISO 8601 UTC with microseconds, so text order is time order."""
    if moment.tzinfo is None:
        raise InvalidArgumentError("Timestamps must carry a time zone.")
    # isoformat always pads the year to four digits (strftime's %Y does not on every libc).
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"


def from_db_time(value: str) -> datetime:
    return datetime.fromisoformat(value.removesuffix("Z")).replace(tzinfo=UTC)


def opt_from_db_time(value: str | None) -> datetime | None:
    return None if value is None else from_db_time(value)


def utc_now() -> datetime:
    return datetime.now(UTC)
