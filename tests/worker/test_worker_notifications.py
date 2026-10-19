"""The worker starts, restarts and stops each account's notification listener (§7, S7)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from scriptorium.accounts import device_token_name
from scriptorium.cloud.notifications import Socket
from scriptorium.users.secrets import SecretBox
from scriptorium.worker import AccountLoop
from tests.cloud.fake_cloud import TOKEN_KEY
from tests.worker.helpers import Linked, link


@dataclass
class QuietCloud:
    """A notifications endpoint that accepts every connection and never sends a frame."""

    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    open: int = 0
    most_open: int = 0

    @asynccontextmanager
    async def __call__(self, url: str, headers: Mapping[str, str]) -> AsyncIterator[Socket]:
        self.calls.append((url, dict(headers)))
        self.open += 1
        self.most_open = max(self.most_open, self.open)
        try:
            yield self
        finally:
            self.open -= 1

    async def recv(self) -> str | bytes:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def notifying(tmp_path: Path, *, on: bool = True) -> tuple[Linked, QuietCloud]:
    linked = link(tmp_path, settings={"notifications": on})
    socket = QuietCloud()
    linked.worker.notify_connector = socket
    return linked, socket


async def until(condition: Callable[[], bool], limit: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while not condition():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.005)


def listener_task(linked: Linked) -> asyncio.Task[None] | None:
    entry = linked.worker._listeners.get(linked.acct)
    return None if entry is None else entry[1]


async def stop_all(linked: Linked) -> None:
    linked.worker.reconcile_loops([])
    tasks = [t for _l, t, _s in linked.worker._loops.values()]
    tasks += [t for _l, t in linked.worker._listeners.values()]
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)


def messages(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [getattr(r, "scriptorium_fields", {}) for r in caplog.records if r.getMessage() == name]


async def test_a_listener_starts_and_stops_with_its_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pokes: list[str] = []
    real_poke = AccountLoop.poke

    def poke(self: AccountLoop) -> None:
        pokes.append(self.acct)
        real_poke(self)

    monkeypatch.setattr(AccountLoop, "poke", poke)
    linked, socket = notifying(tmp_path)
    try:
        linked.worker.reconcile_loops([linked.row])
        task = listener_task(linked)
        assert task is not None and task.get_name() == f"notify-{linked.acct}"
        await until(lambda: socket.open == 1)
        url, headers = socket.calls[0]
        # The fake cloud's user token is not a JWT: the default region, no proactive renewal.
        assert url == "wss://eu.tectonic.remarkable.com/notifications/ws/json/1"
        assert headers == {"authorization": f"Bearer {linked.cloud.user_token}"}
        assert TOKEN_KEY in linked.cloud.targets()
        await until(lambda: bool(pokes))  # the connect poked the account's loop
        assert pokes == [linked.acct]

        linked.worker.reconcile_loops([linked.row])  # a later pass: still the one listener
        assert listener_task(linked) is task
        assert len(socket.calls) == 1

        linked.worker.reconcile_loops([])  # the account left the list
        await asyncio.wait_for(task, 5)
        assert socket.open == 0
        linked.worker.reconcile_loops([])
        assert linked.worker._listeners == {}
        assert socket.most_open == 1
    finally:
        await stop_all(linked)
        linked.close()


async def test_no_listener_without_the_setting(tmp_path: Path) -> None:
    linked, socket = notifying(tmp_path, on=False)
    try:
        linked.worker.reconcile_loops([linked.row])
        await asyncio.sleep(0.05)
        assert linked.worker._listeners == {}
        assert socket.calls == []
    finally:
        await stop_all(linked)
        linked.close()


async def test_a_readded_account_waits_for_its_old_listener(tmp_path: Path) -> None:
    linked, socket = notifying(tmp_path)
    try:
        linked.worker.reconcile_loops([linked.row])
        await until(lambda: socket.open == 1)
        old = listener_task(linked)
        linked.worker.reconcile_loops([])
        linked.worker.reconcile_loops([linked.row])  # back before the old one has ended
        assert listener_task(linked) is old
        assert old is not None
        await asyncio.wait_for(old, 5)
        linked.worker.reconcile_loops([linked.row])
        assert listener_task(linked) is not old
        await until(lambda: len(socket.calls) == 2 and socket.open == 1)
        assert socket.most_open == 1
    finally:
        await stop_all(linked)
        linked.close()


async def test_a_rejected_token_stops_the_listener_until_the_loop_recovers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    linked, socket = notifying(tmp_path)
    try:
        linked.cloud.status[TOKEN_KEY] = 401
        linked.worker.reconcile_loops([linked.row])
        task = listener_task(linked)
        assert task is not None
        await asyncio.wait_for(task, 5)
        assert messages(caplog, "notifications_stopped") == [
            {"acct": linked.acct, "reason": "token_rejected"}
        ]
        assert socket.calls == []
        loop = linked.worker._loops[linked.acct][0]
        await until(lambda: loop._ended_at is not None)
        assert loop._pokeable is False  # its poll was rejected too
        linked.worker.reconcile_loops([linked.row])
        assert listener_task(linked) is task  # not restarted yet

        del linked.cloud.status[TOKEN_KEY]
        loop._pokeable = True  # as after an ok poll
        linked.worker.reconcile_loops([linked.row])
        assert listener_task(linked) is not task
        await until(lambda: socket.open == 1)
    finally:
        await stop_all(linked)
        linked.close()


async def test_no_device_token_is_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="scriptorium")
    linked, socket = notifying(tmp_path)
    try:
        SecretBox(linked.settings, linked.uid).forget(device_token_name(linked.acct))
        for _ in range(3):
            linked.worker.reconcile_loops([linked.row])
            task = listener_task(linked)
            assert task is not None
            await asyncio.wait_for(task, 5)
        skipped = messages(caplog, "notifications_skipped")
        assert skipped == [{"acct": linked.acct, "reason": "no_device_token"}]
        assert socket.calls == []
    finally:
        await stop_all(linked)
        linked.close()


async def test_shutdown_ends_the_listeners(tmp_path: Path) -> None:
    linked, socket = notifying(tmp_path)
    stop = asyncio.Event()
    run = asyncio.create_task(linked.worker.run(stop))
    try:
        await until(lambda: socket.open == 1)
        task = listener_task(linked)
        assert task is not None
    finally:
        stop.set()
        await asyncio.wait_for(run, 10)
    assert task.done()
    assert socket.open == 0
    assert linked.worker._listeners == {}
    linked.close()
