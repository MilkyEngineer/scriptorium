"""``accounts.poll_lock``: the lock a poll and ``disconnect_remarkable`` share."""

from __future__ import annotations

import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scriptorium.accounts import move_aside, poll_lock, removal_destination
from scriptorium.errors import PathEscapeError, RefusedError
from scriptorium.users.store import AccountPaths, account_paths, removed_account_path

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX only")

UID = "0123456789abcdef"
ACCT = "01J0000000000000000000000A"
AT = datetime(2026, 10, 18, 9, 30, tzinfo=UTC)


@pytest.fixture
def paths(tmp_path: Path) -> AccountPaths:
    paths = account_paths(tmp_path, UID, ACCT)
    paths.root.mkdir(parents=True)
    return paths


def test_held_then_busy_for_a_second_holder(paths: AccountPaths) -> None:
    with poll_lock(paths) as first:
        assert first == "held"
        with poll_lock(paths, blocking=False) as second:
            assert second == "busy"
    with poll_lock(paths, blocking=False) as again:
        assert again == "held"
    assert paths.poll_lock.is_file()


def test_gone_when_the_directory_is_not_there(tmp_path: Path) -> None:
    paths = account_paths(tmp_path, UID, ACCT)
    with poll_lock(paths) as state:
        assert state == "gone"
    assert not paths.root.exists()  # nothing was created


def test_gone_when_the_directory_moved_while_waiting(paths: AccountPaths) -> None:
    moved = paths.root.with_name("moved")
    seen: list[str] = []
    waiting = threading.Event()

    def poll() -> None:
        waiting.set()
        with poll_lock(paths) as state:
            seen.append(state)

    with poll_lock(paths) as held:
        assert held == "held"
        thread = threading.Thread(target=poll)
        thread.start()
        waiting.wait(5)
        os.rename(paths.root, moved)
    thread.join(5)
    assert seen == ["gone"]
    assert not paths.root.exists()


def test_a_symlinked_lock_file_is_not_followed(paths: AccountPaths, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("")
    paths.poll_lock.symlink_to(target)
    with pytest.raises(OSError), poll_lock(paths):
        pass


# ---- moving an account aside ----------------------------------------------------------------


def test_move_aside_renames_the_whole_directory(paths: AccountPaths, tmp_path: Path) -> None:
    (paths.root / "blobs").mkdir()
    (paths.root / "blobs" / ("a" * 64)).write_bytes(b"x")
    destination = removal_destination(tmp_path, UID, ACCT, at=AT)
    assert destination == removed_account_path(tmp_path, UID, ACCT, AT)
    assert destination.parent.is_dir() and not destination.exists()

    move_aside(paths, destination)

    assert not paths.root.exists()
    assert (destination / "blobs" / ("a" * 64)).read_bytes() == b"x"


def test_removal_destination_refuses_what_a_rename_cannot_do(
    paths: AccountPaths, tmp_path: Path
) -> None:
    destination = removal_destination(tmp_path, UID, ACCT, at=AT)
    destination.mkdir()
    with pytest.raises(RefusedError):  # never onto something that is there
        removal_destination(tmp_path, UID, ACCT, at=AT)
    with pytest.raises(FileExistsError):
        move_aside(paths, destination)
    assert paths.root.is_dir()
    other = "01J0000000000000000000000B"
    with pytest.raises(RefusedError):  # no directory to move
        removal_destination(tmp_path, UID, other, at=AT)


def test_removal_destination_refuses_a_symlinked_removed_directory(
    paths: AccountPaths, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    removed_dir_path = paths.root.parent.parent / "removed"
    removed_dir_path.symlink_to(elsewhere)
    with pytest.raises(PathEscapeError):
        removal_destination(tmp_path, UID, ACCT, at=AT)
    assert paths.root.is_dir() and not any(elsewhere.iterdir())
