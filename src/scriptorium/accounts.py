"""Names the server and the worker share about one linked account (§7, §11.1).

The device token lives in the user's ``secrets.enc`` under :func:`device_token_name`. The
worker records its view of the account in the account index's ``sync_state`` under the
``STATE_*`` keys below (beside the sync's own keys in :mod:`scriptorium.cloud.sync`);
``account_status`` reads them and ``connect_remarkable`` clears the relink state.

:func:`poll_lock` is the one lock the worker and the server share about an account: the
worker holds it for a whole poll (and for its start-up sweep), and
``disconnect_remarkable(keep_archive=false)`` holds it while it moves the account directory
to ``removed/`` (:func:`removal_destination`, :func:`move_aside`), so a poll never runs
against a directory that is being moved.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from scriptorium.errors import RefusedError
from scriptorium.library.layout import resolve_inside
from scriptorium.logging import log_event
from scriptorium.users.store import (
    ACCT_RE,
    AccountPaths,
    account_paths,
    removed_account_path,
    removed_dir,
    user_dir,
)

if sys.platform != "win32":
    import fcntl

__all__ = [
    "DEVICE_DESCRIPTION",
    "RELINK_AFTER",
    "STATE_LAST_POLL_AT",
    "STATE_LAST_POLL_OUTCOME",
    "STATE_LAST_SYNC_AT",
    "STATE_NEEDS_RELINK",
    "STATE_TOKEN_REJECTIONS",
    "PollLock",
    "device_token_name",
    "move_aside",
    "poll_lock",
    "removal_destination",
]

logger = logging.getLogger(__name__)

PollLock = Literal["held", "busy", "gone"]

DEVICE_DESCRIPTION: Final = "desktop-linux"
"""What ``connect_remarkable`` registers the device as (the fork's default)."""

RELINK_AFTER: Final = 3
"""Consecutive polls whose device token is rejected before the account needs relinking."""

STATE_NEEDS_RELINK: Final = "needs_relink"
"""``"true"`` once the token was rejected :data:`RELINK_AFTER` times in a row: polling stops."""
STATE_TOKEN_REJECTIONS: Final = "token_rejections"  # noqa: S105 - a key name, not a secret
"""Consecutive polls that ended ``token_rejected`` (decimal)."""
STATE_LAST_SYNC_AT: Final = "last_sync_at"
"""ISO 8601 time of the last poll that reached the cloud and ended ``ok`` or ``partial``."""
STATE_LAST_POLL_AT: Final = "last_poll_at"
STATE_LAST_POLL_OUTCOME: Final = "last_poll_outcome"
"""The last poll's ``SyncReport.outcome``, or ``watchdog``/``error`` when it did not end."""


def device_token_name(acct: str) -> str:
    """The ``SecretBox`` name of an account's device token."""
    if not ACCT_RE.fullmatch(acct):
        raise ValueError("not an account id")
    return f"device_token:{acct}"


@contextmanager
def poll_lock(paths: AccountPaths, *, blocking: bool = True) -> Iterator[PollLock]:
    """Hold the account's poll lock (``flock`` on :attr:`AccountPaths.poll_lock`) for the block.

    Yields ``"held"`` while it is held; ``"busy"`` when ``blocking`` is false and another
    holder has it; ``"gone"`` when the account directory is not there, or was moved while
    this call waited (the lock file it locked is no longer the one at the account's path).
    In the last two cases nothing is held and the caller must not touch the account's files.
    The file is opened without following a symlink. On Windows, where there is no ``flock``,
    nothing is locked (development only).
    """
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(paths.poll_lock, flags, 0o600)
    except (FileNotFoundError, NotADirectoryError):
        yield "gone"
        return
    try:
        if sys.platform != "win32":
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield "busy"
                return
        try:
            same = os.path.samestat(os.fstat(fd), os.stat(paths.poll_lock, follow_symlinks=False))
        except (FileNotFoundError, NotADirectoryError):
            same = False
        yield "held" if same else "gone"
    finally:
        os.close(fd)


# ---- moving an account aside (disconnect_remarkable(keep_archive=false)) --------------------


def removal_destination(data_dir: Path, uid: str, acct: str, *, at: datetime) -> Path:
    """Where :func:`move_aside` puts the account: ``users/<uid>/removed/<acct>-<ts>``.

    Creates ``removed/`` if needed and refuses (:class:`RefusedError`) unless the move can be
    one rename: the account directory is a real directory (not a symlink) inside the user's
    directory; ``removed/`` is a real directory too, at exactly ``users/<uid>/removed`` (a
    symlink is refused even when it points inside the user's directory); the destination
    does not exist yet; and both are on the same filesystem (a rename across filesystems
    would copy, and is refused by the kernel anyway). Nothing is moved or deleted here.
    """
    paths = account_paths(data_dir, uid, acct)
    user = user_dir(data_dir, uid)
    try:
        info = os.lstat(paths.root)
    except FileNotFoundError:
        raise RefusedError("the account directory is not there") from None
    if not stat.S_ISDIR(info.st_mode):
        raise RefusedError("the account directory is not a directory")
    resolve_inside(user, paths.root)
    base = removed_dir(data_dir, uid)
    try:
        base.mkdir(mode=0o700, exist_ok=True)
    except OSError:  # e.g. a dangling symlink or a file in its place
        raise RefusedError("removed/ cannot be created") from None
    base_info = _plain_directory(base)
    if base_info is None:
        raise RefusedError("removed/ is not a plain directory")
    if os.path.realpath(base) != os.path.join(os.path.realpath(user), "removed"):
        raise RefusedError("removed/ is not where it should be")
    destination = removed_account_path(data_dir, uid, acct, at)
    if os.path.lexists(destination):
        raise RefusedError("the destination already exists")
    if base_info.st_dev != info.st_dev:
        raise RefusedError("removed/ is on another filesystem")
    return destination


def _plain_directory(path: Path) -> os.stat_result | None:
    """``lstat`` of ``path`` if it is a directory itself (not a symlink to one), else None."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info if stat.S_ISDIR(info.st_mode) else None


def move_aside(paths: AccountPaths, destination: Path) -> None:
    """Rename the account directory to ``destination`` (from :func:`removal_destination`),
    then fsync both parent directories. The only filesystem change Scriptorium makes to an
    account outside its archive paths: one rename on one filesystem, nothing deleted. The
    caller holds :func:`poll_lock`.

    Raises :class:`OSError` only when the rename did not happen, so nothing moved. Once the
    rename has returned the move is done: a failed fsync is logged
    (``account_move_fsync_failed``, the exception's class name only) and not raised, because
    the directory is no longer where a caller could put the account back from.
    """
    if os.path.lexists(destination):
        raise FileExistsError("the destination already exists")
    if _plain_directory(destination.parent) is None:  # checked again: replaced meanwhile?
        raise NotADirectoryError("removed/ is not a plain directory")
    os.rename(paths.root, destination)
    for parent in (paths.root.parent, destination.parent):
        try:
            _fsync_dir(parent)
        except OSError as exc:
            log_event(
                logger,
                "account_move_fsync_failed",
                level=logging.ERROR,
                acct=paths.root.name,
                reason=type(exc).__name__,
            )


def _fsync_dir(path: Path) -> None:
    if sys.platform == "win32":  # directories cannot be opened for fsync there
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
