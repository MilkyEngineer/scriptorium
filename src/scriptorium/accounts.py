"""Names the server and the worker share about one linked account (§7, §11.1).

The device token lives in the user's ``secrets.enc`` under :func:`device_token_name`. The
worker records its view of the account in the account index's ``sync_state`` under the
``STATE_*`` keys below (beside the sync's own keys in :mod:`scriptorium.cloud.sync`);
``account_status`` reads them and ``connect_remarkable`` clears the relink state.

:func:`poll_lock` is the one lock the worker and the server share about an account: the
worker holds it for a whole poll (and for its start-up sweep), and
``disconnect_remarkable(keep_archive=false)`` holds it while it moves the account directory
to ``removed/``, so a poll never runs against a directory that is being moved.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final, Literal

from scriptorium.users.store import ACCT_RE, AccountPaths

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
    "poll_lock",
]

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
