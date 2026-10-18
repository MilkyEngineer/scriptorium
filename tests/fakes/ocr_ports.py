"""Fakes of workstream F's OCR seams (Plan 05 §3.1) for the transcription pipeline's tests.

No test makes a Claude API call: :class:`FakeOcrClient` stands in for ``OcrClient`` and
counts what it was asked, :class:`FakeSpendLedger` for ``SpendLedger`` (it keeps the spend
rows in memory and derives the allowance from caps the test sets), :class:`FakeResolver` for
``ocr.modes.resolve`` (a fixed mode that falls back to lazy when the ledger says the cap is
reached, in §8.1's order). :func:`fake_ports` binds them into an :class:`OcrPorts`.

The value types mirror F's frozen dataclasses field for field, so the Protocols in
``scriptorium.ocr._ports`` are checked against the same shapes F ships.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import SecretStr

from scriptorium.config import Settings
from scriptorium.ocr._ports import OcrPorts
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import GlobalStore

PRICE_IN_PER_MTOK = 3.0
PRICE_OUT_PER_MTOK = 15.0


@dataclass(frozen=True)
class Prompt:
    text: str = "Transcribe this page as Markdown."
    version: str = "v1"


@dataclass(frozen=True)
class Resolution:
    mode: str
    reason: str
    api_key: SecretStr | None = None


@dataclass(frozen=True)
class Spend:
    cost_usd: float = 0.0
    pages: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Allowance:
    global_remaining_usd: float
    user_remaining_usd: float
    own_remaining_usd: float | None
    degraded: bool | str


@dataclass(frozen=True)
class Transcription:
    text: str
    model: str = "claude-fake-1"
    input_tokens: int = 1500
    output_tokens: int = 200
    stop_reason: str = "end_turn"


@dataclass(frozen=True)
class OcrItemError:
    reason: str


@dataclass(frozen=True)
class BatchStatus:
    state: str


@dataclass(frozen=True)
class BatchItem:
    custom_id: str
    png: bytes


class OcrUnavailable(Exception):  # noqa: N818 - F's name
    """Try again later (429, 529, network)."""


class OcrRefused(Exception):  # noqa: N818 - F's name
    """A verdict: the image or the key was refused."""


def cost_usd(settings: Settings, input_tokens: int, output_tokens: int) -> float:
    del settings
    return (input_tokens * PRICE_IN_PER_MTOK + output_tokens * PRICE_OUT_PER_MTOK) / 1e6


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
    """Spend rows in memory; caps in USD per month (None: no own cap)."""

    global_cap: float = 20.0
    user_cap: float = 10.0
    own_cap: float | None = None
    rows: list[SpendRow] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(
        self,
        *,
        uid: str,
        mode: Any,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        at: datetime,
    ) -> None:
        with self._lock:
            self.rows.append(
                SpendRow(uid, str(mode), model, input_tokens, output_tokens, cost_usd, at)
            )

    def month(self, *, uid: str | None, mode: Any, at: datetime) -> Spend:
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
        self, settings: Settings, store: GlobalStore, uid: str, at: datetime
    ) -> Allowance:
        del settings, store
        server_all = self.month(uid=None, mode="server", at=at).cost_usd
        server_user = self.month(uid=uid, mode="server", at=at).cost_usd
        own = self.month(uid=uid, mode="user", at=at).cost_usd
        global_left = self.global_cap - server_all
        user_left = self.user_cap - server_user
        own_left = None if self.own_cap is None else self.own_cap - own
        degraded: bool | str = False
        if global_left <= 0:
            degraded = "global"
        elif user_left <= 0:
            degraded = "user"
        elif own_left is not None and own_left <= 0:
            degraded = "own"
        return Allowance(global_left, user_left, own_left, degraded)

    def pages(self) -> int:
        with self._lock:
            return len(self.rows)


@dataclass
class FakeResolver:
    """``resolve`` with a fixed preferred mode; caps send it to lazy like F's does."""

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
        allowance = ledger.allowance(settings, store, uid, now)
        if self.mode == "user":
            if allowance.degraded == "own":
                return Resolution("lazy", "user_cap_reached")
            return Resolution("user", "user_key", SecretStr("sk-ant-user-test"))
        if allowance.degraded in ("global", "user"):
            return Resolution("lazy", f"cap_reached_{allowance.degraded}")
        return Resolution("server", "server_key", SecretStr("sk-ant-server-test"))


def default_text(png: bytes) -> str:
    """A deterministic 'transcript' naming the image it was made from (by hash only)."""
    return f"# Page {hashlib.sha256(png).hexdigest()[:12]}\n\n- written by hand"


@dataclass
class FakeOcrClient:
    """Counts calls; ``errors`` are raised by the next ``transcribe`` calls, in order."""

    text: Callable[[bytes], str] = default_text
    errors: list[BaseException] = field(default_factory=list)
    transcribed: list[bytes] = field(default_factory=list)
    batches: dict[str, list[BatchItem]] = field(default_factory=dict)
    states: dict[str, str] = field(default_factory=dict)
    results: dict[str, list[tuple[object, object]]] = field(default_factory=dict)
    """Batch results to stream instead of one transcription per submitted item."""
    status_calls: int = 0
    keys: list[bool] = field(default_factory=list)
    """Whether each client was made with a key (never the key itself)."""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def transcribe(self, png: bytes, prompt: Any) -> Transcription:
        assert isinstance(prompt, Prompt)
        with self._lock:
            if self.errors:
                raise self.errors.pop(0)
            self.transcribed.append(png)
        return Transcription(self.text(png))

    def submit_batch(self, items: Sequence[Any], prompt: Any) -> str:
        assert isinstance(prompt, Prompt)
        with self._lock:
            batch_id = f"msgbatch_{len(self.batches) + 1:04d}"
            self.batches[batch_id] = [BatchItem(i.custom_id, i.png) for i in items]
            self.states.setdefault(batch_id, "in_progress")
        return batch_id

    def batch_status(self, batch_id: str) -> BatchStatus:
        with self._lock:
            self.status_calls += 1
            return BatchStatus(self.states.get(batch_id, "ended"))

    def batch_results(self, batch_id: str) -> Iterator[tuple[str, object]]:
        if batch_id in self.results:
            yield from self.results[batch_id]  # type: ignore[misc]
            return
        for item in self.batches.get(batch_id, []):
            yield item.custom_id, Transcription(self.text(item.png))

    def calls(self) -> int:
        """Pages sent to Claude, single calls and batch items together."""
        with self._lock:
            return len(self.transcribed) + sum(len(v) for v in self.batches.values())


def fake_ports(
    *,
    client: FakeOcrClient | None = None,
    ledger: FakeSpendLedger | None = None,
    resolver: FakeResolver | None = None,
    prompt: Prompt | None = None,
) -> OcrPorts:
    the_client = client if client is not None else FakeOcrClient()
    the_prompt = prompt if prompt is not None else Prompt()

    def make_client(key: SecretStr) -> FakeOcrClient:
        the_client.keys.append(bool(key.get_secret_value()))
        return the_client

    def batch_key(settings: Settings, box: SecretBox, mode: str) -> SecretStr | None:
        del settings, box
        return SecretStr(f"sk-ant-{mode}-test")

    return OcrPorts(
        resolve=resolver if resolver is not None else FakeResolver(),
        ledger=ledger if ledger is not None else FakeSpendLedger(),
        client=make_client,
        prompt=lambda: the_prompt,
        batch_item=BatchItem,
        cost_usd=cost_usd,
        transcription=Transcription,
        unavailable=(OcrUnavailable,),
        refused=(OcrRefused,),
        batch_key=batch_key,
    )
