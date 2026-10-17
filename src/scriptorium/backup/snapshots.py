"""Enumerate and read host snapshots mounted read-only at ``/data/snapshots`` (§10.1, S5).

With ``SNAPSHOT_STRATEGY=host`` the operator's root cron job (``docker/host-snapshots/``)
creates read-only btrfs/ZFS snapshots of the data directory named
``YYYY-MM-DDTHH-MM-SSZ`` (UTC) and prunes them; the operator bind-mounts the directory that
holds them read-only into the containers. Scriptorium only lists and reads them.

Snapshot ids reach this module from tools, i.e. from the model's arguments, so they are
untrusted: :func:`resolve_snapshot` matches an id against the server's own enumeration and
returns the path found there; it never joins the caller's string onto a directory (§14).
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

__all__ = [
    "SNAPSHOT_ID_PATTERN",
    "Snapshot",
    "SnapshotError",
    "SnapshotListing",
    "UnknownSnapshot",
    "list_snapshots",
    "open_index_readonly",
    "probe_readonly",
    "resolve_snapshot",
]

#: Exactly what ``date -u +%Y-%m-%dT%H-%M-%SZ`` produces in the host scripts.
SNAPSHOT_ID_PATTERN = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\Z", re.ASCII)
_ID_FORMAT = "%Y-%m-%dT%H-%M-%SZ"


class SnapshotError(Exception):
    """A snapshot or a file inside it cannot be used."""


class UnknownSnapshot(SnapshotError, LookupError):  # noqa: N818 - reads as the condition
    """The id is not one of the enumerated snapshots. The message never echoes the id."""


@dataclass(frozen=True)
class Snapshot:
    """One host snapshot. ``created`` is parsed from the id (UTC); ``path`` is absolute."""

    id: str
    created: datetime
    path: Path
    readonly: bool


class SnapshotListing(list[Snapshot]):
    """Snapshots newest first, plus ``skipped``: names in the directory that are not snapshots.

    ``skipped`` lets the operator see noise (stray files, symlinks, misnamed directories) without
    Scriptorium ever acting on it.
    """

    def __init__(self, snapshots: list[Snapshot], skipped: list[str]) -> None:
        super().__init__(snapshots)
        self.skipped = skipped


def _parse_id(name: str) -> datetime | None:
    if not SNAPSHOT_ID_PATTERN.match(name):
        return None
    try:
        return datetime.strptime(name, _ID_FORMAT).replace(tzinfo=UTC)
    except ValueError:  # matches the shape but is not a real time, e.g. month 13
        return None


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def list_snapshots(snapshots_dir: Path) -> SnapshotListing:
    """List the snapshots directly under ``snapshots_dir``, newest first.

    An entry is a snapshot when its name is a valid id, it is a real directory (not a symlink),
    and its resolved path is a direct child of ``snapshots_dir``. Everything else is reported in
    ``skipped``. A missing ``snapshots_dir`` yields an empty listing (strategy not configured).
    ``readonly`` comes from ``os.access(W_OK)``, which reports a read-only mount (EROFS) and a
    read-only btrfs subvolume; nothing is written to find out.
    """
    root = snapshots_dir.resolve()
    if not root.is_dir():
        return SnapshotListing([], [])
    found: list[Snapshot] = []
    skipped: list[str] = []
    with os.scandir(root) as entries:
        for entry in entries:
            created = _parse_id(entry.name)
            if created is None or not entry.is_dir(follow_symlinks=False):
                skipped.append(entry.name)
                continue
            path = (root / entry.name).resolve()
            if path.parent != root:
                skipped.append(entry.name)
                continue
            readonly = not os.access(path, os.W_OK)
            found.append(Snapshot(id=entry.name, created=created, path=path, readonly=readonly))
    found.sort(key=lambda snap: snap.created, reverse=True)
    skipped.sort()
    return SnapshotListing(found, skipped)


def resolve_snapshot(snapshots_dir: Path, snapshot_id: str) -> Snapshot:
    """Return the enumerated snapshot whose id equals ``snapshot_id``, or raise UnknownSnapshot.

    The returned path comes from the enumeration, not from ``snapshot_id``, and is asserted to be
    inside ``snapshots_dir``.
    """
    if not isinstance(snapshot_id, str) or _parse_id(snapshot_id) is None:
        raise UnknownSnapshot("not a snapshot id")
    root = snapshots_dir.resolve()
    for snapshot in list_snapshots(root):
        if snapshot.id == snapshot_id:
            if not _inside(snapshot.path, root) or snapshot.path == root:
                raise SnapshotError("snapshot path escapes the snapshot directory")
            return snapshot
    raise UnknownSnapshot("no such snapshot")


def probe_readonly(snapshot: Snapshot) -> bool:
    """Return True if creating a file inside the snapshot fails. **Writes if it can.**

    For the S5 check and the operator's diagnostics only: production code relies on
    :attr:`Snapshot.readonly`. If the snapshot turns out to be writable, the probe file is
    removed again at once.
    """
    try:
        handle, name = tempfile.mkstemp(prefix=".scriptorium-ro-probe-", dir=snapshot.path)
    except OSError:
        return True
    os.close(handle)
    Path(name).unlink(missing_ok=True)
    return False


def open_index_readonly(
    snapshot: Snapshot, relative_db_path: str | PurePosixPath
) -> sqlite3.Connection:
    """Open a SQLite database inside a snapshot for reading, without writing to the snapshot.

    A read-only snapshot cannot hold the ``-wal``/``-shm`` files SQLite normally creates, and a
    snapshot of a live WAL database may contain committed transactions that exist only in its
    ``-wal`` file (not yet checkpointed into the main file). Two cases:

    - **No ``-wal`` file, or an empty one**: everything is in the main file. Open with
      ``mode=ro&immutable=1``: no locks, no ``-shm``, no change detection. Plain ``mode=ro``
      fails here ("attempt to write a readonly database") because SQLite would have to create
      the ``-wal``/``-shm`` files.
    - **A non-empty ``-wal`` file**: ``immutable=1`` would silently ignore it and show the
      database as of the last checkpoint (observed: committed rows missing). Open with plain
      ``mode=ro`` instead; SQLite (>= 3.22) then rebuilds the wal-index in private heap memory
      from the ``-wal`` file and reads the snapshot's full committed state. This path requires
      the snapshot to be read-only, otherwise SQLite could create or update ``-shm`` inside it.

    The connection has ``query_only`` set. ``relative_db_path`` is a server-derived path such as
    ``scriptorium.sqlite`` or ``users/<uid>/accounts/<acct>/index.sqlite``; it must be relative,
    without ``..``, and resolve to a regular file inside the snapshot.
    """
    relative = PurePosixPath(relative_db_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise SnapshotError("database path must be relative and inside the snapshot")
    root = snapshot.path.resolve()
    db_path = (root / relative).resolve()
    if not _inside(db_path, root) or db_path == root:
        raise SnapshotError("database path escapes the snapshot")
    if not db_path.is_file():
        raise SnapshotError("database file not found in the snapshot")

    wal_path = db_path.with_name(db_path.name + "-wal")
    try:
        wal_size = wal_path.stat().st_size if wal_path.is_file() else 0
    except OSError:
        wal_size = 0
    if wal_size > 0:
        if not snapshot.readonly:
            raise SnapshotError(
                "snapshot is writable and its database has a WAL; refusing to let SQLite write "
                "into it (mount the snapshot directory read-only)"
            )
        query = "mode=ro"
    else:
        query = "mode=ro&immutable=1"

    connection = sqlite3.connect(f"{db_path.as_uri()}?{query}", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
    except sqlite3.Error as exc:
        connection.close()
        raise SnapshotError(f"cannot read the database in the snapshot ({exc})") from exc
    return connection
