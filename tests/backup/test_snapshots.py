from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scriptorium.backup.snapshots import (
    Snapshot,
    SnapshotError,
    UnknownSnapshot,
    list_snapshots,
    open_index_readonly,
    probe_readonly,
    resolve_snapshot,
)

IDS = ["2026-10-16T03-00-00Z", "2026-10-17T03-00-00Z", "2026-09-30T23-59-59Z"]
INDEX = "users/0123456789abcdef/accounts/01JABCDEF/index.sqlite"

needs_non_root = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root ignores directory modes"
)


@pytest.fixture
def read_only_dirs() -> Iterator[list[Path]]:
    """Directories appended here are made read-only by the test and writable again afterwards."""
    paths: list[Path] = []
    yield paths
    for path in paths:
        path.chmod(0o755)


@pytest.fixture
def snap_dir(tmp_path: Path) -> Path:
    root = tmp_path / "snapshots"
    root.mkdir()
    for snap_id in IDS:
        (root / snap_id).mkdir()
    (root / "notes.txt").write_text("noise")
    (root / ".snapshot.lock").write_text("")
    (root / "2026-13-01T00-00-00Z").mkdir()  # right shape, impossible month
    (root / "scriptorium-20261017T0300Z").mkdir()  # another naming scheme
    (root / "2026-10-18T00-00-00Z").write_text("a file, not a snapshot")
    (root / "2026-10-19T00-00-00Z").symlink_to(root / IDS[0])
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "2026-10-20T00-00-00Z").symlink_to(outside)
    return root


def test_enumeration_keeps_only_snapshot_directories_newest_first(snap_dir: Path) -> None:
    listing = list_snapshots(snap_dir)
    assert [s.id for s in listing] == sorted(IDS, reverse=True)
    assert listing[0].created == datetime(2026, 10, 17, 3, 0, 0, tzinfo=UTC)
    assert all(s.path == snap_dir.resolve() / s.id for s in listing)
    assert listing.skipped == sorted(
        [
            ".snapshot.lock",
            "2026-10-18T00-00-00Z",
            "2026-10-19T00-00-00Z",
            "2026-10-20T00-00-00Z",
            "2026-13-01T00-00-00Z",
            "notes.txt",
            "scriptorium-20261017T0300Z",
        ]
    )


def test_missing_directory_lists_nothing(tmp_path: Path) -> None:
    listing = list_snapshots(tmp_path / "absent")
    assert list(listing) == []
    assert listing.skipped == []


def test_resolve_returns_the_enumerated_snapshot(snap_dir: Path) -> None:
    snap = resolve_snapshot(snap_dir, IDS[1])
    assert snap.id == IDS[1]
    assert snap.path == snap_dir.resolve() / IDS[1]


@pytest.mark.parametrize(
    "bad_id",
    [
        "2026-10-18T00-00-00Z",  # valid shape, a file
        "2026-10-19T00-00-00Z",  # valid shape, a symlink
        "2026-10-21T00-00-00Z",  # valid shape, absent
        "../outside",
        "..",
        f"{IDS[0]}/../{IDS[1]}",
        f"../snapshots/{IDS[0]}",
        "/etc",
        "",
        f"{IDS[0]}\n",
        "٢٠٢٦-10-17T03-00-00Z",  # non-ASCII digits
        "scriptorium-20261017T0300Z",
    ],
)
def test_resolve_rejects_unknown_ids_and_traversal(snap_dir: Path, bad_id: str) -> None:
    with pytest.raises(UnknownSnapshot) as excinfo:
        resolve_snapshot(snap_dir, bad_id)
    if bad_id:
        assert bad_id not in str(excinfo.value)


def test_writable_snapshot_is_not_readonly(snap_dir: Path) -> None:
    snap = resolve_snapshot(snap_dir, IDS[0])
    assert snap.readonly is False
    assert probe_readonly(snap) is False
    assert list(snap.path.iterdir()) == []  # the probe cleaned up


@needs_non_root
def test_read_only_snapshot_is_detected(snap_dir: Path, read_only_dirs: list[Path]) -> None:
    target = snap_dir / IDS[0]
    target.chmod(0o555)
    read_only_dirs.append(target)
    snap = resolve_snapshot(snap_dir, IDS[0])
    assert snap.readonly is True
    assert probe_readonly(snap) is True


def _make_wal_db(directory: Path, *, leave_wal: bool) -> int:
    """Create a WAL database; with ``leave_wal`` two rows exist only in its -wal file."""
    directory.mkdir(parents=True, exist_ok=True)
    live = directory / "live.sqlite"
    conn = sqlite3.connect(live, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE documents(uuid TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO documents VALUES ('00000000-0000-4000-8000-000000000001')")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("INSERT INTO documents VALUES ('00000000-0000-4000-8000-000000000002')")
    conn.execute("INSERT INTO documents VALUES ('00000000-0000-4000-8000-000000000003')")
    target = directory / "index.sqlite"
    if leave_wal:
        # A crash-consistent snapshot of a live database: main file plus -wal and -shm.
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(f"{live}{suffix}", f"{target}{suffix}")
        conn.close()
    else:
        conn.close()  # last connection checkpoints and removes -wal/-shm
        shutil.copyfile(live, target)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{live}{suffix}").unlink(missing_ok=True)
    return 3


def _contents(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in directory.iterdir()}


def _seal(snapshot_root: Path, read_only_dirs: list[Path]) -> None:
    """Make everything in the snapshot read-only, like a btrfs -r snapshot."""
    for path in snapshot_root.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    for path in sorted(
        (p for p in snapshot_root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts)
    ):
        read_only_dirs.append(path)
    read_only_dirs.append(snapshot_root)
    for path in reversed(read_only_dirs):
        path.chmod(0o555)


@needs_non_root
@pytest.mark.parametrize("leave_wal", [False, True], ids=["checkpointed", "with-wal"])
def test_open_index_readonly_reads_the_full_committed_state(
    snap_dir: Path, read_only_dirs: list[Path], leave_wal: bool
) -> None:
    snap_root = snap_dir / IDS[1]
    expected = _make_wal_db((snap_root / INDEX).parent, leave_wal=leave_wal)
    _seal(snap_root, read_only_dirs)
    before = _contents((snap_root / INDEX).parent)

    snap = resolve_snapshot(snap_dir, IDS[1])
    assert snap.readonly
    conn = open_index_readonly(snap, INDEX)
    try:
        assert conn.execute("SELECT count(*) FROM documents").fetchone() == (expected,)
        with pytest.raises(sqlite3.Error):
            conn.execute("INSERT INTO documents VALUES ('x')")
    finally:
        conn.close()
    assert _contents((snap_root / INDEX).parent) == before


def test_open_index_refuses_wal_in_a_writable_snapshot(snap_dir: Path) -> None:
    snap_root = snap_dir / IDS[1]
    _make_wal_db((snap_root / INDEX).parent, leave_wal=True)
    snap = resolve_snapshot(snap_dir, IDS[1])
    assert not snap.readonly
    with pytest.raises(SnapshotError, match="read-only"):
        open_index_readonly(snap, INDEX)


def test_open_index_checkpointed_db_in_a_writable_snapshot_leaves_no_files(
    snap_dir: Path,
) -> None:
    snap_root = snap_dir / IDS[1]
    _make_wal_db((snap_root / INDEX).parent, leave_wal=False)
    snap = resolve_snapshot(snap_dir, IDS[1])
    conn = open_index_readonly(snap, INDEX)
    try:
        assert conn.execute("SELECT count(*) FROM documents").fetchone() == (3,)
    finally:
        conn.close()
    assert sorted(p.name for p in (snap_root / INDEX).parent.iterdir()) == ["index.sqlite"]


@pytest.mark.parametrize(
    "relative",
    ["../2026-10-16T03-00-00Z/index.sqlite", "/etc/passwd", "", ".", "users/../../x.sqlite"],
)
def test_open_index_rejects_paths_outside_the_snapshot(snap_dir: Path, relative: str) -> None:
    snap = resolve_snapshot(snap_dir, IDS[1])
    with pytest.raises(SnapshotError):
        open_index_readonly(snap, relative)


def test_open_index_rejects_symlink_escaping_the_snapshot(snap_dir: Path, tmp_path: Path) -> None:
    _make_wal_db(tmp_path / "elsewhere", leave_wal=False)
    snap_root = snap_dir / IDS[1]
    (snap_root / "index.sqlite").symlink_to(tmp_path / "elsewhere" / "index.sqlite")
    snap = resolve_snapshot(snap_dir, IDS[1])
    with pytest.raises(SnapshotError, match="escapes"):
        open_index_readonly(snap, "index.sqlite")


def test_open_index_missing_file(snap_dir: Path) -> None:
    snap = resolve_snapshot(snap_dir, IDS[1])
    with pytest.raises(SnapshotError, match="not found"):
        open_index_readonly(snap, INDEX)


def test_snapshot_is_frozen() -> None:
    snap = Snapshot(
        id=IDS[0], created=datetime(2026, 10, 16, 3, tzinfo=UTC), path=Path("/x"), readonly=True
    )
    with pytest.raises(AttributeError):
        snap.readonly = False  # type: ignore[misc]
