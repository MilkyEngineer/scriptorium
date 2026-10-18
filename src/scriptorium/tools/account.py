"""Account tools (§11.1, M1 subset): connect_remarkable, disconnect_remarkable, account_status.

``connect_remarkable`` is the one cloud write in M1: it exchanges a one-time code for a device
token (a new device at reMarkable on every call). The token is sealed with the master keys and
never leaves this process again; the worker picks the account up from the registry on its
next supervision pass.
"""

from __future__ import annotations

import asyncio
from typing import Final

from mcp_types import CallToolResult
from pydantic import Field

from scriptorium.accounts import (
    DEVICE_DESCRIPTION,
    STATE_LAST_POLL_AT,
    STATE_LAST_POLL_OUTCOME,
    STATE_LAST_SYNC_AT,
    STATE_NEEDS_RELINK,
    STATE_TOKEN_REJECTIONS,
    device_token_name,
)
from scriptorium.auth.oidc import User
from scriptorium.cloud.sync import (
    STATE_ANOMALY,
    STATE_ANOMALY_REASON,
    STATE_LAST_FULL_LISTING_OK,
    STATE_TOMBSTONING_SUSPENDED_FAILED,
    STATE_TOMBSTONING_SUSPENDED_SINCE,
)
from scriptorium.db.connect import from_db_time
from scriptorium.errors import RefusedError
from scriptorium.tools import ToolContext, ToolRegistry, ToolSpec
from scriptorium.tools._conventions import (
    READ_ONLY,
    RESULT_CAP_BYTES,
    ErrorCode,
    ToolArgs,
    ToolError,
    invalid_argument,
    iso_utc,
    json_result,
    tool_annotations,
)
from scriptorium.tools._library import (
    ACCT_PATTERN,
    CONNECT_URL,
    Account,
    not_linked,
    open_account,
    services,
)
from scriptorium.tools._services import Services
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import worker_heartbeat_path
from scriptorium.worker import HEARTBEAT_STALE_SECONDS, heartbeat_age, read_heartbeat

DEVICES_URL: Final = "https://my.remarkable.com/"
OCR_LAZY_NOTE: Final = (
    "Pages are transcribed by your own Claude session: read_page returns a page's image "
    "with instructions, and save_transcript stores the result. Background transcription "
    "with an API key arrives in a later release."
)


# ---- connect_remarkable ----------------------------------------------------------------------


class ConnectArgs(ToolArgs):
    code: str = Field(
        min_length=4,
        max_length=32,
        pattern=r"^[A-Za-z0-9]+$",
        description=f"The one-time code from {CONNECT_URL}; it expires in a few minutes.",
    )
    replace: bool = Field(
        default=False,
        description="Replace the account linked now; its archive stays readable.",
    )


def _connect(svc: Services, user: User, code: str, replace: bool) -> dict[str, object]:
    uid = svc.uid(user)
    before = [row for row in svc.store.accounts(uid) if row.active]
    if before and not replace:
        # Checked before registering, so a refused call does not leave a device behind.
        raise ToolError(
            ErrorCode.REFUSED,
            "A reMarkable account is already linked.",
            "Call disconnect_remarkable first, or connect_remarkable with replace=true; "
            "either way with a fresh code.",
        )
    token = svc.register_device(code, DEVICE_DESCRIPTION)
    # Nothing is sealed until the link is recorded: a refusal here (the account belongs to
    # another user) leaves no token on this server, only the new device at reMarkable.
    identity = svc.identify(token)
    try:
        row = svc.store.link_account(uid, identity, DEVICE_DESCRIPTION, replace=replace)
    except RefusedError as exc:
        raise ToolError(
            ErrorCode.REFUSED,
            exc.message,
            f"{exc.hint + '. ' if exc.hint else ''}This attempt registered a device named "
            f'"{DEVICE_DESCRIPTION}" at reMarkable; remove it at {DEVICES_URL}.',
        ) from None
    box = SecretBox(svc.settings, uid)
    box.seal(device_token_name(row.acct), token.encode("ascii"))
    replaced = [r.acct for r in before if r.acct != row.acct]
    for acct in replaced:
        # Deactivated by link_account: its token must not outlive it (as on disconnect).
        box.forget(device_token_name(acct))
    index = svc.index(uid, row.acct)
    with index.transaction():
        index.set_state(STATE_NEEDS_RELINK, None)
        index.set_state(STATE_TOKEN_REJECTIONS, None)
    message = (
        "Linked. The first sync starts within a few minutes; account_status shows its "
        f'progress. This registered a new device named "{DEVICE_DESCRIPTION}" at '
        f"reMarkable; you can see and remove devices at {DEVICES_URL}."
    )
    if before:
        message += (
            " The device this server used before is no longer used and its token was deleted "
            f"here; remove that device at {DEVICES_URL} too (Scriptorium cannot revoke it)."
        )
    return {"linked": True, "account": row.acct, "replaced": replaced, "message": message}


async def connect_remarkable(ctx: ToolContext, args: ConnectArgs) -> CallToolResult:
    """Link your reMarkable account: exchange a one-time code from
    https://my.remarkable.com/device/browser/connect for a device token, which is stored
    encrypted on this server, and start mirroring your library. Each call registers a new
    device at reMarkable. Refused if you already have a linked account, unless replace is
    true (the old archive stays readable through list(account=...)), or if that reMarkable
    account is linked to another user."""
    payload = await asyncio.to_thread(_connect, services(ctx), ctx.user, args.code, args.replace)
    ctx.audit["acct"] = payload["account"]
    return json_result(payload, cap=RESULT_CAP_BYTES)


# ---- disconnect_remarkable -------------------------------------------------------------------


class DisconnectArgs(ToolArgs):
    keep_archive: bool = Field(
        default=True,
        description="Keep the archive on this server (the only option in this release).",
    )
    confirmation_token: str | None = Field(
        default=None, max_length=512, description="Not used while keep_archive is true."
    )


def _disconnect(svc: Services, user: User) -> dict[str, object]:
    uid = svc.uid(user)
    active = [row for row in svc.store.accounts(uid) if row.active]
    if not active:
        raise not_linked()
    box = SecretBox(svc.settings, uid)
    for row in active:
        svc.store.deactivate_account(uid, row.acct)
        box.forget(device_token_name(row.acct))
    return {
        "disconnected": [row.acct for row in active],
        "archive_kept": True,
        "message": (
            "Polling stopped and the device token was deleted from this server. Scriptorium "
            "cannot revoke it at reMarkable: remove the device named "
            f'"{DEVICE_DESCRIPTION}" at {DEVICES_URL}. The archive stays readable with '
            "list(account=...)."
        ),
    }


async def disconnect_remarkable(ctx: ToolContext, args: DisconnectArgs) -> CallToolResult:
    """Unlink your reMarkable account: stop polling and delete the device token from this
    server. The archive is kept and stays readable. Scriptorium cannot revoke the token at
    reMarkable, so the reply tells you where to remove the device yourself."""
    if not args.keep_archive:
        raise ToolError(
            ErrorCode.REFUSED,
            "Deleting the archive is not available in this release.",
            "Disconnect with keep_archive=true; the archive stays on this server.",
        )
    if args.confirmation_token is not None:
        raise invalid_argument("A confirmation token is only used to delete the archive.")
    payload = await asyncio.to_thread(_disconnect, services(ctx), ctx.user)
    accounts = payload["disconnected"]
    if isinstance(accounts, list) and len(accounts) == 1:
        ctx.audit["acct"] = accounts[0]
    return json_result(payload, cap=RESULT_CAP_BYTES)


# ---- account_status --------------------------------------------------------------------------


class StatusArgs(ToolArgs):
    account: str | None = Field(
        default=None,
        pattern=ACCT_PATTERN,
        description="An account id from other_accounts; the linked account when omitted.",
    )


def _time(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return iso_utc(from_db_time(value))
    except ValueError:
        return None


def _status(svc: Services, account: Account) -> dict[str, object]:
    index = account.index
    state = {
        key: index.get_state(key)
        for key in (
            STATE_NEEDS_RELINK,
            STATE_TOKEN_REJECTIONS,
            STATE_LAST_SYNC_AT,
            STATE_LAST_POLL_AT,
            STATE_LAST_POLL_OUTCOME,
            STATE_ANOMALY,
            STATE_ANOMALY_REASON,
            STATE_LAST_FULL_LISTING_OK,
            STATE_TOMBSTONING_SUSPENDED_SINCE,
            STATE_TOMBSTONING_SUSPENDED_FAILED,
        )
    }
    jobs = svc.queue.counts(account.uid, acct=account.acct)
    counts = index.counts()
    warnings: list[dict[str, object]] = []
    status = "ok"
    if not account.row.active:
        status = "disconnected"
        warnings.append(
            {
                "code": "disconnected",
                "message": "This account is not polled any more; its archive stays readable.",
            }
        )
    elif state[STATE_NEEDS_RELINK] == "true":
        status = "needs_relink"
        warnings.append(
            {
                "code": ErrorCode.NEEDS_RELINK.value,
                "message": "reMarkable rejected the device token three polls in a row.",
                "hint": f"Get a new code from {CONNECT_URL} and call connect_remarkable "
                "with replace=true.",
            }
        )
    if state[STATE_ANOMALY]:
        status = "anomaly" if status == "ok" else status
        warnings.append(
            {
                "code": ErrorCode.SYNC_ANOMALY.value,
                "anomaly": state[STATE_ANOMALY],
                "reason": state[STATE_ANOMALY_REASON],
                "message": "Sync is paused in read-only mode; the archive is still served.",
                "hint": "An administrator must look at it and clear it.",
            }
        )
    suspended_since = _time(state[STATE_TOMBSTONING_SUSPENDED_SINCE])
    if suspended_since is not None:
        failed_text = state[STATE_TOMBSTONING_SUSPENDED_FAILED] or ""
        failed = int(failed_text) if failed_text.isdigit() else None
        warnings.append(
            {
                "code": "tombstoning_suspended",
                "since": suspended_since,
                "failed": failed,
                "message": "Some documents could not be listed from reMarkable, so documents "
                "deleted there are not moved to the trash here until every document lists "
                "again. Nothing is lost meanwhile.",
                "hint": "If this persists, ask the operator to check the worker logs.",
            }
        )
    verdicts, rerunnable = svc.queue.dead_renders(account.uid, acct=account.acct)
    dead = verdicts + rerunnable
    if dead:
        message = f"{dead} page images could not be rendered."
        if rerunnable:
            message += (
                f" {rerunnable} of them failed for a reason other than the page itself and "
                "are rendered again when the page is next asked for or the worker restarts."
            )
        warnings.append(
            {
                "code": "render_failures",
                "count": dead,
                "rerunnable": rerunnable,
                "message": message,
            }
        )
    beat_path = worker_heartbeat_path(svc.settings.data_dir)
    age = heartbeat_age(beat_path)
    rebuilds = read_heartbeat(beat_path).get("render_pool_rebuilds")
    worker = {
        "heartbeat_age_s": None if age is None else round(age, 1),
        "render_pool_rebuilds": rebuilds if type(rebuilds) is int and rebuilds >= 0 else None,
    }
    if account.row.active and (age is None or age > HEARTBEAT_STALE_SECONDS):
        warnings.append(
            {
                "code": "worker_stale",
                "message": "The sync worker has not reported recently; nothing is syncing.",
                "hint": "Ask the operator to check the worker container.",
            }
        )
    if status == "ok" and state[STATE_LAST_SYNC_AT] is None and account.row.active:
        status = "waiting_for_first_sync"
    listing_ok = state[STATE_LAST_FULL_LISTING_OK]
    rejections = state[STATE_TOKEN_REJECTIONS] or "0"
    pending = {
        kind: by_state.get("queued", 0) + by_state.get("failed", 0) + by_state.get("leased", 0)
        for kind, by_state in jobs.items()
    }
    return {
        "linked": account.row.active,
        "account": account.acct,
        "linked_at": iso_utc(account.row.linked_at),
        "status": status,
        "warnings": warnings,
        "sync": {
            "last_sync_at": _time(state[STATE_LAST_SYNC_AT]),
            "last_poll_at": _time(state[STATE_LAST_POLL_AT]),
            "last_poll_outcome": state[STATE_LAST_POLL_OUTCOME],
            "last_full_listing_ok": None if listing_ok is None else listing_ok == "true",
            "token_rejections": int(rejections) if rejections.isdigit() else 0,
        },
        "library": counts,
        "jobs": {"pending": pending, "dead": dead},
        "ocr": {"mode": "lazy", "note": OCR_LAZY_NOTE},
        "quota": None,
        "worker": worker,
    }


def _account_status(svc: Services, user: User, acct: str | None) -> dict[str, object]:
    uid = svc.uid(user)
    rows = svc.store.accounts(uid)
    if not rows:
        return {
            "linked": False,
            "status": "not_linked",
            "message": (
                f"No reMarkable account is linked. Get a one-time code from {CONNECT_URL} "
                "and call connect_remarkable with it."
            ),
            "ocr": {"mode": "lazy", "note": OCR_LAZY_NOTE},
        }
    account = open_account(svc, user, acct)
    payload = _status(svc, account)
    payload["other_accounts"] = [
        {"account": r.acct, "active": r.active, "linked_at": iso_utc(r.linked_at)}
        for r in rows
        if r.acct != account.acct
    ]
    return payload


async def account_status(ctx: ToolContext, args: StatusArgs) -> CallToolResult:
    """Show the state of your linked reMarkable account: whether it is linked, when it last
    synced, how many documents, pages and transcripts are archived, pending and failed page
    renders, the OCR mode, and warnings (the account needs relinking, sync is paused by an
    anomaly such as a mass deletion, the worker is not running). Works before linking too:
    it then explains how to connect."""
    payload = await asyncio.to_thread(_account_status, services(ctx), ctx.user, args.account)
    acct = payload.get("account")
    if isinstance(acct, str):
        ctx.audit["acct"] = acct
    return json_result(payload, cap=RESULT_CAP_BYTES)


def register(registry: ToolRegistry) -> None:
    """Add the M1 account tools."""
    registry.add(
        ToolSpec.from_handler(
            connect_remarkable,
            args=ConnectArgs,
            annotations=tool_annotations(read_only=False, open_world=True),
        )
    )
    registry.add(
        ToolSpec.from_handler(
            disconnect_remarkable,
            args=DisconnectArgs,
            annotations=tool_annotations(read_only=False, destructive=True, idempotent=True),
        )
    )
    registry.add(ToolSpec.from_handler(account_status, args=StatusArgs, annotations=READ_ONLY))


__all__ = ["account_status", "connect_remarkable", "disconnect_remarkable", "register"]
