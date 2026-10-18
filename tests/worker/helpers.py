"""A linked account on a temporary ``/data``, synced from the fake cloud (worker and tool tests).

Everything is real except the cloud (``FakeCloud`` behind ``httpx.MockTransport``) and the
render pool (threads instead of spawned processes, for speed). The user is the test issuer's
``ALLOWED_SUB``, so an MCP session with ``issuer.token()`` sees this account.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from scriptorium.accounts import device_token_name
from scriptorium.auth.oidc import User, uid_hash_for
from scriptorium.cloud.client import CloudClient
from scriptorium.config import Settings
from scriptorium.jobs.queue import JobQueue
from scriptorium.library.index import Index
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import (
    AccountPaths,
    AccountRow,
    GlobalStore,
    account_paths,
    global_db_path,
)
from scriptorium.worker import AccountContext, AccountLoop, Timing, Worker
from tests.cloud.fake_cloud import DEVICE_TOKEN, NOTEBOOK, FakeCloud, fixture_documents, renamed
from tests.conftest import ADMIN_SUB, ALLOWED_SUB, ISSUER, make_settings

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
FAST = Timing(
    supervise_s=0.05,
    requeue_s=0.2,
    heartbeat_s=0.05,
    job_idle_s=0.01,
    job_heartbeat_s=0.05,
    recheck_s=0.05,
    schema_wait_s=0.02,
    continue_s=5.0,
    watchdog_grace_s=0.2,
)


class ZeroJitter(random.Random):
    """``uniform`` always returns the middle of its range: delays come out exact."""

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2


@dataclass
class TickingClock:
    """A UTC clock that moves one second per reading."""

    now: datetime = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def make_user(sub: str = ALLOWED_SUB, *, admin: bool = False) -> User:
    return User(
        iss=ISSUER,
        sub=sub,
        uid_hash=uid_hash_for(ISSUER, sub),
        display_name="reader",
        email=None,
        email_verified=False,
        is_admin=admin,
        scopes=frozenset({"scriptorium"}),
    )


ADMIN = make_user(ADMIN_SUB, admin=True)
COPY_A = "00000000-0000-4000-8000-0000000000c1"
COPY_B = "00000000-0000-4000-8000-0000000000c2"


def reupload(uuid: str, name: str) -> dict[str, bytes]:
    """The fixture notebook's files uploaded again: a new document UUID and name, the same
    page UUIDs and page files (open question 8, seen on a live library; push and restore do
    this). A copy made on the tablet mints fresh page UUIDs instead."""
    files = renamed(fixture_documents()[NOTEBOOK], NOTEBOOK, uuid)
    metadata = json.loads(files[f"{uuid}.metadata"])
    metadata["visibleName"] = name
    files[f"{uuid}.metadata"] = json.dumps(metadata).encode()
    return files


@dataclass
class Linked:
    """One user with one linked account, a worker, and the fake cloud behind it."""

    settings: Settings
    store: GlobalStore
    queue: JobQueue
    cloud: FakeCloud
    user: User
    row: AccountRow
    paths: AccountPaths
    worker: Worker
    clock: TickingClock
    loops: list[AccountLoop] = field(default_factory=list)

    @property
    def uid(self) -> str:
        return self.row.uid

    @property
    def acct(self) -> str:
        return self.row.acct

    def context(self) -> AccountContext:
        return self.worker.context(self.uid, self.acct)

    def loop(self) -> AccountLoop:
        loop = AccountLoop(self.worker, self.row)
        loop.start(self.context())
        self.loops.append(loop)
        return loop

    def index(self) -> Index:
        return self.context().index

    async def sync(self) -> AccountLoop:
        """Start a loop, poll once and run every queued job."""
        loop = self.loop()
        await loop.poll_once(self.context())
        await self.worker.drain_jobs()
        return loop

    def close(self) -> None:
        self.worker.close()


def link(
    tmp_path: Path,
    *,
    cloud: FakeCloud | None = None,
    settings: dict[str, Any] | None = None,
    timing: Timing = FAST,
    client_factory: Callable[[str], CloudClient] | None = None,
) -> Linked:
    """A temporary ``/data`` with the test user linked to ``cloud`` (the fixture library)."""
    if cloud is None:
        cloud = FakeCloud()
        cloud.put_documents(fixture_documents())
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    values: dict[str, Any] = {"data_dir": data, "render_workers": 2, "sync_concurrency": 2}
    values.update(settings or {})
    config = make_settings(**values)
    clock = TickingClock()
    store = GlobalStore.open(global_db_path(data), migrate=True, clock=clock)
    queue = JobQueue.open(global_db_path(data), migrate=True, clock=clock)
    user = make_user()
    store.ensure_user(user)
    uid = store.ensure_user(user).uid
    row = store.link_account(uid, "a" * 64, "desktop-linux")
    paths = account_paths(data, uid, row.acct)
    paths.root.mkdir(parents=True)
    Index.open(paths.index, migrate=True).close()
    SecretBox(config, uid).seal(device_token_name(row.acct), DEVICE_TOKEN.encode())
    transport = httpx.MockTransport(cloud.handler)

    def make_client(token: str) -> CloudClient:
        return CloudClient(token, cache_dir=tmp_path / "cache", transport=transport)

    worker = Worker(
        config,
        store,
        queue,
        client_factory=client_factory or make_client,
        render_executor=ThreadPoolExecutor(max_workers=2),
        clock=clock,
        timing=timing,
        rng=ZeroJitter(),
    )
    return Linked(config, store, queue, cloud, user, row, paths, worker, clock)
