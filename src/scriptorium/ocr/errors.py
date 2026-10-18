"""OCR failures as :class:`~scriptorium.errors.ScriptoriumError` subclasses (§8, §11.0).

Messages are fixed per reason code: nothing from the API's response, the request, the key or
the page reaches them, so they are safe to log, audit and return to the key's owner.
"""

from __future__ import annotations

from typing import Final

from scriptorium.errors import ScriptoriumError

__all__ = ["KEY_REASONS", "OcrRefused", "OcrUnavailable"]

_UNAVAILABLE: Final[dict[str, str]] = {
    "rate_limited": "the Claude API is rate-limiting this key",
    "overloaded": "the Claude API is overloaded",
    "server_error": "the Claude API had an internal error",
    "timeout": "the Claude API did not answer in time",
    "connection": "the Claude API could not be reached",
    "batch_in_progress": "the batch has not finished",
    "stream_failed": "reading the batch results failed",
}

_REFUSED: Final[dict[str, str]] = {
    "bad_key": "the Claude API rejected the key",
    "forbidden": "the key is not permitted to do this",
    "billing": "the Claude account behind the key has a billing problem",
    "model_unavailable": "the OCR model is not available to this key",
    "invalid_request": "the Claude API refused the request",
    "request_too_large": "the request is larger than the Claude API accepts",
    "image_invalid": "the page image is not a PNG the Claude API accepts",
    "image_too_large": "the page image is larger than the Claude API accepts",
    "refused": "the model declined to transcribe this page",
    "unexpected_response": "the Claude API sent an answer that could not be read",
}

#: Reasons that are about the key or its account rather than the page: retrying with the
#: same key cannot help, so a job should stop until the key changes.
KEY_REASONS: Final = frozenset({"bad_key", "forbidden", "billing", "model_unavailable"})


class OcrUnavailable(ScriptoriumError):  # noqa: N818 - the name is the §3.1 contract
    """The API cannot answer now (rate limit, overload, outage, timeout): back off and retry.

    ``reason`` is a short code from a fixed set; ``retry_after_s`` is the server's hint, when
    it gave one.
    """

    code = "internal"

    def __init__(self, reason: str, *, retry_after_s: float | None = None) -> None:
        if reason not in _UNAVAILABLE:
            raise ValueError(f"unknown OcrUnavailable reason {reason!r}")
        super().__init__(_UNAVAILABLE[reason], hint="try again later")
        self.reason = reason
        self.retry_after_s = retry_after_s


class OcrRefused(ScriptoriumError):  # noqa: N818 - the name is the §3.1 contract
    """A verdict: this request (or this key) will not succeed if repeated as it is.

    ``reason`` is a short code from a fixed set. ``input_tokens`` / ``output_tokens`` are the
    tokens billed for the refused call (non-zero only when the model itself declined), so
    the caller can still record the spend.
    """

    code = "refused"

    def __init__(self, reason: str, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        if reason not in _REFUSED:
            raise ValueError(f"unknown OcrRefused reason {reason!r}")
        super().__init__(_REFUSED[reason])
        self.reason = reason
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    @property
    def about_key(self) -> bool:
        """True when the key or its account is the problem, not the page."""
        return self.reason in KEY_REASONS
