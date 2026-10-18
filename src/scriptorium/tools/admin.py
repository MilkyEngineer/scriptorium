"""Admin tools (§11.6): offered only to administrators, refused to everyone else.

Like the spike's admin tool, each is registered with ``admin_only=True``: ``tools/list`` hides
it from other users and a call from one is ``refused`` before the handler runs. A target user
is named by the 16-character uid that ``account_status`` and the audit log show, never by an
email or a token subject; it is the only identifier these tools audit.
"""

from __future__ import annotations

import asyncio
from typing import Final

from mcp_types import CallToolResult
from pydantic import Field

from scriptorium.errors import NotFoundError
from scriptorium.ocr.modes import server_key_listed
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
from scriptorium.tools._ocr import resume_ocr
from scriptorium.tools._services import Services
from scriptorium.users.store import UID_RE

__all__ = ["UID_PATTERN", "admin_allow_server_key", "register"]

UID_PATTERN: Final = f"^{UID_RE.pattern}$"
_USER_DESCRIPTION: Final = (
    "The user's 16-character id as account_status and the audit log show it (never an email)."
)


def _unknown_user() -> ToolError:
    return ToolError(
        ErrorCode.NOT_FOUND,
        "No user has that id.",
        "Ask the user for the id account_status shows them; they must have signed in once.",
    )


# ---- admin_allow_server_key ------------------------------------------------------------------


class AllowServerKeyArgs(ToolArgs):
    user: str = Field(pattern=UID_PATTERN, description=_USER_DESCRIPTION)
    allowed: bool = Field(
        strict=True, description="true lets the user spend the server's key; false withdraws it."
    )


def _allow_server_key(svc: Services, uid: str, allowed: bool) -> dict[str, object]:
    try:
        svc.store.set_server_key_allowed(uid, allowed)
    except NotFoundError:
        raise _unknown_user() from None
    row = svc.store.user(uid)
    if row is None:  # removed between the two statements; nothing else deletes users yet
        raise _unknown_user()
    if allowed:
        resume_ocr(svc, uid, forget_refusals=False)  # their deferred pages may run now
    settings = svc.settings
    listed = server_key_listed(settings, row)
    configured = settings.anthropic_api_key is not None
    notes = []
    if not configured:
        notes.append(
            "No server key is configured (SCRIPTORIUM_ANTHROPIC_API_KEY), so this has no "
            "effect until one is."
        )
    if listed and not allowed:
        notes.append(
            "SERVER_KEY_USERS still admits this user, so they keep the server key until the "
            "operator removes the entry."
        )
    return {
        "user": uid,
        "server_key_allowed": allowed,
        "listed_in_server_key_users": listed,
        "may_use_server_key": configured and (allowed or listed),
        "server_key_configured": configured,
        "monthly_cap_usd": settings.ocr_user_monthly_cap_usd,
        "note": " ".join(notes) or None,
    }


async def admin_allow_server_key(ctx: ToolContext, args: AllowServerKeyArgs) -> CallToolResult:
    """Administrators only: let a user transcribe with this server's Claude API key, or
    withdraw that (allowed=false). The user is named by the 16-character id that their
    account_status shows, never by an email. A user with their own key keeps using it first;
    the server key's monthly caps apply to everyone it serves. Users listed in
    SERVER_KEY_USERS may use the server key whatever this says."""
    ctx.audit["user"] = args.user
    ctx.audit["allowed"] = args.allowed
    payload = await asyncio.to_thread(_allow_server_key, services(ctx), args.user, args.allowed)
    return json_result(payload, cap=RESULT_CAP_BYTES)


def register(registry: ToolRegistry) -> None:
    """Add the admin tools."""
    registry.add(
        ToolSpec.from_handler(
            admin_allow_server_key,
            args=AllowServerKeyArgs,
            annotations=tool_annotations(read_only=False, idempotent=True),
            admin_only=True,
        )
    )
