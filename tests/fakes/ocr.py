"""Stand-ins for workstream F's OCR modules (M2 breakdown §3.1, §4): no test calls the API.

- :class:`FakeOcrClient` has the real ``OcrClient``'s public methods and signatures, records
  every call (the PNG's byte count, never its bytes), returns canned F ``Transcription``s and
  batch results, and raises scripted errors (F's own ``OcrRefused``/``OcrUnavailable``).
- :class:`FakeOcrFactory` replaces ``client_for`` (``Services.ocr_client``, ``OcrPorts.client``)
  and records which key each client was built with, so a test can tell the user's key from
  the server's.
- :class:`FakeSpendLedger` and :class:`FakeResolver` replace ``SpendLedger`` and
  ``ocr.modes.resolve`` where a pipeline test wants to set a cap or switch the mode directly;
  :func:`fake_ports` binds them into an ``OcrPorts``. They return F's value types
  (``Spend``, ``Allowance``, ``Resolution``), so the pipeline sees exactly what F ships.
  Tests that need the real resolver and ledger bind ``build_ocr_ports(ledger, client=factory)``
  instead.
"""

from __future__ import annotations

import hashlib
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import SecretStr

from scriptorium.config import Settings
from scriptorium.ocr._ports import OcrPorts
from scriptorium.ocr.client import (
    BatchCounts,
    BatchItem,
    BatchStatus,
    OcrItemError,
    Transcription,
    check_png,
)
from scriptorium.ocr.errors import OcrUnavailable
from scriptorium.ocr.ledger import Allowance, Degraded, Spend
from scriptorium.ocr.modes import Mode, Resolution
from scriptorium.ocr.prompt import Prompt
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import GlobalStore

__all__ = [
    "DEFAULT_MODEL",
    "FAKE_PROMPT",
    "FakeBatch",
    "FakeOcrClient",
    "FakeOcrFactory",
    "FakeResolver",
    "FakeSpendLedger",
    "SpendRow",
    "canned",
    "default_text",
    "fake_ports",
]

DEFAULT_MODEL = "claude-sonnet-5-5"
FAKE_PROMPT = Prompt("Transcribe this page as Markdown.", "v1")


def canned(
    text: str = "Transcribed page",
    *,
    model: str = DEFAULT_MODEL,
    input_tokens: int = 2300,
    output_tokens: int = 150,
    stop_reason: str = "end_turn",
    batch: bool = False,
) -> Transcription:
    """A Transcription with plausible token counts for one page."""
    return Transcription(text, model, input_tokens, output_tokens, stop_reason, batch)


def default_text(png: bytes) -> str:
    """A deterministic 'transcript' naming the image it was made from (by hash only)."""
    return f"# Page {hashlib.sha256(png).hexdigest()[:12]}\n\n- written by hand"


@dataclass
class FakeBatch:
    custom_ids: list[str]
    prompt_version: str
    pngs: dict[str, bytes] = field(default_factory=dict, repr=False)
    state: str = "in_progress"
    results: dict[str, Transcription | OcrItemError] = field(default_factory=dict)
    stream: list[tuple[object, object]] | None = None
    """Raw ``(custom_id, result)`` lines streamed instead of ``results`` (hostile streams)."""


class FakeOcrClient:
    """Records calls and answers from a script; never touches the network.

    ``transcriptions`` are returned (or, if exceptions, raised) in order by
    :meth:`transcribe`; when they run out every call returns :func:`canned` text, made by
    ``text(png)`` when it is set. ``key_error`` is raised by :meth:`validate_key`.
    ``check_images`` applies the real client's PNG header check, so a test notices a page
    that was not downscaled.
    """

    def __init__(
        self,
        api_key: SecretStr | None = None,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
        timeout_s: float = 120.0,
        transcriptions: Iterable[Transcription | Exception] = (),
        key_error: Exception | None = None,
        check_images: bool = True,
        text: Callable[[bytes], str] | None = None,
    ) -> None:
        self.api_key = api_key
        self._model = model
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.transcriptions: deque[Transcription | Exception] = deque(transcriptions)
        self.key_error = key_error
        self.check_images = check_images
        self.text = text
        self.calls: list[tuple[str, ...]] = []
        self.batches: dict[str, FakeBatch] = {}
        self.closed = 0
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"FakeOcrClient(model={self._model!r})"

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        """Counted, not enforced: one FakeOcrClient stands in for every client of a test."""
        with self._lock:
            self.closed += 1

    def __enter__(self) -> FakeOcrClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---- what was asked --------------------------------------------------------------------

    def count(self, name: str) -> int:
        """Calls of the method ``name`` so far."""
        with self._lock:
            return sum(1 for call in self.calls if call[0] == name)

    def pages_sent(self) -> int:
        """Pages sent to Claude: single calls and batch items together."""
        with self._lock:
            singles = sum(1 for call in self.calls if call[0] == "transcribe")
            return singles + sum(len(b.custom_ids) for b in self.batches.values())

    # ---- the client's interface ------------------------------------------------------------

    def transcribe(self, png: bytes, prompt: Prompt) -> Transcription:
        with self._lock:
            self.calls.append(("transcribe", str(len(png)), prompt.version))
        if self.check_images:
            check_png(png)
        with self._lock:
            nxt = self.transcriptions.popleft() if self.transcriptions else None
        if isinstance(nxt, Exception):
            raise nxt
        if nxt is not None:
            return nxt
        return canned(self.text(png) if self.text else "Transcribed page", model=self._model)

    def validate_key(self) -> None:
        with self._lock:
            self.calls.append(("validate_key",))
        if self.key_error is not None:
            raise self.key_error

    def submit_batch(self, items: Sequence[BatchItem], prompt: Prompt) -> str:
        with self._lock:
            self.calls.append(("submit_batch", str(len(items)), prompt.version))
        if self.check_images:
            for item in items:
                check_png(item.png)
        with self._lock:
            batch_id = f"msgbatch_fake_{len(self.batches) + 1:04d}"
            self.batches[batch_id] = FakeBatch(
                [i.custom_id for i in items], prompt.version, {i.custom_id: i.png for i in items}
            )
        return batch_id

    def finish_batch(
        self,
        batch_id: str,
        results: Mapping[str, Transcription | OcrItemError] | None = None,
        *,
        state: str = "ended",
        stream: Sequence[tuple[object, object]] | None = None,
    ) -> None:
        """End a batch with ``results`` (default: a batch transcript per page) or a raw
        ``stream`` of result lines."""
        batch = self.batches[batch_id]
        batch.state = state
        if results is None:
            results = {
                cid: canned(self.text(batch.pngs[cid]) if self.text else f"Page {cid}", batch=True)
                for cid in batch.custom_ids
            }
        batch.results = dict(results)
        batch.stream = None if stream is None else list(stream)

    def batch_status(self, batch_id: str) -> BatchStatus:
        with self._lock:
            self.calls.append(("batch_status", batch_id))
        batch = self.batches.get(batch_id)
        if batch is None:
            return BatchStatus("expired", BatchCounts())
        if batch.state != "ended":
            return BatchStatus(batch.state, BatchCounts(processing=len(batch.custom_ids)))  # type: ignore[arg-type]
        ok = sum(isinstance(r, Transcription) for r in batch.results.values())
        return BatchStatus("ended", BatchCounts(succeeded=ok, errored=len(batch.results) - ok))

    def batch_results(self, batch_id: str) -> Iterator[tuple[str, Transcription | OcrItemError]]:
        with self._lock:
            self.calls.append(("batch_results", batch_id))
        batch = self.batches.get(batch_id)
        if batch is None or batch.state != "ended":
            raise OcrUnavailable("batch_in_progress")
        if batch.stream is not None:
            return iter(list(batch.stream))  # type: ignore[arg-type]
        return iter(list(batch.results.items()))


class FakeOcrFactory:
    """Replaces ``client_for``: hands out one shared FakeOcrClient, remembering each key."""

    def __init__(self, client: FakeOcrClient | None = None) -> None:
        self.client = client or FakeOcrClient()
        self.keys: list[str] = []

    def __call__(self, settings: Settings, api_key: SecretStr) -> FakeOcrClient:
        self.keys.append(api_key.get_secret_value())
        self.client.api_key = api_key
        return self.client


# ---- the ledger and the resolver --------------------------------------------------------------


@dataclass(frozen=True)
class SpendRow:
    uid: str
    mode: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    at: datetime


@dataclass
class FakeSpendLedger:
    """Spend rows in memory; caps in USD per month (None: no own cap), set by the test."""

    global_cap: float = 20.0
    user_cap: float = 10.0
    own_cap: float | None = None
    rows: list[SpendRow] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(
        self,
        *,
        uid: str,
        mode: Mode,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        at: datetime,
    ) -> None:
        assert mode in ("user", "server")
        with self._lock:
            self.rows.append(SpendRow(uid, mode, model, input_tokens, output_tokens, cost_usd, at))

    def month(self, *, uid: str | None, mode: Mode | None, at: datetime) -> Spend:
        with self._lock:
            rows = [
                r
                for r in self.rows
                if (uid is None or r.uid == uid)
                and (mode is None or r.mode == mode)
                and (r.at.year, r.at.month) == (at.year, at.month)
            ]
        return Spend(
            cost_usd=sum(r.cost_usd for r in rows),
            pages=len(rows),
            input_tokens=sum(r.input_tokens for r in rows),
            output_tokens=sum(r.output_tokens for r in rows),
        )

    def allowance(
        self,
        settings: Settings,
        store: GlobalStore,
        uid: str,
        at: datetime,
        *,
        mode: Mode | None = None,
    ) -> Allowance:
        """F's rules: ``server`` checks global then user, ``user`` the own cap, None all."""
        del settings, store
        server_all = self.month(uid=None, mode="server", at=at).cost_usd
        server_user = self.month(uid=uid, mode="server", at=at).cost_usd
        own = self.month(uid=uid, mode="user", at=at).cost_usd
        global_left = max(0.0, self.global_cap - server_all)
        user_left = max(0.0, self.user_cap - server_user)
        own_left = None if self.own_cap is None else max(0.0, self.own_cap - own)
        checks: list[tuple[Degraded, float | None]] = []
        if mode in (None, "server"):
            checks += [("global", global_left), ("user", user_left)]
        if mode in (None, "user"):
            checks.append(("own", own_left))
        degraded: Degraded = False
        for name, left in checks:
            if left is not None and left <= 0:
                degraded = name
                break
        return Allowance(global_left, user_left, own_left, degraded)

    def pages(self) -> int:
        with self._lock:
            return len(self.rows)


@dataclass
class FakeResolver:
    """``resolve`` with a fixed preferred mode; caps send it to lazy as F's does."""

    mode: str = "server"
    calls: int = 0

    def __call__(
        self,
        settings: Settings,
        store: GlobalStore,
        secrets: SecretBox,
        ledger: Any,
        uid: str,
        now: datetime,
    ) -> Resolution:
        self.calls += 1
        if self.mode == "lazy":
            return Resolution("lazy", "no_key")
        if self.mode == "user":
            if ledger.allowance(settings, store, uid, now, mode="user").degraded:
                return Resolution("lazy", "user_cap_reached")
            return Resolution("user", "user_key", SecretStr("sk-ant-user-test"))
        degraded = ledger.allowance(settings, store, uid, now, mode="server").degraded
        if degraded:
            return Resolution(
                "lazy", "cap_reached_global" if degraded == "global" else "cap_reached_user"
            )
        return Resolution("server", "server_key", SecretStr("sk-ant-server-test"))


def fake_ports(
    *,
    client: FakeOcrClient | None = None,
    ledger: FakeSpendLedger | None = None,
    resolver: FakeResolver | None = None,
    prompt: Prompt = FAKE_PROMPT,
) -> OcrPorts:
    """OcrPorts over the fakes; a batch is collected with a fixed test key of its mode."""
    factory = FakeOcrFactory(client if client is not None else FakeOcrClient(text=default_text))

    def batch_key(settings: Settings, box: SecretBox, mode: str) -> SecretStr | None:
        del settings, box
        return SecretStr(f"sk-ant-{mode}-test")

    return OcrPorts(
        ledger=ledger if ledger is not None else FakeSpendLedger(),
        client=factory,
        resolve=resolver if resolver is not None else FakeResolver(),
        prompt=lambda: prompt,
        batch_key=batch_key,
    )
