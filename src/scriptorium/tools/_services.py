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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from pydantic import SecretStr

from scriptorium.auth.oidc import User
from scriptorium.cloud.client import CloudClient
from scriptorium.config import Settings
from scriptorium.db.connect import utc_now
from scriptorium.errors import NotFoundError
from scriptorium.jobs.queue import JobQueue
from scriptorium.library.index import Index
from scriptorium.logging import log_event, uid_prefix
from scriptorium.ocr._ports import OcrPorts, build_ocr_ports
from scriptorium.ocr.client import OcrClient, client_for
from scriptorium.ocr.ledger import SpendLedger
from scriptorium.ocr.modes import listed_email_digest
from scriptorium.users.store import GlobalStore, account_paths, global_db_path

__all__ = ["IMAGE_SLOTS", "KEY_CHECKS_PER_WINDOW", "KEY_CHECK_WINDOW_S", "Services", "identity_of"]

logger = logging.getLogger(__name__)

IMAGE_SLOTS: Final = 2
"""Page images the server downscales at once (per process)."""
KEY_CHECKS_PER_WINDOW: Final = 5
KEY_CHECK_WINDOW_S: Final = 15 * 60
"""``set_claude_api_key`` validates at most this many keys per user per window: each check is
a call to the Claude API, and the tool must not become a key-guessing oracle."""
_REMOVED: Final = "That account was removed from this server."


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
    ocr: OcrPorts | None = None
    """The OCR seams; None (the default) binds F's modules over :attr:`ledger` and
    :attr:`ocr_client` (``build_ocr_ports``). Tests pass their own."""
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    poll_interval_s: float = 0.25
    """How often a tool waiting for a render looks for its PNG."""
    image_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(IMAGE_SLOTS))
    """Bounds the server's own PNG decoding (downscales), which can take ~200 MB a page."""
    ocr_client: Callable[[Settings, SecretStr], OcrClient] = client_for
    """Builds the Claude API client for a key (``set_claude_api_key`` validates with it, and
    the OCR ports use it)."""
    monotonic: Callable[[], float] = time.monotonic
    _indexes: dict[tuple[str, str], Index] = field(default_factory=dict, repr=False)
    _users: dict[str, tuple[str, str | None]] = field(default_factory=dict, repr=False)
    _ledger: SpendLedger | None = field(default=None, repr=False)
    _key_checks: dict[str, list[float]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _connect_locks: dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.ocr is None:
            self.ocr = build_ocr_ports(self.ledger, client=self.ocr_client)

    @property
    def ports(self) -> OcrPorts:
        """The OCR seams of this process (one ledger, one client factory)."""
        assert self.ocr is not None  # set in __post_init__
        return self.ocr

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
        """The user's storage id; creates the users row on first sight (§5.1).

        Also keeps ``users.server_key_email_sha256`` in step with the token: the digest of the
        user's verified email while SERVER_KEY_USERS lists it, else nothing (§8.1).
        """
        digest = listed_email_digest(self.settings, user)
        with self._lock:
            known = self._users.get(user.uid_hash)
        if known is not None and known[1] == digest:
            return known[0]
        if known is None:
            row = self.store.ensure_user(user)
            uid, stored = row.uid, row.server_key_email_sha256
            log_event(logger, "user_seen", uid=uid_prefix(user.uid_hash))
        else:
            uid, stored = known[0], known[1]
        if stored != digest:
            self.store.set_server_key_email(uid, digest)
        with self._lock:
            self._users[user.uid_hash] = (uid, digest)
        return uid

    @property
    def ledger(self) -> SpendLedger:
        """OCR spend in ``scriptorium.sqlite`` (migrated by :meth:`open`)."""
        with self._lock:
            if self._ledger is None:
                self._ledger = SpendLedger(global_db_path(self.settings.data_dir))
            return self._ledger

    def allow_key_check(self, uid: str) -> bool:
        """Take one of the user's key validations for this window; False when none are left."""
        now = self.monotonic()
        with self._lock:
            recent = [t for t in self._key_checks.get(uid, ()) if now - t < KEY_CHECK_WINDOW_S]
            allowed = len(recent) < KEY_CHECKS_PER_WINDOW
            if allowed:
                recent.append(now)
            self._key_checks[uid] = recent
            return allowed

    def connect_lock(self, uid: str) -> asyncio.Lock:
        """The lock that serialises ``connect_remarkable`` for one user (event loop only).

        Each call registers a device at reMarkable before the link is recorded, so two
        concurrent calls would both register and the loser would leave an orphan device.
        """
        lock = self._connect_locks.get(uid)
        if lock is None:
            lock = self._connect_locks[uid] = asyncio.Lock()
        return lock

    def index(self, uid: str, acct: str) -> Index:
        """The account's index, migrated and opened once per process.

        ``not_found`` for an account that was removed (its directory moved to ``removed/``):
        opening it would recreate an empty account directory in its place. ``removed_at`` is
        read on every call, cached index or not, because another process may have removed
        the account since this one opened it; such an index is closed and dropped.
        """
        key = (uid, acct)
        if self._removed(uid, acct):
            self.evict_index(uid, acct)
            raise NotFoundError(_REMOVED)
        with self._lock:
            index = self._indexes.get(key)
            if index is None:
                # Again under the lock: a removal in this process marks the account and then
                # evicts under this lock, so no index is opened (and no directory created)
                # once the account is marked.
                if self._removed(uid, acct):
                    raise NotFoundError(_REMOVED)
                paths = account_paths(self.settings.data_dir, uid, acct)
                paths.root.mkdir(parents=True, exist_ok=True)
                paths.inside(paths.index)
                index = Index.open(paths.index, migrate=True, clock=self.clock)
                self._indexes[key] = index
            return index

    def _removed(self, uid: str, acct: str) -> bool:
        """Whether the account's directory was moved to ``removed/`` (one SELECT)."""
        row = self.store.account(uid, acct)
        return row is not None and row.removed_at is not None

    def evict_index(self, uid: str, acct: str) -> None:
        """Close and forget the account's cached index (before its directory is moved); the
        next :meth:`index` call opens it again, or refuses a removed account."""
        with self._lock:
            index = self._indexes.pop((uid, acct), None)
        if index is not None:
            index.close()
