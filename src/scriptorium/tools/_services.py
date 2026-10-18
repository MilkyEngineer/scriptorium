"""What the library tools need from the server process: stores, queue, indexes (§4.1, §6).

The server owns migrations (§6.1): :meth:`Services.open` migrates ``scriptorium.sqlite`` and
every account index it can find before the first request, and :meth:`Services.index` opens
(and migrates) an account's index on first use. Blocking calls belong in a thread; the tools
call these methods through ``asyncio.to_thread``.

The user is always the request's (``ToolContext.user``); nothing here takes a user id from a
tool argument, and paths come from ``users/store.py`` only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from scriptorium.auth.oidc import User
from scriptorium.cloud.client import CloudClient
from scriptorium.config import Settings
from scriptorium.db.connect import utc_now
from scriptorium.jobs.queue import JobQueue
from scriptorium.library.index import Index
from scriptorium.logging import log_event, uid_prefix
from scriptorium.users.store import GlobalStore, account_paths, global_db_path

__all__ = ["IMAGE_SLOTS", "Services", "identity_of"]

logger = logging.getLogger(__name__)

IMAGE_SLOTS: Final = 2
"""Page images the server downscales at once (per process)."""


def identity_of(device_token: str) -> str | None:
    """SHA-256 of the account identity in a device token (§5.1: the registry keeps the hash)."""
    client = CloudClient(device_token, cache_dir=Path(tempfile.gettempdir()) / "scriptorium-link")
    try:
        identity = client.account_identity()
    finally:
        client.close()
    return None if identity is None else hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass
class Services:
    """The server's handles on ``/data``; one per process, shared by every request."""

    settings: Settings
    store: GlobalStore
    queue: JobQueue
    register_device: Callable[[str, str], str] = CloudClient.register_device
    """Exchanges a one-time code for a device token (the one cloud write in M1)."""
    identify: Callable[[str], str | None] = identity_of
    clock: Callable[[], datetime] = utc_now
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    poll_interval_s: float = 0.25
    """How often a tool waiting for a render looks for its PNG."""
    image_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(IMAGE_SLOTS))
    """Bounds the server's own PNG decoding (downscales), which can take ~200 MB a page."""
    _indexes: dict[tuple[str, str], Index] = field(default_factory=dict, repr=False)
    _users: dict[str, str] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def open(cls, settings: Settings, **kwargs: Any) -> Services:
        """Open (and migrate) the global database and every account index under ``/data``."""
        path = global_db_path(settings.data_dir)
        clock = kwargs.get("clock", utc_now)
        store = GlobalStore.open(path, migrate=True, clock=clock)
        queue = JobQueue.open(path, migrate=True, clock=clock)
        services = cls(settings, store, queue, **kwargs)
        migrated = 0
        for row in store.all_accounts():
            paths = account_paths(settings.data_dir, row.uid, row.acct)
            if paths.index.is_file():
                Index.open(paths.index, migrate=True).close()
                migrated += 1
        log_event(logger, "server_migrated", count=migrated)
        return services

    def close(self) -> None:
        with self._lock:
            for index in self._indexes.values():
                index.close()
            self._indexes.clear()
        self.queue.close()

    def uid(self, user: User) -> str:
        """The user's storage id; creates the users row on first sight (§5.1)."""
        with self._lock:
            known = self._users.get(user.uid_hash)
        if known is not None:
            return known
        row = self.store.ensure_user(user)
        with self._lock:
            self._users[user.uid_hash] = row.uid
        log_event(logger, "user_seen", uid=uid_prefix(user.uid_hash))
        return row.uid

    def index(self, uid: str, acct: str) -> Index:
        """The account's index, migrated and opened once per process."""
        key = (uid, acct)
        with self._lock:
            index = self._indexes.get(key)
            if index is None:
                paths = account_paths(self.settings.data_dir, uid, acct)
                paths.root.mkdir(parents=True, exist_ok=True)
                paths.inside(paths.index)
                index = Index.open(paths.index, migrate=True, clock=self.clock)
                self._indexes[key] = index
            return index
