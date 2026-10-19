"""The cloud's change notifications as a wake-up for the sync loop (Architecture §7, S7).

One :class:`NotificationListener` per linked account, in the worker only (the server never
connects). It holds the account's one websocket to
``wss://<region>.tectonic.remarkable.com/notifications/ws/json/1``, authenticated with a
freshly renewed user token, and on every frame calls ``on_event`` (the account loop's
:meth:`~scriptorium.worker.AccountLoop.poke`) after a short settle. The channel is a pure
wake-up: S7 found that frames carry no document, generation or change kind, so nothing in a
frame decides anything. Polling stays the source of truth; when the socket is down the
loop just polls on its interval.

Behaviour (S7 Decision items 1-5):

- Before every connect the user token is renewed; its ``tectonic`` claim names the region
  (``eu`` if absent or malformed) and its ``exp`` sets a proactive reconnect at
  ``exp - RENEW_MARGIN_SECONDS``, ahead of the server's own cut about a minute before
  expiry. Claims are read without verification and never logged: the token carries the
  user's name and email.
- After every successful (re)connect: one poke (an event during the handshake gap is lost).
- Each frame: the event name is logged only if it is in :data:`KNOWN_EVENTS`, else
  ``other``; nothing else from the frame is logged or kept. Then a poke after
  :data:`SETTLE_SECONDS` (the root had moved within 1.7 s in 30 of 30 cases).
- Failures (renewal error, handshake refused, network error, close) back off from 1 s
  doubling to :data:`BACKOFF_CAP_SECONDS` with jitter. A connection that closes within
  :data:`CONTENDED_SECONDS` of opening, three times in a row, means another process holds
  the device's slot (the server kicks the older socket): ``notifications_contended`` is
  logged once and the backoff restarts at 1 s with a cap of :data:`CONTENDED_CAP_SECONDS`
  until a connection outlives :data:`CONTENDED_SECONDS`. ``notifications_degraded`` is
  logged once at the first failure, ``notifications_restored`` once a connection lasts
  :data:`HEALTHY_SECONDS` or receives a frame.
- A device token rejected by the token endpoint (401/403) ends the listener
  (``notifications_stopped``): that failure class belongs to the poll loop (§7 step 7),
  and the worker starts a new listener once the loop polls successfully again.
- One socket at a time: the old socket is closed before a new one is opened, because a
  second connection with the same device token kicks the first.

Logs carry the account id, a reason code, counts and durations only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, Protocol

import jwt
from websockets.asyncio.client import connect as _ws_connect

from scriptorium.cloud.errors import TokenRejectedError
from scriptorium.logging import log_event

__all__ = [
    "BACKOFF_CAP_SECONDS",
    "CONTENDED_CAP_SECONDS",
    "CONTENDED_SECONDS",
    "DEFAULT_REGION",
    "HEALTHY_SECONDS",
    "KNOWN_EVENTS",
    "MAX_FRAME_BYTES",
    "PING_INTERVAL_SECONDS",
    "RENEW_MARGIN_SECONDS",
    "SETTLE_SECONDS",
    "Connector",
    "NotificationListener",
    "Socket",
    "TokenClaims",
    "event_label",
    "notifications_url",
    "token_claims",
    "websocket_connector",
]

logger = logging.getLogger(__name__)

SETTLE_SECONDS: Final = 2.0
"""Delay between a frame and the poke: the new root is readable by then (S7 §3)."""
RENEW_MARGIN_SECONDS: Final = 120
"""Reconnect this long before the user token's ``exp`` (the server cuts at about 60 s)."""
CONTENDED_SECONDS: Final = 5.0
"""A connection that closes sooner than this after opening may have been kicked."""
CONTENDED_AFTER: Final = 3
"""Consecutive short connections that mean another process holds the device's slot."""
CONTENDED_CAP_SECONDS: Final = 60.0
BACKOFF_CAP_SECONDS: Final = 300.0
HEALTHY_SECONDS: Final = 60.0
"""A connection this old (or one that delivered a frame) ends a degraded spell."""
BACKOFF_JITTER: Final = 0.2
"""Each backoff is shortened by up to this fraction, at random."""
PING_INTERVAL_SECONDS: Final = 20.0
MAX_FRAME_BYTES: Final = 1024 * 1024
OPEN_TIMEOUT_SECONDS: Final = 30.0
CLOSE_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_REGION: Final = "eu"
KNOWN_EVENTS: Final = frozenset({"SyncComplete"})
"""Event names that may appear in a log line; anything else is logged as ``other``."""

_HOST_SUFFIX: Final = ".tectonic.remarkable.com"
_PATH: Final = "/notifications/ws/json/1"
_REGION_RE: Final = re.compile(r"[a-z0-9]{1,16}")


class Uniform(Protocol):
    """The part of :class:`random.Random` the backoff jitter uses."""

    def uniform(self, a: float, b: float, /) -> float: ...


class Socket(Protocol):
    """What the listener needs from an open websocket."""

    async def recv(self) -> str | bytes: ...


Connector = Callable[[str, Mapping[str, str]], AbstractAsyncContextManager[Socket]]
"""``(url, headers)`` → an async context manager holding one open socket; leaving it closes
the socket. Injectable so tests never touch the network."""


# ---- pure helpers ----------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenClaims:
    """The two user-token claims the listener reads; nothing else is ever kept."""

    region: str
    exp: float | None


def token_claims(token: str) -> TokenClaims:
    """``tectonic`` (the region, :data:`DEFAULT_REGION` if absent or not a short lowercase
    code) and ``exp`` (None if absent or not a number) from the user token.

    Decoded without verification: the token is reMarkable's and only steers which of its
    hosts to ask and when to reconnect. Never log the token or any claim.
    """
    try:
        claims = jwt.decode(
            token, options={"verify_signature": False, "verify_exp": False, "verify_aud": False}
        )
    except jwt.PyJWTError:
        return TokenClaims(DEFAULT_REGION, None)
    region = claims.get("tectonic")
    if not (isinstance(region, str) and _REGION_RE.fullmatch(region)):
        region = DEFAULT_REGION
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int | float) or exp != exp:  # NaN
        return TokenClaims(region, None)
    return TokenClaims(region, float(exp))


def notifications_url(region: str) -> str:
    """The notifications endpoint of ``region`` (already checked by :func:`token_claims`)."""
    if not _REGION_RE.fullmatch(region):
        region = DEFAULT_REGION
    return f"wss://{region}{_HOST_SUFFIX}{_PATH}"


def event_label(frame: str | bytes) -> str:
    """The frame's ``message.attributes.event`` if it is in :data:`KNOWN_EVENTS`, else
    ``other`` (also for binary, non-JSON or oddly shaped frames). Nothing else is read."""
    if not isinstance(frame, str):
        return "other"
    try:
        body: Any = json.loads(frame)
    except (ValueError, RecursionError):
        return "other"
    message = body.get("message") if isinstance(body, dict) else None
    attributes = message.get("attributes") if isinstance(message, dict) else None
    event = attributes.get("event") if isinstance(attributes, dict) else None
    return event if isinstance(event, str) and event in KNOWN_EVENTS else "other"


# ---- the real connector ----------------------------------------------------------------------


class _NoRedirects(_ws_connect):
    """``websockets`` follows redirects by default; a redirect here is a failure instead,
    so the bearer token only ever goes to the host derived from its own claim."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


@asynccontextmanager
async def websocket_connector(url: str, headers: Mapping[str, str]) -> AsyncIterator[Socket]:
    """Open ``url`` with ``websockets``: client pings every 20 s, frames up to 1 MiB, no
    proxy from the environment, no redirects."""
    async with _NoRedirects(
        url,
        additional_headers=dict(headers),
        user_agent_header="scriptorium",
        proxy=None,
        open_timeout=OPEN_TIMEOUT_SECONDS,
        ping_interval=PING_INTERVAL_SECONDS,
        ping_timeout=PING_INTERVAL_SECONDS,
        close_timeout=CLOSE_TIMEOUT_SECONDS,
        max_size=MAX_FRAME_BYTES,
    ) as socket:
        yield socket


# ---- the listener ----------------------------------------------------------------------------


class _RenewDueError(Exception):
    """Internal: the proactive reconnect before the token expires is due."""


class NotificationListener:
    """Holds one account's notifications websocket and pokes its loop (module docstring)."""

    def __init__(
        self,
        acct: str,
        user_token: Callable[[], str],
        on_event: Callable[[], None],
        *,
        connector: Connector = websocket_connector,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Uniform | None = None,
    ) -> None:
        """``user_token`` is blocking and returns a freshly renewed user token
        (``CloudClient.user_token(renew=True)``); it runs in a thread. ``clock`` measures
        connection ages, ``wall`` is compared with ``exp``, ``sleep`` waits out backoffs and
        the settle; tests replace all three."""
        self.acct = acct
        self._user_token = user_token
        self._on_event = on_event
        self._connector = connector
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self._rng: Uniform = rng if rng is not None else random.SystemRandom()
        self.failures = 0
        """Consecutive failures since the last healthy connection (sets the backoff)."""
        self.short_closes = 0
        """Consecutive connections that closed within :data:`CONTENDED_SECONDS`."""
        self.contended = False
        self.degraded = False
        self.connects = 0
        self._settle: asyncio.Task[None] | None = None

    async def run(self, stop: asyncio.Event) -> None:
        """Listen until ``stop`` is set or the device token is rejected."""
        main = asyncio.create_task(self._loop(), name=f"notify-main-{self.acct}")
        stopper = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({main, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (main, stopper, self._settle):
                if task is not None:
                    task.cancel()
            pending = [t for t in (main, stopper, self._settle) if t is not None]
            await asyncio.gather(*pending, return_exceptions=True)
        if main.done() and not main.cancelled() and main.exception() is not None:
            log_event(
                logger,
                "notifications_failed",
                level=logging.ERROR,
                acct=self.acct,
                reason=type(main.exception()).__name__,
            )

    # ---- one connection after another ------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                token = await asyncio.to_thread(self._user_token)
            except TokenRejectedError:
                log_event(
                    logger,
                    "notifications_stopped",
                    level=logging.WARNING,
                    acct=self.acct,
                    reason="token_rejected",
                )
                return
            except Exception as exc:
                await self._failed(type(exc).__name__, lived=None)
                continue
            claims = token_claims(token)
            renew_in = None
            if claims.exp is not None:
                remaining = claims.exp - RENEW_MARGIN_SECONDS - self._wall()
                renew_in = remaining if remaining > 0 else None
            url = notifications_url(claims.region)
            headers = {"authorization": f"Bearer {token}"}
            del token
            opened: float | None = None
            try:
                async with self._connector(url, headers) as socket:
                    opened = self._clock()
                    await self._connected()
                    await self._listen(socket, opened, renew_in)
            except _RenewDueError:
                log_event(logger, "notifications_renew", acct=self.acct)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                lived = None if opened is None else self._clock() - opened
                await self._failed(type(exc).__name__, lived=lived)
                continue
            # The socket's context ended without an error: the server closed it cleanly.
            lived = None if opened is None else self._clock() - opened
            await self._failed("closed", lived=lived)

    async def _connected(self) -> None:
        self.connects += 1
        log_event(logger, "notifications_connected", level=logging.DEBUG, acct=self.acct)
        self._on_event()  # an event during the handshake gap is lost: poll once anyway

    async def _listen(self, socket: Socket, opened: float, renew_in: float | None) -> None:
        """Receive until the socket closes (raises) or the proactive renewal is due."""
        renew_at = None if renew_in is None else opened + renew_in
        healthy = False
        while True:
            now = self._clock()
            age = now - opened
            if not healthy and age >= HEALTHY_SECONDS:
                healthy = True
                self._healthy()
            if age >= CONTENDED_SECONDS and (self.contended or self.short_closes):
                self._outlived_contention()
            if renew_at is not None and now >= renew_at:
                raise _RenewDueError
            deadlines = [d for d in (renew_at,) if d is not None]
            if not healthy:
                deadlines.append(opened + HEALTHY_SECONDS)
            if self.contended or self.short_closes:
                deadlines.append(opened + CONTENDED_SECONDS)
            future = [d - now for d in deadlines if d > now]
            timeout = min(future) if future else None
            try:
                frame = await asyncio.wait_for(socket.recv(), timeout)
            except TimeoutError:
                continue
            label = event_label(frame)
            del frame
            log_event(logger, "notification", acct=self.acct, reason=label)
            if not healthy:
                healthy = True
                self._healthy()
            self._schedule_poke()

    def _schedule_poke(self) -> None:
        """Poke after the settle; frames arriving meanwhile share the one pending poke."""
        if self._settle is not None and not self._settle.done():
            return
        self._settle = asyncio.create_task(self._settled_poke())

    async def _settled_poke(self) -> None:
        await self._sleep(SETTLE_SECONDS)
        self._on_event()

    # ---- health bookkeeping ----------------------------------------------------------------

    def _healthy(self) -> None:
        self.failures = 0
        if self.degraded:
            self.degraded = False
            log_event(logger, "notifications_restored", acct=self.acct)

    def _outlived_contention(self) -> None:
        self.short_closes = 0
        self.contended = False

    async def _failed(self, reason: str, *, lived: float | None) -> None:
        """Record one failure (``lived``: how long the socket was open, None if it never
        opened) and wait out the backoff."""
        if lived is not None:
            if lived < CONTENDED_SECONDS:
                self.short_closes += 1
            else:
                self._outlived_contention()
        if not self.degraded:
            self.degraded = True
            log_event(
                logger,
                "notifications_degraded",
                level=logging.WARNING,
                acct=self.acct,
                reason=reason,
            )
        self.failures += 1
        if not self.contended and self.short_closes >= CONTENDED_AFTER:
            self.contended = True
            self.failures = 1
            log_event(
                logger,
                "notifications_contended",
                level=logging.WARNING,
                acct=self.acct,
                count=self.short_closes,
            )
        delay = self.backoff()
        log_event(
            logger,
            "notifications_retry",
            level=logging.DEBUG,
            acct=self.acct,
            reason=reason,
            count=self.failures,
            duration_ms=round(delay * 1000),
        )
        await self._sleep(delay)

    def backoff(self) -> float:
        """The wait after :attr:`failures` consecutive failures: 1 s doubling to the cap
        (:data:`CONTENDED_CAP_SECONDS` while contended), shortened by up to
        :data:`BACKOFF_JITTER` at random."""
        cap = CONTENDED_CAP_SECONDS if self.contended else BACKOFF_CAP_SECONDS
        base = min(cap, 2.0 ** min(max(self.failures, 1) - 1, 16))
        return base * (1.0 - BACKOFF_JITTER * self._rng.uniform(0.0, 1.0))
