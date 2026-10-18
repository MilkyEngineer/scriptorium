"""What the transcription pipeline uses from workstream F (Plan 05 §3.1), as Protocols.

The pipeline (``ocr/batches.py``, ``ocr/pipeline.py``, ``ocr/runner.py``, the OCR tools) is
written against these shapes, never against F's modules directly, so it can be built and
tested before F's ``ocr/modes.py``, ``ocr/ledger.py``, ``ocr/client.py`` and
``ocr/prompt.py`` exist. Tests inject fakes (``tests/fakes/ocr.py``). :func:`build_ocr_ports`
is the one place that binds F's real classes; until it does, it returns ``None`` and every
caller behaves as in M1 (lazy mode: the user's own Claude session transcribes).

Every attribute is declared as a read-only property so F's frozen dataclasses (with
``Literal`` fields) satisfy the Protocols structurally. Parameters that only travel from one
of F's functions back into another (the prompt, batch items, the ledger passed to
``resolve``) are typed ``Any`` here so F's concrete parameter types stay compatible.

Nothing here holds a key longer than a call: ``Resolution.api_key`` and the batch key are
handed to the client factory and dropped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from pydantic import SecretStr

from scriptorium.config import Settings
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import GlobalStore

__all__ = [
    "BATCH_DISCOUNT",
    "CLAUDE_KEY_NAME",
    "AllowanceLike",
    "BatchItemLike",
    "BatchStatusLike",
    "OcrClientLike",
    "OcrItemErrorLike",
    "OcrPorts",
    "PromptLike",
    "ResolutionLike",
    "ResolveFn",
    "SpendLedgerLike",
    "SpendLike",
    "TranscriptionLike",
    "build_ocr_ports",
    "default_batch_key",
]

CLAUDE_KEY_NAME: Final = "claude_api_key"
"""The ``SecretBox`` name of the user's own key (§3.1)."""
BATCH_DISCOUNT: Final = 0.5
"""Message Batches cost half the standard price; ``cost_usd`` (F) prices a standard call."""


class PromptLike(Protocol):
    @property
    def text(self) -> str: ...
    @property
    def version(self) -> str: ...


class ResolutionLike(Protocol):
    @property
    def mode(self) -> str: ...  # "user" | "server" | "lazy"
    @property
    def reason(self) -> str: ...
    @property
    def api_key(self) -> SecretStr | None: ...


class SpendLike(Protocol):
    @property
    def cost_usd(self) -> float: ...
    @property
    def pages(self) -> int: ...
    @property
    def input_tokens(self) -> int: ...
    @property
    def output_tokens(self) -> int: ...


class AllowanceLike(Protocol):
    @property
    def global_remaining_usd(self) -> float: ...
    @property
    def user_remaining_usd(self) -> float: ...
    @property
    def own_remaining_usd(self) -> float | None: ...
    @property
    def degraded(self) -> bool | str: ...  # False | "global" | "user" | "own"


class SpendLedgerLike(Protocol):
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
    ) -> None: ...
    def month(self, *, uid: str | None, mode: Any, at: datetime) -> SpendLike: ...
    def allowance(
        self, settings: Settings, store: GlobalStore, uid: str, at: datetime
    ) -> AllowanceLike: ...


class TranscriptionLike(Protocol):
    @property
    def text(self) -> str: ...
    @property
    def model(self) -> str: ...
    @property
    def input_tokens(self) -> int: ...
    @property
    def output_tokens(self) -> int: ...
    @property
    def stop_reason(self) -> str: ...


class OcrItemErrorLike(Protocol):
    @property
    def reason(self) -> str: ...


class BatchStatusLike(Protocol):
    @property
    def state(self) -> str: ...  # "in_progress" | "ended" | "canceling" | "expired"


class BatchItemLike(Protocol):
    @property
    def custom_id(self) -> str: ...
    @property
    def png(self) -> bytes: ...


class OcrClientLike(Protocol):
    def transcribe(self, png: bytes, prompt: Any) -> TranscriptionLike: ...
    def submit_batch(self, items: Sequence[Any], prompt: Any) -> str: ...
    def batch_status(self, batch_id: str) -> BatchStatusLike: ...
    def batch_results(self, batch_id: str) -> Iterator[tuple[str, object]]: ...


ResolveFn = Callable[[Settings, GlobalStore, SecretBox, Any, str, datetime], ResolutionLike]
"""``ocr.modes.resolve(settings, store, secrets, ledger, uid, now)``."""


def default_batch_key(settings: Settings, box: SecretBox, mode: str) -> SecretStr | None:
    """The key to collect a batch submitted in ``mode`` with, whatever ``resolve`` says now.

    Collecting results costs nothing more (the spend happened at submission), so a batch
    submitted before a cap was reached, or before the server key was withdrawn, is still
    collected and recorded. ``user``: the user's own key from the ``SecretBox``; ``server``:
    ``ANTHROPIC_API_KEY``. None when that key is gone.
    """
    if mode == "user":
        raw = box.open(CLAUDE_KEY_NAME)
        if raw is None:
            return None
        try:
            return SecretStr(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return None
    if mode == "server":
        # F adds ``anthropic_api_key`` to Settings (§3.1); read defensively until it merges.
        key = getattr(settings, "anthropic_api_key", None)
        return key if isinstance(key, SecretStr) else None
    return None


@dataclass(frozen=True)
class OcrPorts:
    """F's functions and classes as the pipeline uses them; one per process."""

    resolve: ResolveFn
    ledger: SpendLedgerLike
    client: Callable[[SecretStr], OcrClientLike]
    """``OcrClient(key, model=settings.ocr_model, max_tokens=settings.ocr_max_tokens)``."""
    prompt: Callable[[], PromptLike]
    """``ocr.prompt.load_prompt``."""
    batch_item: Callable[[str, bytes], Any]
    """``BatchItem(custom_id, png)``."""
    cost_usd: Callable[[Settings, int, int], float]
    """``ocr.ledger.cost_usd(settings, input_tokens, output_tokens)``: standard price."""
    transcription: type[Any]
    """``Transcription``: tells a batch result from an ``OcrItemError``."""
    unavailable: tuple[type[BaseException], ...]
    """``(OcrUnavailable,)``: try again later (rate limit, overload, network)."""
    refused: tuple[type[BaseException], ...]
    """``(OcrRefused,)``: a verdict (image rejected, key refused); not retried."""
    batch_key: Callable[[Settings, SecretBox, str], SecretStr | None] = default_batch_key
    """The key a batch submitted in ``mode`` is collected with."""


def build_ocr_ports(settings: Settings) -> OcrPorts | None:
    """The real ports, or None while F's modules are not wired (lazy mode everywhere).

    Integration replaces the body with the binding of F's names (see the hand-back notes):
    ``OcrPorts(resolve=modes.resolve, ledger=SpendLedger(...), client=lambda key:
    OcrClient(key, model=settings.ocr_model, max_tokens=settings.ocr_max_tokens),
    prompt=load_prompt, batch_item=BatchItem, cost_usd=cost_usd, transcription=Transcription,
    unavailable=(OcrUnavailable,), refused=(OcrRefused,))``.
    """
    del settings
    return None
