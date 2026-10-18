"""Message Batches for auto-OCR (Architecture §8.2, Plan 05 §3.2).

More than ``OCR_BATCH_MIN`` stable pages found by one scan go to Claude as one Message
Batches request (half price, results within 24 hours) instead of one call per page:

- :func:`plan` cuts the pages to at most :data:`~scriptorium.jobs.kinds.MAX_BATCH_ITEMS`
  items and :data:`MAX_BATCH_PNG_BYTES` of images, each under the custom id
  ``<nonce>-p<n>``: the nonce is fresh per submission, so a batch the API created for a
  submission that failed in flight can be told apart from any other batch of the key.
- :func:`submit` sends them (image + prompt only, never a name or a path) and checks the
  batch id the API returns before it goes into a job key.
- The ``ocr_batch`` job then polls :func:`poll_state` with the queue's backoff
  (:data:`PENDING_REASON` is a retry, not an error) until the batch has ended or expired,
  and :func:`collect` reads the streamed results.

A batch result is untrusted input. :func:`collect` accepts only custom ids that are in the
job's own item list, each once; a result stream longer than the item list is cut off; token
counts must be sane integers; a transcript is bounded at
:data:`~scriptorium.ocr.pipeline.TRANSCRIPT_MAX_BYTES`; an item error's reason is reduced to
a short ``[a-z_]`` code and keeps F's ``retryable`` flag and the tokens it was billed for.
Every item named in the job ends in exactly one :class:`Outcome` (``missing`` when the stream
never mentioned it), so the caller can fall back to a single ``ocr`` job for whatever did not
come back as a transcript and may succeed on another try.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal

from scriptorium.errors import InvalidArgumentError
from scriptorium.jobs.kinds import BATCH_ID_RE, BATCH_NONCE_RE, MAX_BATCH_ITEMS, OcrSpec
from scriptorium.ocr._ports import OcrClientLike
from scriptorium.ocr.client import BatchItem, BatchStatus, Transcription, check_png
from scriptorium.ocr.errors import OcrRefused
from scriptorium.ocr.pipeline import TRANSCRIPT_MAX_BYTES
from scriptorium.ocr.prompt import Prompt

__all__ = [
    "BATCH_FIRST_POLL",
    "BATCH_MAX_ATTEMPTS",
    "MAX_BATCH_PNG_BYTES",
    "PENDING_REASON",
    "Outcome",
    "Planned",
    "Tally",
    "collect",
    "custom_id",
    "fits",
    "judge",
    "nonce_of",
    "plan",
    "poll_state",
    "submit",
]

MAX_BATCH_PNG_BYTES: Final = 64 * 1024 * 1024
"""Raw image bytes in one batch (base64 makes it ~86 MB, well under the API's request cap)."""
BATCH_MAX_ATTEMPTS: Final = 40
"""Polls of one batch: the queue's backoff reaches an hour after seven, so 40 cover a day."""
BATCH_FIRST_POLL: Final = timedelta(minutes=1)
PENDING_REASON: Final = "batch_pending"
"""The ``ocr_batch`` job's retry reason while the batch is still running (not an error)."""
MAX_TOKENS: Final = 10_000_000
"""A token count above this in a result is not believed."""
EXTRA_RESULTS: Final = 100
"""Results read beyond the item count before the stream is abandoned as hostile."""
_REASON: Final = re.compile(r"[a-z_]{1,40}")

WAIT_STATES: Final = frozenset({"in_progress", "canceling"})
DONE_STATES: Final = frozenset({"ended", "expired"})


def custom_id(n: int, nonce: str | None = None) -> str:
    """``<nonce>-p<n>`` (``p<n>`` without a nonce, as single calls and old payloads use)."""
    if nonce is None:
        return f"p{n}"
    if not isinstance(nonce, str) or not BATCH_NONCE_RE.fullmatch(nonce):
        raise InvalidArgumentError("a batch nonce must be 16 hex digits")
    return f"{nonce}-p{n}"


def nonce_of(cid: object) -> str | None:
    """The nonce a custom id carries, if any (results are untrusted: anything else is None)."""
    if not isinstance(cid, str):
        return None
    head, sep, _tail = cid.partition("-")
    return head if sep and BATCH_NONCE_RE.fullmatch(head) else None


def fits(png: bytes) -> str | None:
    """None when the API takes ``png`` as it is, else F's refusal code (``check_png``)."""
    try:
        check_png(png)
    except OcrRefused as exc:
        return exc.reason
    return None


@dataclass(frozen=True, slots=True)
class Planned:
    """One page for a batch: its job spec and its OCR image."""

    spec: OcrSpec
    png: bytes


def plan(pages: Sequence[Planned], nonce: str | None = None) -> dict[str, Planned]:
    """The first pages that fit one batch, keyed by custom id (``<nonce>-p0``, ...)."""
    out: dict[str, Planned] = {}
    total = 0
    for page in pages:
        if len(out) >= MAX_BATCH_ITEMS or total + len(page.png) > MAX_BATCH_PNG_BYTES:
            break
        out[custom_id(len(out), nonce)] = page
        total += len(page.png)
    return out


def submit(client: OcrClientLike, prompt: Prompt, planned: Mapping[str, Planned]) -> str:
    """Submit ``planned`` as one batch; the batch id, checked before it is used in a key."""
    items = [BatchItem(cid, page.png) for cid, page in planned.items()]
    batch_id = client.submit_batch(items, prompt)
    if not isinstance(batch_id, str) or not BATCH_ID_RE.fullmatch(batch_id):
        raise InvalidArgumentError("the batch id returned is malformed")
    return batch_id


def poll_state(status: BatchStatus) -> Literal["wait", "collect", "unknown"]:
    """``collect`` once the batch has ended or expired, ``wait`` while it runs."""
    state = getattr(status, "state", None)
    if state in WAIT_STATES:
        return "wait"
    if state in DONE_STATES:
        return "collect"
    return "unknown"


@dataclass(frozen=True, slots=True)
class Outcome:
    """One item's result.

    ``transcription`` is set whenever the API billed the item (its token counts are sane),
    including a transcript too large to store; ``error`` is None when ``transcription``
    can be stored, otherwise a reason code (``too_large``, ``missing``, ``bad_result``, or
    the item error's own code). For an item error, ``retryable`` is F's verdict (whether
    sending the page again may help) and ``billed`` the tokens it cost (a model refusal).
    """

    custom_id: str
    spec: OcrSpec
    transcription: Transcription | None
    error: str | None
    retryable: bool = True
    billed: tuple[int, int] = (0, 0)


@dataclass
class Tally:
    """What :func:`collect` ignored: ids not in the job, repeated ids, a cut-off stream."""

    unexpected: int = 0
    duplicates: int = 0
    truncated: bool = False


def _sane_count(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_TOKENS


def _reason(result: object) -> str:
    reason = getattr(result, "reason", None)
    if isinstance(reason, str) and _REASON.fullmatch(reason):
        return reason
    return "item_error"


def judge(cid: str, spec: OcrSpec, result: object) -> Outcome:
    """The :class:`Outcome` of one result (a batch item's, or a single call's)."""
    if not isinstance(result, Transcription):
        if hasattr(result, "reason"):
            retryable = getattr(result, "retryable", True)
            tokens_in = getattr(result, "input_tokens", 0)
            tokens_out = getattr(result, "output_tokens", 0)
            billed = (
                (tokens_in, tokens_out)
                if _sane_count(tokens_in) and _sane_count(tokens_out)
                else (0, 0)
            )
            return Outcome(cid, spec, None, _reason(result), retryable is not False, billed)
        return Outcome(cid, spec, None, "bad_result")
    tokens_in = getattr(result, "input_tokens", None)
    tokens_out = getattr(result, "output_tokens", None)
    if not (_sane_count(tokens_in) and _sane_count(tokens_out)):
        return Outcome(cid, spec, None, "bad_result")
    text = getattr(result, "text", None)
    if not isinstance(text, str):
        return Outcome(cid, spec, result, "bad_result")
    if len(text.encode("utf-8", "replace")) > TRANSCRIPT_MAX_BYTES:
        return Outcome(cid, spec, result, "too_large")
    return Outcome(cid, spec, result, None)


def collect(
    results: Iterable[object],
    items: Mapping[str, OcrSpec],
    *,
    tally: Tally,
) -> Iterator[Outcome]:
    """One :class:`Outcome` per item of ``items``, as the stream delivers them.

    Ids not in ``items`` and repeats of an id already seen are counted in ``tally`` and
    skipped; after ``len(items) + EXTRA_RESULTS`` results the stream is abandoned
    (``tally.truncated``). Items the stream never mentioned come last, as ``missing``.
    """
    seen: set[str] = set()
    limit = len(items) + EXTRA_RESULTS
    read = 0
    for entry in results:
        read += 1
        if read > limit:
            tally.truncated = True
            break
        if not isinstance(entry, tuple) or len(entry) != 2:
            tally.unexpected += 1
            continue
        cid, result = entry
        if not isinstance(cid, str) or cid not in items:
            tally.unexpected += 1
            continue
        if cid in seen:
            tally.duplicates += 1
            continue
        seen.add(cid)
        yield judge(cid, items[cid], result)
    for cid in sorted(items):
        if cid not in seen:
            yield Outcome(cid, items[cid], None, "missing")
