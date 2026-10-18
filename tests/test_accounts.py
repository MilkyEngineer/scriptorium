"""``accounts.poll_lock``: the lock a poll and ``disconnect_remarkable`` share."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from scriptorium.accounts import poll_lock
from scriptorium.users.store import AccountPaths, account_paths

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX only")

UID = "0123456789abcdef"
ACCT = "01J0000000000000000000000A"


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
