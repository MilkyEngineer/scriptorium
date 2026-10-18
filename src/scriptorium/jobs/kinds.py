"""The M1 job kinds, ``render`` and ``page_image``: keys and payloads (§6.2, §8.3).

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
A family at its cap is treated like a verdict.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from scriptorium.errors import InvalidArgumentError
from scriptorium.library.layout import is_sha256, is_uuid

__all__ = [
    "PAGE_IMAGE",
    "RENDER",
    "RENDER_KINDS",
    "RENDER_VERDICTS",
    "RERUN_CAP",
    "RenderSpec",
    "is_render_verdict",
    "page_image_key",
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
AFTER: Final = ":after:"
MAX_SIDE_PX: Final = 8192
"""Largest ``page_image`` longest side a job may ask for."""
_MAX_GENERATION: Final = 2**62


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
