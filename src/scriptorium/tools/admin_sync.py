"""Admin sync tools (§11.6): ``admin_clear_anomaly``.

An anomaly (§7: a tripped mass-delete breaker, a regressed or emptied root, an unparseable
root) puts the user's active account in read-only safe mode until an administrator has
looked. Clearing it only removes the marker: the next poll decides everything again, so a
cloud listing that is still wrong trips the same anomaly again. ``allow_mass_delete=true``
additionally writes the one-shot approval of §7 step 4, built from the candidates the
breaker stored (``sync_state.anomaly_candidates``) with :func:`mass_delete_approval`; the
next poll whose planner runs spends it, used or not, and what it lets through is tombstoned
(moved, never deleted).

Like the other admin tools it is ``admin_only``, names the user by uid, and audits only
identifiers: the uid, the account, a count and the length of the administrator's reason.
The candidates and the reason text are never returned, stored or logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Final

from mcp_types import CallToolResult
from pydantic import Field, field_validator

from scriptorium.cloud.protocol import is_uuid
from scriptorium.cloud.sync import (
    ANOMALY_MASS_DELETE,
    STATE_ALLOW_MASS_DELETE,
    STATE_ANOMALY,
    STATE_ANOMALY_CANDIDATES,
    STATE_ANOMALY_REASON,
    STATE_LAST_GENERATION,
    mass_delete_approval,
)
from scriptorium.db.connect import to_db_time
from scriptorium.library.index import Index
from scriptorium.logging import log_event
from scriptorium.tools import ToolContext, ToolRegistry, ToolSpec
from scriptorium.tools._conventions import (
    RESULT_CAP_BYTES,
    ErrorCode,
    ToolArgs,
    ToolError,
    json_result,
    tool_annotations,
)
from scriptorium.tools._library import services
from scriptorium.tools._services import Services
from scriptorium.tools.admin import _USER_DESCRIPTION, UID_PATTERN, _unknown_user

__all__ = ["admin_clear_anomaly", "register"]

logger = logging.getLogger(__name__)

MAX_REASON_CHARS: Final = 200
APPROVAL_MIN_VALIDITY: Final = timedelta(hours=1)
APPROVAL_POLLS: Final = 3
"""An approval outlives this many poll intervals (at least :data:`APPROVAL_MIN_VALIDITY`), so a
poll that is late or backs off once still finds it; the first poll that plans spends it."""


class ClearAnomalyArgs(ToolArgs):
    user: str = Field(pattern=UID_PATTERN, description=_USER_DESCRIPTION)
    reason: str = Field(
        min_length=1,
        max_length=MAX_REASON_CHARS,
        description="Why it is safe to clear, in your own words (up to 200 characters). Only "
        "its length is recorded.",
    )
    allow_mass_delete: bool = Field(
        default=False,
        strict=True,
        description="true also lets the next poll move the documents that tripped the "
        "mass-delete breaker to the archive's trash, once. Only for a mass_delete anomaly "
        "whose deletions you have checked are genuine.",
    )

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


def _refused(message: str, hint: str, reason: str) -> ToolError:
    return ToolError(ErrorCode.REFUSED, message, hint, extra={"reason": reason})


def _stored_candidates(raw: str | None) -> frozenset[str] | None:
    """The breaker's stored candidates; None if none were stored or the value is unusable
    (an approval is then never built from it)."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, list) or not data:
        return None
    if not all(isinstance(u, str) and is_uuid(u) for u in data):
        return None
    return frozenset(data)


def _generation(raw: str | None) -> int | None:
    if raw is None or not raw.isascii() or not raw.isdigit():
        return None
    return int(raw)


def _clear(
    index: Index, *, allow_mass_delete: bool, expires_at: datetime
) -> tuple[str, str | None, int, bool]:
    """In one transaction: check the anomaly, write or withdraw the approval, clear the
    marker. Returns ``(anomaly, anomaly_reason, approved, withdrawn)``."""
    with index.transaction():
        anomaly = index.get_state(STATE_ANOMALY)
        if not anomaly:
            raise _refused(
                "That account has no sync anomaly to clear.",
                "account_status shows whether sync is paused by an anomaly.",
                "no_anomaly",
            )
        reason = index.get_state(STATE_ANOMALY_REASON)
        withdrawn = False
        approved = 0
        if allow_mass_delete:
            if anomaly != ANOMALY_MASS_DELETE:
                raise _refused(
                    "allow_mass_delete applies only to a mass_delete anomaly.",
                    "Clear it without allow_mass_delete; the next poll looks again.",
                    "not_mass_delete",
                )
            candidates = _stored_candidates(index.get_state(STATE_ANOMALY_CANDIDATES))
            generation = _generation(index.get_state(STATE_LAST_GENERATION))
            if candidates is None or generation is None:
                raise _refused(
                    "No usable record of the documents that tripped the breaker is stored.",
                    "Clear it without allow_mass_delete: if the documents are still missing, "
                    "the next poll trips the breaker again and records them.",
                    "no_candidates",
                )
            index.set_state(
                STATE_ALLOW_MASS_DELETE,
                mass_delete_approval(candidates, root_generation=generation, expires_at=expires_at),
            )
            approved = len(candidates)
        elif index.get_state(STATE_ALLOW_MASS_DELETE) is not None:
            # An approval left by an earlier clear and not yet spent: clearing without
            # approval must not let a mass deletion through.
            index.set_state(STATE_ALLOW_MASS_DELETE, None)
            withdrawn = True
        index.set_state(STATE_ANOMALY, None)
        index.set_state(STATE_ANOMALY_REASON, None)
        index.set_state(STATE_ANOMALY_CANDIDATES, None)
    return anomaly, reason, approved, withdrawn


def _clear_anomaly(svc: Services, uid: str, allow_mass_delete: bool) -> dict[str, object]:
    if svc.store.user(uid) is None:
        raise _unknown_user()
    row = next((r for r in svc.store.accounts(uid) if r.active and r.removed_at is None), None)
    if row is None:
        raise ToolError(
            ErrorCode.NOT_FOUND,
            "That user has no linked reMarkable account.",
            "Only the active account is polled, so only it can have an anomaly to clear.",
        )
    settings = svc.settings
    validity = max(APPROVAL_MIN_VALIDITY, timedelta(seconds=APPROVAL_POLLS * settings.poll_seconds))
    expires_at = svc.clock() + validity
    anomaly, reason, approved, withdrawn = _clear(
        svc.index(uid, row.acct), allow_mass_delete=allow_mass_delete, expires_at=expires_at
    )
    log_event(
        logger,
        "admin.anomaly_cleared",
        level=logging.WARNING,
        acct=row.acct,
        reason=anomaly,
        count=approved if allow_mass_delete else None,
    )
    if allow_mass_delete:
        message = (
            f"Cleared. The next poll may move the {approved} documents that tripped the "
            "breaker to the archive's trash (tombstones; nothing is deleted), once, if they "
            "are still missing and no others have joined them. The approval is spent by that "
            "poll whether or not it is used."
        )
    elif anomaly == ANOMALY_MASS_DELETE:
        message = (
            "Cleared. The next poll looks again: if the same documents are still missing it "
            "trips the breaker again. If the deletions are genuine, clear it then with "
            "allow_mass_delete=true."
        )
    else:
        message = "Cleared. The next poll resumes syncing and decides everything again."
    if withdrawn:
        message += " An earlier, unspent mass-delete approval was withdrawn."
    return {
        "user": uid,
        "account": row.acct,
        "cleared": {"anomaly": anomaly, "reason": reason},
        "allow_mass_delete": allow_mass_delete,
        "approved_documents": approved,
        "approval_expires_at": to_db_time(expires_at) if allow_mass_delete else None,
        "earlier_approval_withdrawn": withdrawn,
        "message": message,
    }


async def admin_clear_anomaly(ctx: ToolContext, args: ClearAnomalyArgs) -> CallToolResult:
    """Administrators only: resume syncing a user's active reMarkable account after a sync
    anomaly (mass deletion, a regressed or empty root, an unreadable root) once a human has
    checked it. The user is named by the 16-character id their account_status shows. Clearing
    alone changes nothing in the archive: the next poll decides again, so a cloud listing that
    is still wrong trips the same anomaly again. With allow_mass_delete=true (mass_delete only)
    the next poll may also move the documents that tripped the breaker to the archive's trash,
    once; nothing is ever deleted. The reason is your own words; only its length is kept."""
    ctx.audit["user"] = args.user
    ctx.audit["allowed"] = args.allow_mass_delete
    ctx.audit["reason_chars"] = len(args.reason)
    payload = await asyncio.to_thread(
        _clear_anomaly, services(ctx), args.user, args.allow_mass_delete
    )
    ctx.audit["acct"] = payload["account"]
    ctx.audit["count"] = payload["approved_documents"]
    return json_result(payload, cap=RESULT_CAP_BYTES)


def register(registry: ToolRegistry) -> None:
    """Add the admin sync tools."""
    registry.add(
        ToolSpec.from_handler(
            admin_clear_anomaly,
            args=ClearAnomalyArgs,
            annotations=tool_annotations(read_only=False, destructive=True),
            admin_only=True,
        )
    )
