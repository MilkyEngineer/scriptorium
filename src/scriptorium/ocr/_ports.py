"""The four OCR seams the transcription pipeline takes from one place (Plan 05 §3.1, §3.2).

The pipeline (``ocr/runner.py``, the OCR tools, ``account_status.ocr``) reaches workstream F's
modules directly for everything that is a value or a pure function: ``Transcription``,
``BatchItem``, ``BatchStatus``, ``OcrItemError``, ``Prompt``, ``Resolution``, ``Spend``,
``Allowance``, ``cost_usd``, ``check_png``, ``OcrRefused`` and ``OcrUnavailable``. Only what
holds state, spends money or decides the mode goes through an :class:`OcrPorts`:

- ``ledger``: the process's one ``SpendLedger`` (the server's ``Services.ledger``, the
  worker's opened in ``Worker.open``), so every spend row and every cap check of a process
  goes through one object;
- ``client``: ``client_for(settings, key)``, the only way to an ``OcrClient``;
- ``resolve``: ``ocr.modes.resolve``;
- ``prompt``: ``ocr.prompt.load_prompt``;
- ``batch_key``: the key a batch is collected with.

Tests replace any of them (``tests/fakes/ocr.py``: a fake client, an in-memory ledger whose
caps a test sets directly, a resolver whose mode a test switches) without key material or
settings juggling; production builds them with :func:`build_ocr_ports`. Nothing here holds a
key longer than a call: ``Resolution.api_key`` and the batch key go to the client factory
and are dropped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import SecretStr

from scriptorium.config import Settings
from scriptorium.errors import ScriptoriumError
from scriptorium.ocr import modes
from scriptorium.ocr.client import (
    BatchItem,
    BatchStatus,
    OcrItemError,
    Transcription,
    client_for,
)
from scriptorium.ocr.ledger import Allowance, Spend
from scriptorium.ocr.modes import Mode, Resolution, read_user_key
from scriptorium.ocr.prompt import Prompt, load_prompt
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import GlobalStore

__all__ = [
    "ClientFactory",
    "OcrClientLike",
    "OcrPorts",
    "ResolveFn",
    "SpendLedgerLike",
    "build_ocr_ports",
    "default_batch_key",
]


class SpendLedgerLike(Protocol):
    """``ocr.ledger.SpendLedger`` as the pipeline uses it."""

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
        batch_id: str | None = None,
        custom_id: str | None = None,
    ) -> bool: ...
    def month(self, *, uid: str | None, mode: Mode | None, at: datetime) -> Spend: ...
    def allowance(
        self,
        settings: Settings,
        store: GlobalStore,
        uid: str,
        at: datetime,
        *,
        mode: Mode | None = None,
    ) -> Allowance: ...


class OcrClientLike(Protocol):
    """``ocr.client.OcrClient`` as the pipeline uses it (one instance per key, then closed)."""

    def transcribe(self, png: bytes, prompt: Prompt) -> Transcription: ...
    def submit_batch(self, items: Sequence[BatchItem], prompt: Prompt) -> str: ...
    def batch_status(self, batch_id: str) -> BatchStatus: ...
    def batch_results(
        self, batch_id: str
    ) -> Iterator[tuple[str, Transcription | OcrItemError]]: ...
    def close(self) -> None: ...


ClientFactory = Callable[[Settings, SecretStr], OcrClientLike]
"""``ocr.client.client_for(settings, key)``."""
ResolveFn = Callable[[Settings, GlobalStore, SecretBox, Any, str, datetime], Resolution]
"""``ocr.modes.resolve(settings, store, secrets, ledger, uid, now)``."""


def default_batch_key(settings: Settings, box: SecretBox, mode: str) -> SecretStr | None:
    """The key to collect a batch submitted in ``mode`` with, whatever ``resolve`` says now.

    Collecting results costs nothing more (the spend happened at submission), so a batch
    submitted before a cap was reached, or before the server key was withdrawn, is still
    collected and recorded. ``user``: the user's own key from the ``SecretBox``; ``server``:
    ``ANTHROPIC_API_KEY``. None when that key is gone or cannot be read.
    """
    if mode == "user":
        try:
            return read_user_key(box)
        except ScriptoriumError:
            return None
    if mode == "server":
        return settings.anthropic_api_key
    return None


@dataclass(frozen=True)
class OcrPorts:
    """F's stateful OCR seams as the pipeline uses them; one per process."""

    ledger: SpendLedgerLike
    client: ClientFactory = client_for
    resolve: ResolveFn = modes.resolve
    prompt: Callable[[], Prompt] = load_prompt
    batch_key: Callable[[Settings, SecretBox, str], SecretStr | None] = default_batch_key


def build_ocr_ports(ledger: SpendLedgerLike, *, client: ClientFactory = client_for) -> OcrPorts:
    """The production binding over the process's ledger (``client`` is ``Services.ocr_client``
    on the server, so ``set_claude_api_key`` and the OCR tools share one factory)."""
    return OcrPorts(ledger=ledger, client=client)
