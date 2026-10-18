"""Steps shared by the OCR worker and the OCR tools (Architecture §8, Plan 05 §3.2).

- **The auto-OCR setting** (``settings`` key ``auto_ocr``): ``{"enabled": bool, "folders":
  [uuid, ...]}``, written by ``set_auto_ocr`` and read by the worker's scan. It is read
  defensively: anything malformed reads as disabled.
- **The mode** a user's OCR runs in comes from F's ``resolve`` (§8.1), with a stored key that
  cannot be decrypted read as ``lazy`` / ``key_unreadable`` (never the server key); the
  allowance is asked for the mode that applies (``own`` for a user's key, ``global`` and
  ``user`` for the server key).
- **A refused key** (``OcrRefused.about_key``: ``bad_key``, ``forbidden``, ``billing``,
  ``model_unavailable``) is noted in the account's ``settings`` under ``ocr_key_refused``
  (mode, reason code, time; never the key). While the note applies to the mode ``resolve``
  picks, the worker sends nothing with that key: jobs are deferred, the scan queues
  nothing and ``read_page`` answers as in lazy mode, for :data:`KEY_REFUSED_HOLD` or until
  the user stores or clears their key. The job that met the refusal dies with
  ``key_<reason>``; ``ocr_status`` shows the note.
- **Pending batches** (``settings`` key ``ocr_pending_batches``): a batch's payload is
  written there as soon as the API has accepted it and removed once its ``ocr_batch`` job is
  queued, so a batch whose job could not be queued is adopted by the next scan instead of
  being paid for and never collected.
- **Pending submissions** (``settings`` key ``ocr_pending_submissions``): written *before*
  a batch is submitted (its random nonce, time, mode, items and prompt version; no batch id
  yet) and replaced by a pending batch once the API answers with the id. A submission whose
  answer was lost (a timeout, a dropped connection, a 5xx: the batch may exist) stays, and
  the scan looks for it in the key's batch listing: a batch created around that time with
  as many requests, whose results carry the nonce, is adopted; none after a grace period
  means it was never created; an entry that cannot be settled is dropped after a day and
  logged as ``ocr_batch_lost``.
- **Queueing a page** follows the queue's dedupe convention: the key
  ``ocr:<acct>:<document>:<page>:<hash>`` names one page version, so ten polls of an
  unchanged page find the same job. A job that cannot run now (no key, a cap reached, a
  refused key) is *deferred* (``JobQueue.defer``): it stays queued under its key until the
  situation changes, so waiting never mints a re-run. A deliberate re-run (``force``, a dead
  job ``transcribe`` retries, or a job that died of a refused key, which the scan retries
  once the key's hold is over) is an ``:after:`` re-run of the latest job, at most
  :data:`~scriptorium.jobs.kinds.RERUN_CAP` per page version for all time; re-runs of
  ``key_*`` deaths are not counted, since the refusal was not about the page.

Payloads carry UUIDs, hashes and generations only; transcripts never enter a job.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from pydantic import SecretStr

from scriptorium.config import Settings
from scriptorium.db.connect import from_db_time, to_db_time
from scriptorium.errors import ScriptoriumError
from scriptorium.jobs.kinds import (
    BATCH_NONCE_RE,
    KEY_ERROR_PREFIX,
    PROMPT_VERSION_RE,
    BatchMode,
    OcrBatchSpec,
    OcrSpec,
    parse_ocr_batch_payload,
    parse_ocr_payload,
)
from scriptorium.jobs.queue import JobQueue, JobRow, is_deferred
from scriptorium.library.index import Index
from scriptorium.library.layout import is_uuid
from scriptorium.ocr._ports import OcrPorts
from scriptorium.ocr.errors import KEY_REASONS
from scriptorium.ocr.ledger import Allowance
from scriptorium.ocr.modes import Mode
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import GlobalStore

__all__ = [
    "ACTIVE_STATES",
    "AUTO_OCR_KEY",
    "AUTO_OCR_MAX_FOLDERS",
    "KEY_ERROR_PREFIX",
    "KEY_REFUSED_HOLD",
    "KEY_REFUSED_KEY",
    "KEY_UNREADABLE",
    "LAZY",
    "OCR_IMAGE_SIDE",
    "OCR_MAX_ATTEMPTS",
    "PENDING_BATCHES_KEY",
    "PENDING_SUBMISSIONS_KEY",
    "SERVER_MODES",
    "TRANSCRIPT_MAX_BYTES",
    "AutoOcr",
    "KeyRefusal",
    "ModeChoice",
    "PendingSubmission",
    "Queued",
    "add_pending_batch",
    "add_pending_submission",
    "blocked_reason",
    "clear_key_refusal",
    "drop_pending_batch",
    "drop_pending_submission",
    "dump_auto_ocr",
    "enqueue_page",
    "is_deferred",
    "is_key_failure",
    "key_error",
    "key_mode",
    "load_auto_ocr",
    "load_key_refusal",
    "load_pending_batches",
    "load_pending_submissions",
    "note_key_refusal",
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
KEY_UNREADABLE: Final = "key_unreadable"
"""The reason a user whose stored key cannot be decrypted is in lazy mode (§8.1)."""
KEY_REFUSED_KEY: Final = "ocr_key_refused"
KEY_REFUSED_HOLD: Final = timedelta(hours=6)
"""How long a refused key is left alone before one job tries it again."""
PENDING_BATCHES_KEY: Final = "ocr_pending_batches"
MAX_PENDING_BATCHES: Final = 10
PENDING_SUBMISSIONS_KEY: Final = "ocr_pending_submissions"
MAX_PENDING_SUBMISSIONS: Final = 10
_MODEL_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}")
"""A model id as ``SpendLedger.record`` accepts it."""


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


@dataclass(frozen=True)
class ModeChoice:
    """F's ``Resolution``, plus ``key_unreadable``; ``api_key`` is set for ``user``/``server``."""

    mode: Mode
    reason: str
    api_key: SecretStr | None = field(default=None, repr=False, compare=False)


def resolve_mode(
    ports: OcrPorts, settings: Settings, store: GlobalStore, uid: str, now: datetime
) -> ModeChoice:
    """F's ``resolve`` for ``uid``; the caller drops the key as soon as it has used it.

    A stored key that cannot be decrypted is ``lazy`` / ``key_unreadable``: ``resolve`` fails
    closed rather than spend the server key, and so does every caller of this function. A
    key mode without a key (it cannot happen with F's ``resolve``) is lazy too.
    """
    try:
        found = ports.resolve(settings, store, SecretBox(settings, uid), ports.ledger, uid, now)
    except ScriptoriumError as exc:
        if exc.code != "internal":
            raise
        return ModeChoice(LAZY, KEY_UNREADABLE)
    if found.mode not in SERVER_MODES or found.api_key is None:
        return ModeChoice(LAZY, str(found.reason))
    return ModeChoice(found.mode, str(found.reason), found.api_key)


def key_mode(mode: str) -> BatchMode | None:
    """``mode`` when a key is spent in it (``user`` or ``server``), else None."""
    if mode == "user":
        return "user"
    if mode == "server":
        return "server"
    return None


def blocked_reason(allowance: Allowance, mode: str) -> str | None:
    """The cap that stops ``mode`` now (a reason code), or None when there is room."""
    degraded = allowance.degraded
    if degraded is False or not isinstance(degraded, str):
        return None
    if mode == "user" and degraded == "own":
        return "user_cap_reached"
    if mode == "server" and degraded in ("global", "user"):
        return f"cap_reached_{degraded}"
    return None


def remaining_usd(allowance: Allowance, mode: str) -> float | None:
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
    """A model id fit to store and record (the ledger's pattern); ``unknown`` otherwise."""
    if isinstance(model, str) and _MODEL_RE.fullmatch(model):
        return model
    return "unknown"


# ---- a refused key ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeyRefusal:
    """The account's note that the key of ``mode`` was refused (``reason`` in KEY_REASONS)."""

    mode: BatchMode
    reason: str
    at: datetime

    def applies(self, mode: str, now: datetime) -> bool:
        """Whether the worker should leave ``mode``'s key alone at ``now``."""
        return self.mode == mode and now - self.at < KEY_REFUSED_HOLD


def key_error(reason: str) -> str:
    """The job error for a refused key: ``key_bad_key``, ``key_billing``, ..."""
    return KEY_ERROR_PREFIX + reason


def load_key_refusal(index: Index) -> KeyRefusal | None:
    """The account's refused-key note; None when unset or malformed."""
    raw = index.get_setting(KEY_REFUSED_KEY)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        mode, reason, at = value["mode"], value["reason"], from_db_time(value["at"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if mode not in SERVER_MODES or reason not in KEY_REASONS:
        return None
    return KeyRefusal(mode, reason, at)


def note_key_refusal(index: Index, mode: str, reason: str, at: datetime) -> None:
    """Record that ``mode``'s key was refused for ``reason`` (a code; never the key)."""
    if mode not in SERVER_MODES or reason not in KEY_REASONS:
        return
    body = {"mode": mode, "reason": reason, "at": to_db_time(at)}
    index.set_setting(KEY_REFUSED_KEY, json.dumps(body, separators=(",", ":")))


def clear_key_refusal(index: Index, mode: str | None = None) -> None:
    """Forget the note (only if it is about ``mode``, when given)."""
    if index.get_setting(KEY_REFUSED_KEY) is None:
        return
    note = load_key_refusal(index)  # a malformed note is dropped whatever ``mode`` is
    if mode is None or note is None or note.mode == mode:
        index.set_setting(KEY_REFUSED_KEY, None)


def is_key_failure(job: JobRow) -> bool:
    """A job that died because the key was refused (``key_<reason>``): the scan re-runs it
    once the key's hold is over, ``transcribe`` on request; ``read_page`` never does."""
    return job.state == "dead" and (job.error or "").startswith(KEY_ERROR_PREFIX)


# ---- pending batches --------------------------------------------------------------------------


def load_pending_batches(index: Index) -> list[OcrBatchSpec]:
    """Batches submitted whose ``ocr_batch`` job may not exist; malformed entries are dropped."""
    raw = index.get_setting(PENDING_BATCHES_KEY)
    if raw is None:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    out: list[OcrBatchSpec] = []
    for entry in value[:MAX_PENDING_BATCHES]:
        try:
            out.append(parse_ocr_batch_payload(entry))
        except ScriptoriumError:
            continue
    return out


def _store_pending(index: Index, batches: list[OcrBatchSpec]) -> None:
    if not batches:
        index.set_setting(PENDING_BATCHES_KEY, None)
        return
    body = [b.payload() for b in batches[-MAX_PENDING_BATCHES:]]
    index.set_setting(PENDING_BATCHES_KEY, json.dumps(body, separators=(",", ":")))


def add_pending_batch(index: Index, batch: OcrBatchSpec) -> None:
    """Remember ``batch`` until its job is queued (the scan is the only writer)."""
    kept = [b for b in load_pending_batches(index) if b.batch_id != batch.batch_id]
    _store_pending(index, [*kept, batch])


def drop_pending_batch(index: Index, batch_id: str) -> None:
    pending = load_pending_batches(index)
    kept = [b for b in pending if b.batch_id != batch_id]
    if len(kept) != len(pending) or (
        not kept and index.get_setting(PENDING_BATCHES_KEY) is not None
    ):
        _store_pending(index, kept)


@dataclass(frozen=True, slots=True)
class PendingSubmission:
    """A batch being submitted, or whose submission failed after the request may have
    reached the API: everything its ``ocr_batch`` job needs except the batch id.

    Every item's custom id starts with ``<nonce>-``; ``at`` is when it was written (just
    before the request), so the batch, if it exists, was created around then.
    """

    nonce: str
    at: datetime
    mode: BatchMode
    items: Mapping[str, OcrSpec]
    prompt_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.nonce, str) or not BATCH_NONCE_RE.fullmatch(self.nonce):
            raise ScriptoriumError("pending submission nonce is malformed")
        if not isinstance(self.at, datetime) or self.at.tzinfo is None:
            raise ScriptoriumError("pending submission time is malformed")
        prefix = self.nonce + "-"
        if any(not isinstance(cid, str) or not cid.startswith(prefix) for cid in self.items):
            raise ScriptoriumError("pending submission item id is malformed")
        if self.prompt_version is not None and (
            not isinstance(self.prompt_version, str)
            or not PROMPT_VERSION_RE.fullmatch(self.prompt_version)
        ):
            raise ScriptoriumError("pending submission prompt version is malformed")
        self.spec("pending_" + self.nonce)  # mode, items and their ids, as a job checks them

    def spec(self, batch_id: str) -> OcrBatchSpec:
        """The ``ocr_batch`` job of this submission once its batch id is known."""
        return OcrBatchSpec(batch_id, self.mode, self.items, prompt_version=self.prompt_version)

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "nonce": self.nonce,
            "at": to_db_time(self.at),
            "mode": self.mode,
            "items": {cid: spec.payload() for cid, spec in sorted(self.items.items())},
        }
        if self.prompt_version is not None:
            body["prompt_version"] = self.prompt_version
        return body


_SUBMISSION_FIELDS: Final = frozenset({"nonce", "at", "mode", "items"})


def _parse_submission(entry: object) -> PendingSubmission:
    if not isinstance(entry, Mapping) or set(entry) - {"prompt_version"} != _SUBMISSION_FIELDS:
        raise ScriptoriumError("pending submission is malformed")
    items = entry["items"]
    at = entry["at"]
    if not isinstance(items, Mapping) or not isinstance(at, str):
        raise ScriptoriumError("pending submission is malformed")
    try:
        when = from_db_time(at)
    except ValueError:
        raise ScriptoriumError("pending submission time is malformed") from None
    return PendingSubmission(
        nonce=entry["nonce"],
        at=when,
        mode=entry["mode"],
        items={str(cid): parse_ocr_payload(item) for cid, item in items.items()},
        prompt_version=entry.get("prompt_version"),
    )


def load_pending_submissions(index: Index) -> list[PendingSubmission]:
    """Submissions not yet settled (oldest first); malformed entries are dropped."""
    raw = index.get_setting(PENDING_SUBMISSIONS_KEY)
    if raw is None:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    out: list[PendingSubmission] = []
    for entry in value[:MAX_PENDING_SUBMISSIONS]:
        try:
            out.append(_parse_submission(entry))
        except ScriptoriumError:
            continue
    return out


def _store_submissions(index: Index, entries: list[PendingSubmission]) -> None:
    if not entries:
        index.set_setting(PENDING_SUBMISSIONS_KEY, None)
        return
    body = [e.payload() for e in entries[-MAX_PENDING_SUBMISSIONS:]]
    index.set_setting(PENDING_SUBMISSIONS_KEY, json.dumps(body, separators=(",", ":")))


def add_pending_submission(index: Index, entry: PendingSubmission) -> None:
    """Remember ``entry`` before its batch is submitted (the scan is the only writer)."""
    kept = [e for e in load_pending_submissions(index) if e.nonce != entry.nonce]
    _store_submissions(index, [*kept, entry])


def drop_pending_submission(index: Index, nonce: str) -> None:
    pending = load_pending_submissions(index)
    kept = [e for e in pending if e.nonce != nonce]
    if len(kept) != len(pending) or (
        not kept and index.get_setting(PENDING_SUBMISSIONS_KEY) is not None
    ):
        _store_submissions(index, kept)


# ---- queueing ---------------------------------------------------------------------------------


Queued = Literal["queued", "rerun", "active", "finished", "capped"]


def enqueue_page(
    queue: JobQueue,
    *,
    uid: str,
    acct: str,
    spec: OcrSpec,
    retry_failed: bool,
    retry_key_failure: bool = False,
    wake: bool = False,
) -> tuple[JobRow | None, Queued]:
    """Queue the ``ocr`` job for one page version, or find the one that exists.

    - No job for the key yet: a new one (``queued``).
    - The latest job of the family is queued (deferred or not), leased or waiting to retry:
      that job (``active``); a deferred one is made runnable now when ``wake`` (the caller
      has just checked that a key with room is available).
    - The latest job finished: re-run it (``rerun``) when ``spec.force``, when it died or was
      cancelled and ``retry_failed`` (``transcribe``), or when it died of a refused key and
      ``retry_key_failure`` (the scan, which runs only while no refusal applies); otherwise
      it is the answer (``finished``). A family at its re-run cap gives ``(None, "capped")``.

    A deferral is never finished work, so waiting for a key or a cap never mints a re-run.
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
        if wake and is_deferred(latest):
            latest = queue.wake(latest.id, uid) or latest
        return latest, "active"
    again = (
        spec.force
        or (retry_failed and latest.state in ("dead", "cancelled"))
        or (retry_key_failure and is_key_failure(latest))
    )
    if not again:
        return latest, "finished"
    nxt = queue.rerun(latest, payload=spec.payload())
    if nxt is None:
        return None, "capped"
    return nxt, "rerun"
