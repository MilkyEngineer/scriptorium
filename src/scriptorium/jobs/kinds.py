"""Job kinds: keys and payloads (§6.2, §7 step 6, §8.3).

M1: ``render`` and ``page_image``. M2: ``ocr`` and ``ocr_batch`` (see "OCR jobs" below).

The server enqueues and the worker runs, so both build keys and read payloads here. A key
names the version of the work (§6.2): ``render:<acct>:<page>:<hash>`` renders the archive PNG
of one page version, ``page_image:<acct>:<page>:<hash>:<px>`` renders it for ``page_image``
at a longest side of ``px``. A deliberate re-run (the PNG cache was lost after the job
finished) appends ``:after:<previous job id>``.

Payloads hold identifiers only: the document and page UUIDs, the content hash, the
generation whose files to render, and the size. The worker re-checks every field.

A ``dead`` render job means one of two things. Its ``error`` is a **verdict** on the page
(:data:`RENDER_VERDICTS`: the renderer refused or could not parse it), and re-running it
would fail the same way; or the *run* failed (``internal``, ``lease expired``, ``local_io``,
a broken render pool, …), and the page itself may be fine. Only the first stays dead:
``page_png`` re-runs the second under a new key and the worker revives such jobs at start.

Re-runs are bounded for the life of the data, not per call: a key's **family** is its base
key and every ``:after:`` key built from it, and ``JobQueue.rerun`` creates at most
:data:`RERUN_CAP` ``:after:`` jobs per family, so a page version whose content keeps killing
render children costs a bounded number of crashes across tool calls and worker restarts.
A family at its cap is treated like a verdict. A job that finds its page version gone (the
version or the account is no longer there) completes with :data:`GONE_RESULT`: it is not
re-run for that version, and its family is not revived at worker start; another version
holding the same page re-runs it with its own payload.

**OCR jobs.** ``ocr:<acct>:<document>:<page>:<hash>`` transcribes one version of one
document's page (transcripts are per document, §6.1, so the document is in the key);
``transcribe(force=true)`` re-runs it under ``:after:<id>`` within the same
:data:`RERUN_CAP` (a re-run of a job that died of a refused key, :data:`KEY_ERROR_PREFIX`, is
not counted). ``ocr_batch:<acct>:<batch id>`` is one submitted Message Batches request:
its payload is the batch id, the mode whose key submitted it, and the items as
``{custom_id: page}``. Payloads carry UUIDs, hashes, generations and the batch id only; a
transcript never goes into a payload or a result.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal

from scriptorium.errors import InvalidArgumentError
from scriptorium.library.layout import is_sha256, is_uuid

__all__ = [
    "BATCH_ID_RE",
    "BATCH_NONCE_RE",
    "CUSTOM_ID_RE",
    "GONE_RESULT",
    "KEY_ERROR_PREFIX",
    "MAX_BATCH_ITEMS",
    "OCR",
    "OCR_BATCH",
    "OCR_KINDS",
    "PAGE_IMAGE",
    "PROMPT_VERSION_RE",
    "RENDER",
    "RENDER_KINDS",
    "RENDER_VERDICTS",
    "RERUN_CAP",
    "BatchMode",
    "OcrBatchSpec",
    "OcrSpec",
    "RenderSpec",
    "is_gone",
    "is_render_verdict",
    "ocr_batch_key",
    "ocr_key",
    "page_image_key",
    "parse_ocr_batch_payload",
    "parse_ocr_payload",
    "parse_render_payload",
    "render_key",
    "rerun_family",
    "rerun_key",
]

RENDER: Final = "render"
PAGE_IMAGE: Final = "page_image"
RENDER_KINDS: Final = (RENDER, PAGE_IMAGE)
RENDER_VERDICTS: Final = frozenset({"invalid_argument", "refused"})
"""Job errors that say the page is at fault, not the run: such a dead job stays dead."""
RERUN_CAP: Final = 3
"""Most ``:after:`` re-runs one key family ever gets (page_png and worker-start revival)."""
KEY_ERROR_PREFIX: Final = "key_"
"""Errors ``key_<reason>`` of OCR jobs that died because the API refused the *key*, not the
page. A re-run of such a job does not count against :data:`RERUN_CAP`: the refusal says
nothing about the page, and the 6-hour key hold (``ocr/pipeline.py``) already bounds how often
one can happen, since every refusal pauses the key's mode for the account."""
AFTER: Final = ":after:"
GONE_RESULT: Final[Mapping[str, str]] = MappingProxyType({"state": "gone"})
"""A render job's result when its page version (or its account) no longer exists: the job is
``done``, it is not re-run for that version, and that version's page is ``not_found``."""
MAX_SIDE_PX: Final = 8192
"""Largest ``page_image`` longest side a job may ask for."""
_MAX_GENERATION: Final = 2**62

OCR: Final = "ocr"
OCR_BATCH: Final = "ocr_batch"
OCR_KINDS: Final = (OCR, OCR_BATCH)
MAX_BATCH_ITEMS: Final = 100
"""Most pages in one ``ocr_batch``: bounds the request (images) and the payload (≤ 64 KB)."""
BATCH_NONCE_RE: Final = re.compile(r"[0-9a-f]{16}")
"""The random tag of one batch submission (``secrets.token_hex(8)``)."""
CUSTOM_ID_RE: Final = re.compile(r"(?:[0-9a-f]{16}-)?p[0-9]{1,4}")
"""A batch item's ``custom_id``: ``<nonce>-p<n>``, the submission's random tag and an index
into the job's own item list (``p<n>`` alone in a payload written before the tag). It is
built by Scriptorium, never from names, and a result is matched against the item list; the
tag lets a batch whose submission failed in flight be recognised as this one (§8.2)."""
BATCH_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
"""A Message Batches id as the API returns it (``msgbatch_…``), checked before it is stored."""
PROMPT_VERSION_RE: Final = re.compile(r"v[1-9][0-9]{0,5}")
"""A prompt version as ``ocr/prompt.md`` declares it."""
BatchMode = Literal["user", "server"]
_BATCH_MODES: Final = frozenset({"user", "server"})


@dataclass(frozen=True, slots=True)
class RenderSpec:
    """One page version to render; ``max_side`` is set for ``page_image`` jobs only."""

    document: str
    page: str
    hash: str
    generation: int
    max_side: int | None = None

    def __post_init__(self) -> None:
        if not (is_uuid(self.document) and is_uuid(self.page) and is_sha256(self.hash)):
            raise InvalidArgumentError("render job identifiers are malformed")
        if type(self.generation) is not int or not 0 <= self.generation < _MAX_GENERATION:
            raise InvalidArgumentError("render job generation is malformed")
        if self.max_side is not None and (
            type(self.max_side) is not int or not 1 <= self.max_side <= MAX_SIDE_PX
        ):
            raise InvalidArgumentError("render job size is malformed")

    @property
    def kind(self) -> str:
        return RENDER if self.max_side is None else PAGE_IMAGE

    def key(self, acct: str) -> str:
        if self.max_side is None:
            return render_key(acct, self.page, self.hash)
        return page_image_key(acct, self.page, self.hash, self.max_side)

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "document": self.document,
            "page": self.page,
            "hash": self.hash,
            "generation": self.generation,
        }
        if self.max_side is not None:
            out["max_side"] = self.max_side
        return out


def render_key(acct: str, page: str, content_hash: str) -> str:
    return f"{RENDER}:{acct}:{page}:{content_hash}"


def page_image_key(acct: str, page: str, content_hash: str, max_side: int) -> str:
    return f"{PAGE_IMAGE}:{acct}:{page}:{content_hash}:{max_side}"


def is_render_verdict(error: str | None) -> bool:
    """True when a render job's ``error`` is the renderer's verdict on the page."""
    return error in RENDER_VERDICTS


def is_gone(result: Mapping[str, Any] | None) -> bool:
    """True when a finished render job's ``result`` is :data:`GONE_RESULT`."""
    return result is not None and dict(result) == dict(GONE_RESULT)


def rerun_family(key: str) -> str:
    """The base key ``key`` was built from (``key`` itself unless it is a re-run's)."""
    return key.split(AFTER, 1)[0]


def rerun_key(key: str, previous_job: int) -> str:
    """The key of a deliberate re-run after job ``previous_job`` (whatever its key)."""
    return f"{rerun_family(key)}{AFTER}{previous_job}"


def parse_render_payload(kind: str, payload: Mapping[str, Any]) -> RenderSpec:
    """The payload of a ``render`` or ``page_image`` job; ``invalid_argument`` otherwise."""
    if kind not in RENDER_KINDS:
        raise InvalidArgumentError("not a render job")
    required = {"document", "page", "hash", "generation"}
    if not isinstance(payload, Mapping) or not required <= set(payload):
        raise InvalidArgumentError("render job payload is malformed")
    if set(payload) - required - {"max_side"}:
        raise InvalidArgumentError("render job payload is malformed")
    max_side = payload.get("max_side")
    if (kind == PAGE_IMAGE) != (max_side is not None):
        raise InvalidArgumentError("render job payload is malformed")
    return RenderSpec(  # every field is checked in __post_init__
        document=payload["document"],
        page=payload["page"],
        hash=payload["hash"],
        generation=payload["generation"],
        max_side=max_side,
    )


# ---- OCR ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OcrSpec:
    """One version of one document's page to transcribe (§7 step 6, §11.3).

    ``generation`` names the version whose files hold the page (as for renders); ``force``
    transcribes even when a transcript of this version exists (``transcribe(force=true)``).
    """

    document: str
    page: str
    hash: str
    generation: int
    force: bool = False

    def __post_init__(self) -> None:
        if not (is_uuid(self.document) and is_uuid(self.page) and is_sha256(self.hash)):
            raise InvalidArgumentError("ocr job identifiers are malformed")
        if type(self.generation) is not int or not 0 <= self.generation < _MAX_GENERATION:
            raise InvalidArgumentError("ocr job generation is malformed")
        if type(self.force) is not bool:
            raise InvalidArgumentError("ocr job flag is malformed")

    @property
    def kind(self) -> str:
        return OCR

    def key(self, acct: str) -> str:
        return ocr_key(acct, self.document, self.page, self.hash)

    def payload(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "page": self.page,
            "hash": self.hash,
            "generation": self.generation,
            "force": self.force,
        }

    def render_spec(self) -> RenderSpec:
        """The archive render of the same page version (the OCR input)."""
        return RenderSpec(self.document, self.page, self.hash, self.generation)


def ocr_key(acct: str, document: str, page: str, content_hash: str) -> str:
    return f"{OCR}:{acct}:{document}:{page}:{content_hash}"


_OCR_FIELDS: Final = frozenset({"document", "page", "hash", "generation", "force"})


def parse_ocr_payload(payload: Mapping[str, Any]) -> OcrSpec:
    """The payload of an ``ocr`` job (or one batch item); ``invalid_argument`` otherwise."""
    if not isinstance(payload, Mapping):
        raise InvalidArgumentError("ocr job payload is malformed")
    keys = set(payload)
    if not keys <= _OCR_FIELDS or not keys >= _OCR_FIELDS - {"force"}:
        raise InvalidArgumentError("ocr job payload is malformed")
    return OcrSpec(  # every field is checked in __post_init__
        document=payload["document"],
        page=payload["page"],
        hash=payload["hash"],
        generation=payload["generation"],
        force=payload.get("force", False),
    )


@dataclass(frozen=True, slots=True)
class OcrBatchSpec:
    """One submitted Message Batches request and the pages it carries.

    ``mode`` is the mode whose key submitted it: results are read with that key and the
    spend is recorded under it, even if the user's mode has changed since.
    ``prompt_version`` is the prompt the pages were sent with, stored with their transcripts
    whatever the prompt is when the results are collected (None in a payload written before
    it was recorded).
    """

    batch_id: str
    mode: BatchMode
    items: Mapping[str, OcrSpec]
    prompt_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.batch_id, str) or not BATCH_ID_RE.fullmatch(self.batch_id):
            raise InvalidArgumentError("ocr batch id is malformed")
        if self.mode not in _BATCH_MODES:
            raise InvalidArgumentError("ocr batch mode is malformed")
        if not 1 <= len(self.items) <= MAX_BATCH_ITEMS:
            raise InvalidArgumentError("ocr batch size is malformed")
        for custom_id, spec in self.items.items():
            if not isinstance(custom_id, str) or not CUSTOM_ID_RE.fullmatch(custom_id):
                raise InvalidArgumentError("ocr batch item id is malformed")
            if not isinstance(spec, OcrSpec) or spec.force:
                raise InvalidArgumentError("ocr batch item is malformed")
        if self.prompt_version is not None and (
            not isinstance(self.prompt_version, str)
            or not PROMPT_VERSION_RE.fullmatch(self.prompt_version)
        ):
            raise InvalidArgumentError("ocr batch prompt version is malformed")

    @property
    def kind(self) -> str:
        return OCR_BATCH

    def key(self, acct: str) -> str:
        return ocr_batch_key(acct, self.batch_id)

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "batch_id": self.batch_id,
            "mode": self.mode,
            "items": {cid: spec.payload() for cid, spec in sorted(self.items.items())},
        }
        if self.prompt_version is not None:
            body["prompt_version"] = self.prompt_version
        return body


def ocr_batch_key(acct: str, batch_id: str) -> str:
    return f"{OCR_BATCH}:{acct}:{batch_id}"


_BATCH_FIELDS: Final = frozenset({"batch_id", "mode", "items"})


def parse_ocr_batch_payload(payload: Mapping[str, Any]) -> OcrBatchSpec:
    """The payload of an ``ocr_batch`` job; ``invalid_argument`` otherwise."""
    if not isinstance(payload, Mapping) or set(payload) - {"prompt_version"} != _BATCH_FIELDS:
        raise InvalidArgumentError("ocr batch payload is malformed")
    items = payload["items"]
    if not isinstance(items, Mapping):
        raise InvalidArgumentError("ocr batch payload is malformed")
    return OcrBatchSpec(
        batch_id=payload["batch_id"],
        mode=payload["mode"],
        items={cid: parse_ocr_payload(item) for cid, item in items.items()},
        prompt_version=payload.get("prompt_version"),
    )
