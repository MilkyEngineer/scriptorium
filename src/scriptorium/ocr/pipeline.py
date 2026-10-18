"""Steps shared by the OCR worker and the OCR tools (Architecture §8, Plan 05 §3.2).

- **The auto-OCR setting** (``settings`` key ``auto_ocr``): ``{"enabled": bool, "folders":
  [uuid, ...]}``, written by ``set_auto_ocr`` and read by the worker's scan. It is read
  defensively: anything malformed reads as disabled.
- **The mode** a user's OCR runs in comes from F's ``resolve`` (§8.1); the allowance's
  ``degraded`` field is read for the mode that applies (``own`` for a user's key, ``global``
  and ``user`` for the server key).
- **Queueing a page** follows the queue's dedupe convention: the key
  ``ocr:<acct>:<document>:<page>:<hash>`` names one page version, so ten polls of an
  unchanged page find the same job; a deliberate re-run (``force``, or a job that was
  deferred because no key was available) is an ``:after:`` re-run of the latest job, at most
  :data:`~scriptorium.jobs.kinds.RERUN_CAP` per page version for all time.

Payloads carry UUIDs, hashes and generations only; transcripts never enter a job.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from scriptorium.config import Settings
from scriptorium.jobs.kinds import OcrSpec
from scriptorium.jobs.queue import JobQueue, JobRow
from scriptorium.library.index import Index
from scriptorium.library.layout import is_uuid
from scriptorium.ocr._ports import AllowanceLike, OcrPorts, ResolutionLike
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import GlobalStore

__all__ = [
    "ACTIVE_STATES",
    "AUTO_OCR_KEY",
    "AUTO_OCR_MAX_FOLDERS",
    "LAZY",
    "OCR_IMAGE_SIDE",
    "OCR_MAX_ATTEMPTS",
    "SERVER_MODES",
    "TRANSCRIPT_MAX_BYTES",
    "AutoOcr",
    "Queued",
    "blocked_reason",
    "dump_auto_ocr",
    "enqueue_page",
    "is_deferred",
    "load_auto_ocr",
    "remaining_usd",
    "resolve_mode",
    "safe_model",
]

AUTO_OCR_KEY: Final = "auto_ocr"
AUTO_OCR_MAX_FOLDERS: Final = 100
TRANSCRIPT_MAX_BYTES: Final = 30 * 1024
"""Largest transcript stored: ``read_page`` must return it whole within the 32 KB result cap."""
OCR_IMAGE_SIDE: Final = 2576
"""The Claude API's longest image side (§8.3); the archive PNG is downscaled to it."""
OCR_MAX_ATTEMPTS: Final = 5
LAZY: Final = "lazy"
SERVER_MODES: Final = frozenset({"user", "server"})
ACTIVE_STATES: Final = frozenset({"queued", "leased", "failed"})
MODEL_MAX: Final = 100


# ---- the auto-OCR setting ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AutoOcr:
    enabled: bool = False
    folders: tuple[str, ...] = ()


def load_auto_ocr(index: Index) -> AutoOcr:
    """The account's auto-OCR setting; disabled when unset or malformed."""
    raw = index.get_setting(AUTO_OCR_KEY)
    if raw is None:
        return AutoOcr()
    try:
        value = json.loads(raw)
    except ValueError:
        return AutoOcr()
    if not isinstance(value, dict):
        return AutoOcr()
    enabled = value.get("enabled")
    folders = value.get("folders")
    if type(enabled) is not bool or not isinstance(folders, list):
        return AutoOcr()
    clean = sorted({f for f in folders if isinstance(f, str) and is_uuid(f)})
    return AutoOcr(enabled, tuple(clean[:AUTO_OCR_MAX_FOLDERS]))


def dump_auto_ocr(setting: AutoOcr) -> str:
    return json.dumps(
        {"enabled": setting.enabled, "folders": list(setting.folders)}, separators=(",", ":")
    )


# ---- the mode and the caps --------------------------------------------------------------------


def resolve_mode(
    ports: OcrPorts, settings: Settings, store: GlobalStore, uid: str, now: datetime
) -> ResolutionLike:
    """F's ``resolve`` for ``uid``; the caller drops the key as soon as it has used it."""
    return ports.resolve(settings, store, SecretBox(settings, uid), ports.ledger, uid, now)


def blocked_reason(allowance: AllowanceLike, mode: str) -> str | None:
    """The cap that stops ``mode`` now (a reason code), or None when there is room."""
    degraded = allowance.degraded
    if degraded is False or not isinstance(degraded, str):
        return None
    if mode == "user" and degraded == "own":
        return "user_cap_reached"
    if mode == "server" and degraded in ("global", "user"):
        return f"cap_reached_{degraded}"
    return None


def remaining_usd(allowance: AllowanceLike, mode: str) -> float | None:
    """What ``mode`` may still spend this month; None when no cap applies."""
    if mode == "user":
        own = allowance.own_remaining_usd
        return None if own is None else max(0.0, own)
    if mode == "server":
        return max(0.0, min(allowance.global_remaining_usd, allowance.user_remaining_usd))
    return 0.0


def affordable_pages(remaining: float | None, per_page_usd: float, wanted: int) -> int:
    """How many of ``wanted`` pages fit in ``remaining`` at ``per_page_usd`` each."""
    if remaining is None or per_page_usd <= 0:
        return wanted
    return max(0, min(wanted, math.floor(remaining / per_page_usd)))


def safe_model(model: object) -> str:
    """A model name fit to store: printable, bounded; ``unknown`` otherwise (untrusted)."""
    if isinstance(model, str) and 0 < len(model) <= MODEL_MAX and model.isprintable():
        return model
    return "unknown"


# ---- queueing ---------------------------------------------------------------------------------


Queued = Literal["queued", "rerun", "active", "finished", "capped"]


def is_deferred(job: JobRow) -> bool:
    """A finished ``ocr`` job that did nothing because no key or no room was available."""
    result: Mapping[str, Any] | None = job.result
    return job.state == "done" and result is not None and result.get("state") == "deferred"


def enqueue_page(
    queue: JobQueue, *, uid: str, acct: str, spec: OcrSpec, retry_failed: bool
) -> tuple[JobRow | None, Queued]:
    """Queue the ``ocr`` job for one page version, or find the one that exists.

    - No job for the key yet: a new one (``queued``).
    - The latest job of the family is queued, leased or waiting to retry: that job
      (``active``).
    - The latest job finished: re-run it (``rerun``) when ``spec.force``, when it was
      deferred, or when it died or was cancelled and ``retry_failed``; otherwise it is the
      answer (``finished``). A family at its re-run cap gives ``(None, "capped")``.
    """
    key = spec.key(acct)
    family = queue.family(uid, key)
    if not family:
        job = queue.enqueue(
            uid=uid,
            acct=acct,
            kind=spec.kind,
            dedupe_key=key,
            payload=spec.payload(),
            max_attempts=OCR_MAX_ATTEMPTS,
        )
        return job, "queued"
    latest = family[-1]
    if latest.state in ACTIVE_STATES:
        return latest, "active"
    again = (
        spec.force
        or is_deferred(latest)
        or (retry_failed and latest.state in ("dead", "cancelled"))
    )
    if not again:
        return latest, "finished"
    nxt = queue.rerun(latest, payload=spec.payload())
    if nxt is None:
        return None, "capped"
    return nxt, "rerun"
