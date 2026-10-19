"""The notifications listener (Architecture §7, S7) against a scripted, in-process socket."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import jwt
import pytest

from scriptorium.cloud import notifications as nt
from scriptorium.cloud.errors import TokenRejectedError, TransientCloudError
from scriptorium.cloud.notifications import (
    NotificationListener,
    Socket,
    TokenClaims,
    _NoRedirects,
    event_label,
    notifications_url,
    token_claims,
)
from scriptorium.logging import JsonFormatter

ACCT = "01J00000000000000000000000"
WALL = 1_000_000.0
KEY = "k" * 32
MESSAGE_ID = "12345678901234567"
USER_ID = "auth0|aaaaaaaaaaaaaaaaaaaaaaaa"
DEVICE_ID = "bbbbbbbbbbbbbbb"
TRACE = "00-cccccccccccccccccccccccccccccccc-dddddddddddddddd-01"


def user_token(**claims: Any) -> str:
    return jwt.encode(claims, KEY, algorithm="HS256")


def frame(event: str = "SyncComplete") -> str:
    """A frame shaped like the ones S7 recorded, with made-up identifiers."""
    return json.dumps(
        {
            "message": {
                "attributes": {
                    "auth0UserID": USER_ID,
                    "event": event,
                    "eventType": "SyncEventNotification",
                    "eventVersion": "1",
                    "googclient_traceparent": TRACE,
                    "orgID": "",
                    "sourceDeviceID": DEVICE_ID,
                    "userID": USER_ID,
                },
                "data": None,
                "messageid": MESSAGE_ID,
            }
        }
    )


# ---- fakes -----------------------------------------------------------------------------------


@dataclass
class Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now


class ClosedError(Exception):
    """Stands in for ``websockets.ConnectionClosed``."""


@dataclass
class Idle:
    """The socket stays quiet for ``seconds``: the clock moves on and ``recv`` times out, as
    the listener's own deadline would make it."""

    seconds: float


@dataclass
class Close:
    """The server drops the socket ``after`` seconds."""

    after: float = 0.0


Item = str | bytes | Idle | Close
Script = list[Item] | BaseException


class FakeSocket:
    def __init__(self, items: list[Item], clock: Clock) -> None:
        self.items = list(items)
        self.clock = clock

    async def recv(self) -> str | bytes:
        await asyncio.sleep(0)
        if not self.items:
            await asyncio.Event().wait()  # quiet until cancelled
        item = self.items.pop(0)
        if isinstance(item, Idle):
            self.clock.now += item.seconds
            raise TimeoutError
        if isinstance(item, Close):
            self.clock.now += item.after
            raise ClosedError
        return item


@dataclass
class Connector:
    """Hands out one scripted connection per call; when the scripts run out it sets
    ``stop`` and waits. Fails the test if two sockets are ever open at once."""

    scripts: list[Script]
    clock: Clock
    stop: asyncio.Event
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    open: int = 0

    @asynccontextmanager
    async def __call__(self, url: str, headers: Mapping[str, str]) -> AsyncIterator[Socket]:
        self.calls.append((url, dict(headers)))
        if not self.scripts:
            self.stop.set()
            await asyncio.Event().wait()
        script = self.scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        assert self.open == 0, "two sockets open for one device token"
        self.open += 1
        try:
            yield FakeSocket(script, self.clock)
        finally:
            self.open -= 1


class Rng:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.value


@dataclass
class Harness:
    clock: Clock
    stop: asyncio.Event
    connector: Connector
    listener: NotificationListener
    sleeps: list[float]
    pokes: list[float]
    tokens: list[str]

    async def run(self, until: Callable[[], bool] | None = None, limit: float = 5.0) -> None:
        """Run the listener until ``until`` holds (then stop it) or it ends by itself."""
        task = asyncio.create_task(self.listener.run(self.stop))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit
        while not task.done() and loop.time() < deadline:
            if until is not None and until():
                break
            await asyncio.sleep(0.001)
        self.stop.set()
        await asyncio.wait_for(task, limit)
        assert self.connector.open == 0  # stopping closed the socket


def harness(
    scripts: list[Script],
    *,
    token: Callable[[], str] | None = None,
    sleep: Callable[[float], Any] | None = None,
    rng: Rng | None = None,
) -> Harness:
    clock = Clock()
    stop = asyncio.Event()
    connector = Connector(list(scripts), clock, stop)
    sleeps: list[float] = []
    pokes: list[float] = []
    tokens: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(0)

    def issue() -> str:
        value = token() if token is not None else user_token(tectonic="us", exp=WALL + 10_800)
        tokens.append(value)
        return value

    listener = NotificationListener(
        ACCT,
        issue,
        lambda: pokes.append(clock.now),
        connector=connector,
        clock=clock,
        wall=lambda: WALL,
        sleep=sleep or fake_sleep,
        rng=rng or Rng(0.0),
    )
    return Harness(clock, stop, connector, listener, sleeps, pokes, tokens)


def events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [getattr(r, "scriptorium_fields", {}) for r in caplog.records if r.getMessage() == name]


def backoffs(caplog: pytest.LogCaptureFixture) -> list[float]:
    """The backoff waits, from the listener's own ``notifications_retry`` lines."""
    return [f["duration_ms"] / 1000 for f in events(caplog, "notifications_retry")]


def order(caplog: pytest.LogCaptureFixture, names: set[str]) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage() in names]


@pytest.fixture(autouse=True)
def _debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="scriptorium")


# ---- pure helpers ----------------------------------------------------------------------------


def test_token_claims_read_region_and_expiry() -> None:
    assert token_claims(user_token(tectonic="us", exp=1234)) == TokenClaims("us", 1234.0)
    assert token_claims(user_token(tectonic="eu", exp=12.5)) == TokenClaims("eu", 12.5)


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"tectonic": ""},
        {"tectonic": 7},
        {"tectonic": "evil.example/x"},
        {"tectonic": "US"},
        {"tectonic": "a" * 17},
    ],
)
def test_token_claims_default_region(claims: dict[str, Any]) -> None:
    assert token_claims(user_token(**claims)).region == nt.DEFAULT_REGION


@pytest.mark.parametrize("exp", [None, "1234", True, [1]])
def test_token_claims_without_a_usable_expiry(exp: Any) -> None:
    claims = {"tectonic": "us"} if exp is None else {"tectonic": "us", "exp": exp}
    token = jwt.encode(claims, KEY, algorithm="HS256")
    assert token_claims(token) == TokenClaims("us", None)


def test_token_claims_of_something_that_is_not_a_jwt() -> None:
    assert token_claims("opaque-token") == TokenClaims("eu", None)
    assert token_claims("") == TokenClaims("eu", None)


def test_notifications_url() -> None:
    assert notifications_url("us") == "wss://us.tectonic.remarkable.com/notifications/ws/json/1"
    assert notifications_url("x/../") == "wss://eu.tectonic.remarkable.com/notifications/ws/json/1"


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        (frame(), "SyncComplete"),
        (frame("DocumentDeleted"), "other"),
        ("not json", "other"),
        ("[1, 2]", "other"),
        ('{"message": "x"}', "other"),
        ('{"message": {"attributes": {"event": 5}}}', "other"),
        (b"\x00\x01", "other"),
        ("[" * 100_000, "other"),
    ],
)
def test_event_label(raw: str | bytes, label: str) -> None:
    assert event_label(raw) == label


async def test_the_real_connector_pins_its_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Recorder:
        def __init__(self, url: str, **kwargs: Any) -> None:
            seen.update(kwargs, url=url)

        async def __aenter__(self) -> str:
            return "socket"

        async def __aexit__(self, *_exc: object) -> None:
            seen["closed"] = True

    monkeypatch.setattr(nt, "_NoRedirects", Recorder)
    url = notifications_url("us")
    async with nt.websocket_connector(url, {"authorization": "Bearer t"}) as socket:
        assert socket == "socket"
    assert seen["url"] == url
    assert seen["additional_headers"] == {"authorization": "Bearer t"}
    assert (seen["ping_interval"], seen["max_size"], seen["proxy"]) == (20.0, 1024 * 1024, None)
    assert seen["closed"] is True


def test_redirects_are_failures() -> None:
    refused = ValueError("redirect")
    connect = _NoRedirects(notifications_url("us"))  # builds, does not connect
    assert connect.process_redirect(refused) is refused


# ---- the listener --------------------------------------------------------------------------


async def test_connects_with_a_renewed_token_and_pokes_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = harness([[]])
    await h.run(until=lambda: bool(h.pokes))
    assert len(h.pokes) == 1
    assert len(h.tokens) == 1
    url, headers = h.connector.calls[0]
    assert url == "wss://us.tectonic.remarkable.com/notifications/ws/json/1"
    assert headers == {"authorization": f"Bearer {h.tokens[0]}"}
    assert backoffs(caplog) == []
    assert events(caplog, "notifications_degraded") == []


async def test_a_sync_complete_frame_pokes_after_the_settle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = asyncio.Event()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if seconds == nt.SETTLE_SECONDS:
            await gate.wait()

    h = harness([[frame()]], sleep=sleep)
    task = asyncio.create_task(h.listener.run(h.stop))
    for _ in range(1000):
        if sleeps:
            break
        await asyncio.sleep(0.001)
    assert sleeps == [nt.SETTLE_SECONDS]
    assert len(h.pokes) == 1  # the connect's poke only: the frame's waits for the settle
    assert events(caplog, "notification") == [{"acct": ACCT, "reason": "SyncComplete"}]
    gate.set()
    for _ in range(1000):
        if len(h.pokes) == 2:
            break
        await asyncio.sleep(0.001)
    assert len(h.pokes) == 2
    h.stop.set()
    await asyncio.wait_for(task, 5)


async def test_odd_frames_count_as_other_and_still_poke(caplog: pytest.LogCaptureFixture) -> None:
    odd: list[Item] = ["not json", frame("SomethingNew"), b"\x00", "[1]"]
    h = harness([odd])
    await h.run(until=lambda: len(events(caplog, "notification")) == 4 and len(h.pokes) >= 2)
    assert [f["reason"] for f in events(caplog, "notification")] == ["other"] * 4
    assert len(h.pokes) >= 2
    assert h.sleeps.count(nt.SETTLE_SECONDS) >= 1


async def test_frames_during_the_settle_share_one_poke() -> None:
    gate = asyncio.Event()

    async def sleep(seconds: float) -> None:
        await gate.wait()

    h = harness([[frame(), frame(), frame()]], sleep=sleep)
    task = asyncio.create_task(h.listener.run(h.stop))
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.sleep(0.05)
    assert len(h.pokes) == 2  # connect + one settled poke for the burst
    h.stop.set()
    await asyncio.wait_for(task, 5)


async def test_reconnects_before_the_token_expires(caplog: pytest.LogCaptureFixture) -> None:
    lifetime = 3 * 3600
    h = harness([[Idle(lifetime - 120 - 1), Idle(1)], []])
    await h.run(until=lambda: len(h.connector.calls) == 2 and len(h.pokes) == 2)
    assert len(h.connector.calls) == 2
    assert len(h.tokens) == 2  # renewed before the second connect
    assert len(h.pokes) == 2  # one per connect
    assert backoffs(caplog) == []
    assert len(events(caplog, "notifications_renew")) == 1
    assert events(caplog, "notifications_degraded") == []


@pytest.mark.parametrize("exp", [None, WALL + 60, WALL - 10])
async def test_without_a_usable_expiry_it_waits_for_a_failure(
    caplog: pytest.LogCaptureFixture, exp: float | None
) -> None:
    def token() -> str:
        return user_token(tectonic="us") if exp is None else user_token(tectonic="us", exp=exp)

    h = harness([[Idle(10**6), Close()], []], token=token)
    await h.run(until=lambda: len(h.connector.calls) == 2)
    assert events(caplog, "notifications_renew") == []
    assert [f["reason"] for f in events(caplog, "notifications_degraded")] == ["ClosedError"]
    assert backoffs(caplog) == [1.0]


async def test_backoff_doubles_to_the_cap(caplog: pytest.LogCaptureFixture) -> None:
    failures: list[Script] = [OSError("refused") for _ in range(12)]
    h = harness(failures)
    await h.run()
    assert backoffs(caplog) == [1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300]
    assert h.pokes == []


def test_backoff_jitter_only_shortens() -> None:
    listener = NotificationListener(ACCT, lambda: "", lambda: None, rng=Rng(1.0))
    listener.failures = 3
    assert listener.backoff() == pytest.approx(4 * (1 - nt.BACKOFF_JITTER))
    listener.failures = 50
    assert listener.backoff() == pytest.approx(300 * (1 - nt.BACKOFF_JITTER))
    listener.contended = True
    assert listener.backoff() == pytest.approx(60 * (1 - nt.BACKOFF_JITTER))


async def test_three_quick_closes_mean_contention(caplog: pytest.LogCaptureFixture) -> None:
    quick: list[Script] = [[Close(1)] for _ in range(9)]
    h = harness([*quick, [Idle(10), Close()]])
    await h.run()
    contended = events(caplog, "notifications_contended")
    assert contended == [{"acct": ACCT, "count": 3}]
    # 1, 2, then the third quick close restarts at 1 s with the 60 s cap.
    assert backoffs(caplog)[:9] == [1, 2, 1, 2, 4, 8, 16, 32, 60]
    assert h.listener.contended is False  # a connection outlived 5 s
    assert h.listener.short_closes == 0
    assert len(events(caplog, "notifications_degraded")) == 1


async def test_a_connection_that_outlives_five_seconds_resets_the_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = harness([[Close(1)], [Close(1)], [Close(6)], [Close(1)], [Close(1)]])
    await h.run()
    assert events(caplog, "notifications_contended") == []


async def test_degraded_and_restored_are_logged_once_per_spell(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = harness(
        [
            OSError("down"),
            OSError("down"),
            [Idle(nt.HEALTHY_SECONDS + 1), Close(1)],
            TimeoutError(),
            [frame()],
        ]
    )
    names = {"notifications_degraded", "notifications_restored"}
    await h.run(until=lambda: len(order(caplog, names)) == 4)
    assert order(caplog, names) == [
        "notifications_degraded",
        "notifications_restored",
        "notifications_degraded",
        "notifications_restored",
    ]
    reasons = [f["reason"] for f in events(caplog, "notifications_degraded")]
    assert reasons == ["OSError", "ClosedError"]
    # A healthy connection resets the backoff.
    assert backoffs(caplog)[:4] == [1, 2, 1, 2]


async def test_a_rejected_device_token_stops_the_listener(caplog: pytest.LogCaptureFixture) -> None:
    def token() -> str:
        raise TokenRejectedError("rejected", status=401)

    h = harness([[]], token=token)
    task = asyncio.create_task(h.listener.run(h.stop))
    await asyncio.wait_for(task, 5)  # ends by itself, stop never set
    assert not h.stop.is_set()
    assert h.connector.calls == []
    assert events(caplog, "notifications_stopped") == [{"acct": ACCT, "reason": "token_rejected"}]


async def test_a_transient_renewal_failure_backs_off_and_retries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    def token() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TransientCloudError("token: HTTP 503", status=503)
        return user_token(tectonic="us", exp=WALL + 10_800)

    h = harness([[]], token=token)
    await h.run(until=lambda: bool(h.pokes))
    assert backoffs(caplog) == [1.0]
    assert [f["reason"] for f in events(caplog, "notifications_degraded")] == [
        "TransientCloudError"
    ]
    assert len(h.connector.calls) == 1


async def test_logs_carry_the_event_name_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = harness([[frame(), frame("Other"), Close(1)], OSError("x"), [frame()]])
    await h.run(until=lambda: len(events(caplog, "notification")) == 3)
    formatter = JsonFormatter()
    lines = [formatter.format(r) for r in caplog.records] + [r.getMessage() for r in caplog.records]
    text = "\n".join(lines)
    for secret in (MESSAGE_ID, USER_ID, DEVICE_ID, TRACE, "SyncEventNotification", *h.tokens):
        assert secret not in text
    assert "SyncComplete" in text
    for record in caplog.records:
        fields = getattr(record, "scriptorium_fields", {})
        assert set(fields) <= {"acct", "reason", "count", "duration_ms"}


async def test_stop_while_backing_off_ends_promptly() -> None:
    async def slow(seconds: float) -> None:
        await asyncio.sleep(3600)

    h = harness([OSError("down")], sleep=slow)
    task = asyncio.create_task(h.listener.run(h.stop))
    await asyncio.sleep(0.02)
    h.stop.set()
    await asyncio.wait_for(task, 2)
