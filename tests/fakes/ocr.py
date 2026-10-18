"""A stand-in for ``scriptorium.ocr.client.OcrClient`` (M2 breakdown §3.1).

``FakeOcrClient`` has the real client's public methods and signatures, records every call
(the PNG's byte count, never its bytes), returns canned :class:`Transcription` results and
batch results, and raises scripted errors. ``FakeOcrFactory`` replaces
``scriptorium.ocr.client.client_for`` (``Services.ocr_client``) and records which key each
client was built with, so a test can tell the user's key from the server's.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import SecretStr

from scriptorium.config import Settings
from scriptorium.ocr.client import (
    BatchCounts,
    BatchItem,
    BatchStatus,
    OcrItemError,
    Transcription,
    check_png,
)
from scriptorium.ocr.errors import OcrUnavailable
from scriptorium.ocr.prompt import Prompt

__all__ = ["FakeBatch", "FakeOcrClient", "FakeOcrFactory", "canned"]

DEFAULT_MODEL = "claude-sonnet-5-5"


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


@dataclass
class FakeBatch:
    custom_ids: list[str]
    prompt_version: str
    state: str = "in_progress"
    results: dict[str, Transcription | OcrItemError] = field(default_factory=dict)


class FakeOcrClient:
    """Records calls and answers from a script; never touches the network.

    ``transcriptions`` are returned (or, if exceptions, raised) in order by
    :meth:`transcribe`; when they run out every call returns :func:`canned` text.
    ``key_error`` is raised by :meth:`validate_key`. ``check_images`` applies the real
    client's PNG header check, so a test notices a page that was not downscaled.
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
    ) -> None:
        self.api_key = api_key
        self._model = model
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.transcriptions: deque[Transcription | Exception] = deque(transcriptions)
        self.key_error = key_error
        self.check_images = check_images
        self.calls: list[tuple[str, ...]] = []
        self.batches: dict[str, FakeBatch] = {}
        self.closed = False

    def __repr__(self) -> str:
        return f"FakeOcrClient(model={self._model!r})"

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeOcrClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def transcribe(self, png: bytes, prompt: Prompt) -> Transcription:
        self.calls.append(("transcribe", str(len(png)), prompt.version))
        if self.check_images:
            check_png(png)
        if self.transcriptions:
            nxt = self.transcriptions.popleft()
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return canned(model=self._model)

    def validate_key(self) -> None:
        self.calls.append(("validate_key",))
        if self.key_error is not None:
            raise self.key_error

    def submit_batch(self, items: Sequence[BatchItem], prompt: Prompt) -> str:
        self.calls.append(("submit_batch", str(len(items)), prompt.version))
        if self.check_images:
            for item in items:
                check_png(item.png)
        batch_id = f"msgbatch_fake_{len(self.batches) + 1:04d}"
        self.batches[batch_id] = FakeBatch([i.custom_id for i in items], prompt.version)
        return batch_id

    def finish_batch(
        self,
        batch_id: str,
        results: Mapping[str, Transcription | OcrItemError] | None = None,
        *,
        state: str = "ended",
    ) -> None:
        """End a batch with ``results`` (default: a canned batch transcript per page)."""
        batch = self.batches[batch_id]
        batch.state = state
        if results is None:
            results = {cid: canned(f"Page {cid}", batch=True) for cid in batch.custom_ids}
        batch.results = dict(results)

    def batch_status(self, batch_id: str) -> BatchStatus:
        self.calls.append(("batch_status", batch_id))
        batch = self.batches.get(batch_id)
        if batch is None:
            return BatchStatus("expired", BatchCounts())
        if batch.state != "ended":
            return BatchStatus(batch.state, BatchCounts(processing=len(batch.custom_ids)))  # type: ignore[arg-type]
        ok = sum(isinstance(r, Transcription) for r in batch.results.values())
        return BatchStatus("ended", BatchCounts(succeeded=ok, errored=len(batch.results) - ok))

    def batch_results(self, batch_id: str) -> Iterator[tuple[str, Transcription | OcrItemError]]:
        self.calls.append(("batch_results", batch_id))
        batch = self.batches.get(batch_id)
        if batch is None or batch.state != "ended":
            raise OcrUnavailable("batch_in_progress")
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
