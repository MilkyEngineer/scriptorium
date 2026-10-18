"""Message Batches for auto-OCR (Architecture §8.2, Plan 05 §3.2).

More than ``OCR_BATCH_MIN`` stable pages found by one scan go to Claude as one Message
Batches request (half price, results within 24 hours) instead of one call per page:

- :func:`plan` cuts the pages to at most :data:`~scriptorium.jobs.kinds.MAX_BATCH_ITEMS`
  items and :data:`MAX_BATCH_PNG_BYTES` of images, each under the custom id ``p<n>``.
- :func:`submit` sends them (image + prompt only, never a name or a path) and checks the
  batch id the API returns before it goes into a job key.
- The ``ocr_batch`` job then polls :func:`poll_state` with the queue's backoff
  (:data:`PENDING_REASON` is a retry, not an error) until the batch has ended or expired,
  and :func:`collect` reads the streamed results.

A batch result is untrusted input. :func:`collect` accepts only custom ids that are in the
job's own item list, each once; a result stream longer than the item list is cut off; token
counts must be sane integers; a transcript is bounded at
:data:`~scriptorium.ocr.pipeline.TRANSCRIPT_MAX_BYTES`; an item error's reason is reduced to
a short ``[a-z_]`` code. Every item named in the job ends in exactly one :class:`Outcome`
(``missing`` when the stream never mentioned it), so the caller can fall back to a single
``ocr`` job for whatever did not come back as a transcript.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Literal

from scriptorium.errors import InvalidArgumentError
from scriptorium.jobs.kinds import BATCH_ID_RE, MAX_BATCH_ITEMS, OcrSpec
from scriptorium.ocr._ports import BatchStatusLike, OcrClientLike, PromptLike, TranscriptionLike
from scriptorium.ocr.pipeline import TRANSCRIPT_MAX_BYTES

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


def custom_id(n: int) -> str:
    return f"p{n}"


@dataclass(frozen=True, slots=True)
class Planned:
    """One page for a batch: its job spec and its OCR image."""

    spec: OcrSpec
    png: bytes


def plan(pages: Sequence[Planned]) -> dict[str, Planned]:
    """The first pages that fit one batch, keyed by custom id (``p0``, ``p1``, ...)."""
    out: dict[str, Planned] = {}
    total = 0
    for page in pages:
        if len(out) >= MAX_BATCH_ITEMS or total + len(page.png) > MAX_BATCH_PNG_BYTES:
            break
        out[custom_id(len(out))] = page
        total += len(page.png)
    return out


def submit(
    client: OcrClientLike,
    prompt: PromptLike,
    planned: Mapping[str, Planned],
    batch_item: Any,
) -> str:
    """Submit ``planned`` as one batch; the batch id, checked before it is used in a key."""
    items = [batch_item(cid, page.png) for cid, page in planned.items()]
    batch_id = client.submit_batch(items, prompt)
    if not isinstance(batch_id, str) or not BATCH_ID_RE.fullmatch(batch_id):
        raise InvalidArgumentError("the batch id returned is malformed")
    return batch_id


def poll_state(status: BatchStatusLike) -> Literal["wait", "collect", "unknown"]:
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
    the item error's own code).
    """

    custom_id: str
    spec: OcrSpec
    transcription: TranscriptionLike | None
    error: str | None


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


def _outcome(cid: str, spec: OcrSpec, result: object, transcription_type: type[Any]) -> Outcome:
    if not isinstance(result, transcription_type):
        if hasattr(result, "reason"):
            return Outcome(cid, spec, None, _reason(result))
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
    transcription_type: type[Any],
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
        yield _outcome(cid, items[cid], result, transcription_type)
    for cid in sorted(items):
        if cid not in seen:
            yield Outcome(cid, items[cid], None, "missing")
