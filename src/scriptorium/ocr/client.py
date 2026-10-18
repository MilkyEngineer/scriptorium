"""The Claude API client for page transcription (Architecture §8.2).

This is the only module in Scriptorium that imports the ``anthropic`` SDK. What it sends is
fixed: the system prompt from ``ocr/prompt.md`` and one user message holding the page PNG.
No document name, path, tag or user identifier is ever part of a request, and there is no
``metadata``. The key arrives as a ``SecretStr``, is handed to the SDK and nowhere else, and
never appears in a ``repr``, a log line or an exception.

Hardening against the SDK's environment hooks: the base URL is pinned to the first-party
API (``ANTHROPIC_BASE_URL`` cannot redirect page images or the key), the key is explicit (so
no credential is read from the environment or a profile), OpenTelemetry spans are off (so
``ANTHROPIC_OPEN_TELEMETRY_*`` cannot copy page content into traces), and the ``anthropic``
logger is held at WARNING (``ANTHROPIC_LOG=debug`` would log request bodies).

Retries are ours, not the SDK's (``max_retries=0``): up to three, with backoff, on rate
limits, overload and transient server or connection errors, honouring ``retry-after`` up to
a minute. Every SDK exception is turned into :class:`OcrUnavailable` or :class:`OcrRefused`
*outside* the ``except`` block, so the SDK's exception (which holds the request, headers
included) is never chained onto ours.
"""

from __future__ import annotations

import base64
import logging
import random
import re
import struct
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol, TypeVar
from urllib.parse import urlsplit

import anthropic
from anthropic.types import Message, MessageParam, TextBlock
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages import MessageBatchIndividualResponse
from anthropic.types.messages.batch_create_params import Request as BatchRequest
from anthropic.types.messages.message_batch_result import MessageBatchResult
from pydantic import SecretStr

from scriptorium.errors import InvalidArgumentError
from scriptorium.ocr.errors import OcrRefused, OcrUnavailable
from scriptorium.ocr.prompt import Prompt

if TYPE_CHECKING:
    import httpx2

    from scriptorium.config import Settings

__all__ = [
    "API_BASE_URL",
    "MAX_BATCH_BYTES",
    "MAX_BATCH_ITEMS",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_SIDE",
    "BatchCounts",
    "BatchItem",
    "BatchState",
    "BatchStatus",
    "OcrClient",
    "OcrItemError",
    "Transcription",
    "check_png",
    "client_for",
]

API_BASE_URL: Final = "https://api.anthropic.com"
_API_HOST: Final = "api.anthropic.com"

#: The API's limits for one image: 8000 px a side, but anything above 2576 px on the long
#: side is downscaled by the API anyway (and billed for), so the caller downscales first.
MAX_IMAGE_SIDE: Final = 2576
#: 5 MB of base64 per image is the API's limit; 3.75 MiB of PNG is 5 MiB of base64.
MAX_IMAGE_BYTES: Final = 3_932_160
#: A batch holds at most 100 000 requests and 256 MB; keep well inside both.
MAX_BATCH_ITEMS: Final = 10_000
MAX_BATCH_BYTES: Final = 200 * 1024 * 1024

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_CUSTOM_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_BATCH_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MODEL_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}")

_MAX_RETRIES: Final = 3
_BACKOFF_BASE_S: Final = 2.0
_RETRY_AFTER_MAX_S: Final = 60.0
_RETRY_STATUSES: Final = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
#: Creating a batch is not idempotent: a 5xx may come after the batch exists, and a retry
#: would pay for every page twice. Only answers that guarantee nothing was created retry.
_CREATE_RETRY_STATUSES: Final = frozenset({429, 529})
_STATUS_REFUSALS: Final[dict[int, str]] = {
    400: "invalid_request",
    401: "bad_key",
    402: "billing",
    403: "forbidden",
    404: "model_unavailable",
    413: "request_too_large",
    422: "invalid_request",
}
_ITEM_ERRORS: Final[dict[str, tuple[str, bool]]] = {
    "invalid_request_error": ("invalid_request", False),
    "authentication_error": ("bad_key", False),
    "permission_error": ("forbidden", False),
    "not_found_error": ("model_unavailable", False),
    "billing_error": ("billing", False),
    "rate_limit_error": ("rate_limited", True),
    "overloaded_error": ("overloaded", True),
    "timeout_error": ("timeout", True),
    "api_error": ("server_error", True),
}

# The SDK raises its own logger to DEBUG under ANTHROPIC_LOG=debug, and below WARNING it logs
# request options (the page image and the prompt) and retries. Pinned here, whatever the root
# level is, and again by configure_logging.
for _name in ("anthropic", "httpx2"):
    logging.getLogger(_name).setLevel(max(logging.getLogger(_name).level, logging.WARNING))

_T = TypeVar("_T")
BatchState = Literal["in_progress", "ended", "canceling", "expired"]


class _Lines(Protocol):
    """The SDK's streaming JSONL decoder of batch results, as far as this module uses it."""

    def __iter__(self) -> Iterator[MessageBatchIndividualResponse]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class Transcription:
    """One page's transcript and what it cost in tokens (``output_tokens`` includes thinking).

    ``batch`` is True for a result of the Message Batches API, which bills at half price.
    """

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    batch: bool = False


@dataclass(frozen=True)
class BatchItem:
    """One page of a batch: ``custom_id`` matches ``[A-Za-z0-9_-]{1,64}``."""

    custom_id: str
    png: bytes


@dataclass(frozen=True)
class BatchCounts:
    processing: int = 0
    succeeded: int = 0
    errored: int = 0
    canceled: int = 0
    expired: int = 0


@dataclass(frozen=True)
class BatchStatus:
    """``expired``: the batch's results are gone (archived, or the batch is unknown to the key)."""

    state: BatchState
    counts: BatchCounts


@dataclass(frozen=True)
class OcrItemError:
    """One batch item that has no transcript: a reason code and whether resubmitting may help.

    Tokens are non-zero only when the model declined the page (``reason == "refused"``).
    """

    reason: str
    retryable: bool
    input_tokens: int = 0
    output_tokens: int = 0


def check_png(png: bytes) -> tuple[int, int]:
    """Width and height of ``png`` from its header; raises OcrRefused if the API would refuse it.

    Only the signature and the IHDR chunk are read: no decoding, so it is cheap and safe on
    untrusted bytes. The caller downscales to :data:`MAX_IMAGE_SIDE` before calling.
    """
    if not isinstance(png, bytes | bytearray) or len(png) < 33:
        raise OcrRefused("image_invalid")
    if bytes(png[:8]) != _PNG_SIGNATURE or bytes(png[12:16]) != b"IHDR":
        raise OcrRefused("image_invalid")
    width, height = struct.unpack(">II", bytes(png[16:24]))
    if width == 0 or height == 0:
        raise OcrRefused("image_invalid")
    if max(width, height) > MAX_IMAGE_SIDE or len(png) > MAX_IMAGE_BYTES:
        raise OcrRefused("image_too_large")
    return int(width), int(height)


def _usage_tokens(message: Message) -> tuple[int, int]:
    usage = message.usage
    # Scriptorium sets no cache_control, so the cache fields are normally zero; if the API
    # ever reports them they are input all the same.
    input_tokens = (
        usage.input_tokens
        + (usage.cache_creation_input_tokens or 0)
        + (usage.cache_read_input_tokens or 0)
    )
    return max(0, int(input_tokens)), max(0, int(usage.output_tokens))


class OcrClient:
    """Transcribes page PNGs with one Claude model, through one key.

    Blocking; call it from a worker thread. One instance per key; :meth:`close` releases the
    HTTP connections. ``http_client`` and ``sleep`` exist for tests (a mock transport and a
    sleep that does not wait).
    """

    def __init__(
        self,
        api_key: SecretStr,
        *,
        model: str,
        max_tokens: int,
        timeout_s: float = 120.0,
        http_client: httpx2.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(api_key, SecretStr):
            raise TypeError("api_key must be a SecretStr")
        if not _MODEL_ID_RE.fullmatch(model):
            raise ValueError("model is not a model id")
        if not 1 <= max_tokens <= 128_000:
            raise ValueError("max_tokens is out of range")
        self._model = model
        self._max_tokens = max_tokens
        self._sleep = sleep
        self._sdk = anthropic.Anthropic(
            api_key=api_key.get_secret_value(),
            base_url=API_BASE_URL,
            max_retries=0,
            timeout=timeout_s,
            open_telemetry=False,
            http_client=http_client,
        )

    def __repr__(self) -> str:
        return f"OcrClient(model={self._model!r})"

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._sdk.close()

    def __enter__(self) -> OcrClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---- one page --------------------------------------------------------------------

    def transcribe(self, png: bytes, prompt: Prompt) -> Transcription:
        """Transcribe one page with one Messages call (three retries with backoff).

        Raises :class:`OcrUnavailable` when the API cannot answer now and
        :class:`OcrRefused` for a verdict (bad image, bad key, the model declined); a refusal
        by the model carries the tokens it was billed for.
        """
        params = self._params(png, prompt)
        message = self._call(lambda: self._sdk.messages.create(**params))
        result = self._result(message, batch=False)
        if isinstance(result, OcrItemError):
            raise OcrRefused(
                result.reason,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        return result

    def validate_key(self) -> None:
        """The cheapest call that proves the key works for this model (no tokens billed).

        Raises ``OcrRefused("bad_key")`` for a key the API rejects, another
        :class:`OcrRefused` when the key cannot use the model, and :class:`OcrUnavailable`
        when the API cannot answer now.
        """
        self._call(lambda: self._sdk.models.retrieve(self._model))

    # ---- batches ---------------------------------------------------------------------

    def submit_batch(self, items: Sequence[BatchItem], prompt: Prompt) -> str:
        """Submit ``items`` to the Message Batches API (half price); returns the batch id.

        Each image is checked first (:func:`check_png`), so one bad page fails the call
        before anything is sent; callers check pages beforehand to leave such pages out.
        """
        if not items:
            raise InvalidArgumentError("a batch needs at least one page")
        if len(items) > MAX_BATCH_ITEMS:
            raise InvalidArgumentError(f"a batch holds at most {MAX_BATCH_ITEMS} pages")
        seen: set[str] = set()
        total = 0
        requests: list[BatchRequest] = []
        for item in items:
            if not isinstance(item.custom_id, str) or not _CUSTOM_ID_RE.fullmatch(item.custom_id):
                raise InvalidArgumentError("a batch custom_id must match [A-Za-z0-9_-]{1,64}")
            if item.custom_id in seen:
                raise InvalidArgumentError("batch custom_ids must be unique")
            seen.add(item.custom_id)
            total += len(item.png)
            if total * 4 // 3 > MAX_BATCH_BYTES:
                raise InvalidArgumentError("the batch is too large; split it")
            requests.append({"custom_id": item.custom_id, "params": self._params(item.png, prompt)})
        batch = self._call(
            lambda: self._sdk.messages.batches.create(requests=requests), idempotent=False
        )
        if not _BATCH_ID_RE.fullmatch(batch.id or ""):
            raise OcrRefused("unexpected_response")
        return batch.id

    def batch_status(self, batch_id: str) -> BatchStatus:
        """Where the batch is; ``expired`` once its results can no longer be fetched."""
        _check_batch_id(batch_id)
        try:
            batch = self._call(lambda: self._sdk.messages.batches.retrieve(batch_id))
        except OcrRefused as exc:
            if exc.reason != "model_unavailable":  # 404: the batch is unknown to this key
                raise
            return BatchStatus("expired", BatchCounts())
        rc = batch.request_counts
        counts = BatchCounts(
            processing=rc.processing,
            succeeded=rc.succeeded,
            errored=rc.errored,
            canceled=rc.canceled,
            expired=rc.expired,
        )
        if batch.archived_at is not None:
            return BatchStatus("expired", counts)
        state: BatchState = batch.processing_status
        return BatchStatus(state, counts)

    def batch_results(self, batch_id: str) -> Iterator[tuple[str, Transcription | OcrItemError]]:
        """Stream ``(custom_id, result)`` for an ended batch, one line at a time.

        Results arrive in any order. A line whose ``custom_id`` is not of the expected shape
        is skipped (results are untrusted input). Raises ``OcrUnavailable("batch_in_progress")``
        before the batch has ended; a failure part-way raises ``OcrUnavailable("stream_failed")``
        and the caller re-reads the batch later (results stay available for 29 days).
        """
        _check_batch_id(batch_id)
        batch = self._call(lambda: self._sdk.messages.batches.retrieve(batch_id))
        if batch.processing_status != "ended" or not batch.results_url:
            raise OcrUnavailable("batch_in_progress")
        url = urlsplit(batch.results_url)
        if url.scheme != "https" or url.hostname != _API_HOST:
            # The SDK sends the key with the results request: never to another host.
            raise OcrRefused("unexpected_response")
        decoder = self._call(lambda: self._sdk.messages.batches.results(batch_id))
        return self._stream(decoder)

    def _stream(self, decoder: _Lines) -> Iterator[tuple[str, Transcription | OcrItemError]]:
        try:
            iterator = iter(decoder)
            while True:
                failed = False
                try:
                    line = next(iterator)
                except StopIteration:
                    return
                except Exception:  # a broken connection or line; mapped below, unchained
                    failed = True
                if failed:
                    raise OcrUnavailable("stream_failed")
                custom_id = getattr(line, "custom_id", None)
                if not isinstance(custom_id, str) or not _CUSTOM_ID_RE.fullmatch(custom_id):
                    continue
                yield custom_id, self._item(line.result)
        finally:
            decoder.close()

    # ---- internals -------------------------------------------------------------------

    def _params(self, png: bytes, prompt: Prompt) -> MessageCreateParamsNonStreaming:
        check_png(png)
        message: MessageParam = {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(png).decode("ascii"),
                    },
                }
            ],
        }
        # Thinking is left at the model's default and effort at its lowest: Sonnet 5.5
        # refuses `thinking: disabled`, and §8.2 asks for the lowest setting the model takes.
        return {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": prompt.text,
            "messages": [message],
            "output_config": {"effort": "low"},
        }

    def _result(self, message: Message, *, batch: bool) -> Transcription | OcrItemError:
        input_tokens, output_tokens = _usage_tokens(message)
        stop = str(message.stop_reason or "unknown")
        if stop == "refusal":
            return OcrItemError("refused", False, input_tokens, output_tokens)
        # Text blocks only: thinking blocks (summaries) are not part of the transcript.
        text = "".join(block.text for block in message.content if isinstance(block, TextBlock))
        model = message.model if _MODEL_ID_RE.fullmatch(str(message.model)) else self._model
        return Transcription(
            text=text.strip(),
            model=str(model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=stop,
            batch=batch,
        )

    def _item(self, result: MessageBatchResult) -> Transcription | OcrItemError:
        kind = getattr(result, "type", None)
        if kind == "succeeded":
            message = getattr(result, "message", None)
            if not isinstance(message, Message):
                return OcrItemError("unexpected_response", True)
            return self._result(message, batch=True)
        if kind == "errored":
            error = getattr(getattr(getattr(result, "error", None), "error", None), "type", None)
            reason, retryable = _ITEM_ERRORS.get(str(error), ("server_error", True))
            return OcrItemError(reason, retryable)
        if kind == "canceled":
            return OcrItemError("canceled", True)
        if kind == "expired":
            return OcrItemError("expired", True)
        return OcrItemError("unexpected_response", True)

    def _call(self, fn: Callable[[], _T], *, idempotent: bool = True) -> _T:
        attempt = 0
        while True:
            outcome: OcrUnavailable | OcrRefused | None = None
            retry_after: float | None = None
            retryable = False
            try:
                return fn()
            except anthropic.APITimeoutError:
                outcome = OcrUnavailable("timeout")
            except anthropic.APIConnectionError:
                outcome = OcrUnavailable("connection")
                retryable = idempotent  # the request may have arrived
            except anthropic.APIStatusError as exc:
                status = exc.status_code
                retry_after = _retry_after(exc.response.headers.get("retry-after"))
                if status in _STATUS_REFUSALS:
                    outcome = OcrRefused(_STATUS_REFUSALS[status])
                else:
                    outcome = OcrUnavailable(_unavailable_reason(status), retry_after_s=retry_after)
                    retryable = status in (
                        _RETRY_STATUSES if idempotent else _CREATE_RETRY_STATUSES
                    )
            except anthropic.APIResponseValidationError:
                outcome = OcrRefused("unexpected_response")
            except anthropic.AnthropicError:
                outcome = OcrUnavailable("connection")
            # Raised here, after the handler: no SDK exception is chained onto ours.
            if not retryable or attempt >= _MAX_RETRIES:
                raise outcome
            self._sleep(_backoff(attempt, retry_after))
            attempt += 1


def _check_batch_id(batch_id: str) -> None:
    if not isinstance(batch_id, str) or not _BATCH_ID_RE.fullmatch(batch_id):
        raise InvalidArgumentError("a batch id must match [A-Za-z0-9_-]{1,128}")


def _unavailable_reason(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status == 529:
        return "overloaded"
    if status in (408, 504):
        return "timeout"
    return "server_error"


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if 0 <= seconds <= _RETRY_AFTER_MAX_S else None


def _backoff(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return retry_after
    return float(_BACKOFF_BASE_S * 2**attempt + random.uniform(0, 1))  # noqa: S311 - jitter


def client_for(settings: Settings, api_key: SecretStr) -> OcrClient:
    """An :class:`OcrClient` for ``api_key`` with the configured model and budget."""
    return OcrClient(api_key, model=settings.ocr_model, max_tokens=settings.ocr_max_tokens)
