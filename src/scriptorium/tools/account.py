"""Account tools (§11.1): connect_remarkable, disconnect_remarkable, account_status,
set_sync_scope, and the user's own Claude API key (set_claude_api_key, clear_claude_api_key).

``connect_remarkable`` is the one cloud write in M1: it exchanges a one-time code for a device
token (a new device at reMarkable on every call). The token is sealed with the master keys and
never leaves this process again; the worker picks the account up from the registry on its
next supervision pass.

A Claude API key is checked with the cheapest API call before it is sealed (SecretBox name
``claude_api_key``) and is never returned, logged or audited afterwards, not even a prefix:
results say only whether a key is stored (§8.1).

``set_sync_scope`` stores the account's scope (``cloud/scope.py``) in its index; the worker
applies it on its next poll. Tag names are the user's display strings: they are returned to
the user and never logged or audited (the audit records how many there are).
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Final, Literal

from mcp_types import CallToolResult
from pydantic import ConfigDict, Field, SecretStr, field_validator

from scriptorium.accounts import (
    DEVICE_DESCRIPTION,
    STATE_LAST_POLL_AT,
    STATE_LAST_POLL_OUTCOME,
    STATE_LAST_SYNC_AT,
    STATE_NEEDS_RELINK,
    STATE_TOKEN_REJECTIONS,
    device_token_name,
    is_anomaly_reason,
    move_aside,
    poll_lock,
    removal_destination,
)
from scriptorium.auth.oidc import User
from scriptorium.cloud.scope import (
    MAX_SCOPE_TAGS,
    MAX_TAG_LEN,
    SETTING_SYNC_SCOPE,
    ScopeError,
    SyncScope,
    dump_scope,
    load_scope,
    make_scope,
)
from scriptorium.cloud.sync import (
    STATE_ANOMALY,
    STATE_ANOMALY_REASON,
    STATE_LAST_FULL_LISTING_OK,
    STATE_TOMBSTONING_SUSPENDED_FAILED,
    STATE_TOMBSTONING_SUSPENDED_SINCE,
)
from scriptorium.config import API_KEY_RE
from scriptorium.db.connect import from_db_time
from scriptorium.errors import RefusedError
from scriptorium.library.index import Index
from scriptorium.logging import log_event
from scriptorium.ocr.errors import OcrRefused, OcrUnavailable
from scriptorium.ocr.modes import USER_KEY_SECRET
from scriptorium.ocr.pipeline import (
    AUTO_OCR_KEY,
    AUTO_OCR_MAX_FOLDERS,
    SERVER_MODES,
    AutoOcr,
    dump_auto_ocr,
)
from scriptorium.tools import ToolContext, ToolRegistry, ToolSpec
from scriptorium.tools._conventions import (
    CONFIRMATION_TTL_SECONDS,
    READ_ONLY,
    RESULT_CAP_BYTES,
    ErrorCode,
    NoArgs,
    ToolArgs,
    ToolError,
    invalid_argument,
    iso_utc,
    issue_confirmation_token,
    json_result,
    refuse_spent_token,
    spend_confirmation_token,
    tool_annotations,
    verify_confirmation_token,
)
from scriptorium.tools._library import (
    ACCT_PATTERN,
    CONNECT_URL,
    FOLDER,
    IDENTIFIER_MAX,
    Account,
    display_name,
    not_linked,
    open_account,
    resolve,
    services,
    state_of,
    use_account,
)
from scriptorium.tools._ocr import account_ocr, ocr_mode, ocr_summary, resume_ocr
from scriptorium.tools._services import KEY_CHECK_WINDOW_S, Services
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import (
    AccountPaths,
    AccountRow,
    account_paths,
    worker_heartbeat_path,
)
from scriptorium.worker import HEARTBEAT_STALE_SECONDS, heartbeat_age, read_heartbeat

logger = logging.getLogger(__name__)

DEVICES_URL: Final = "https://my.remarkable.com/"
CONSOLE_URL: Final = "https://console.anthropic.com/settings/keys"
MAX_OWN_CAP_USD: Final = 100_000.0


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
    svc = services(ctx)
    uid = await asyncio.to_thread(svc.uid, ctx.user)
    # One connect per user at a time: the "already linked" check, the registration, the
    # link and the sealing run as one sequence, so a concurrent call sees the link.
    async with svc.connect_lock(uid):
        payload = await asyncio.to_thread(_connect, svc, ctx.user, args.code, args.replace)
    ctx.audit["acct"] = payload["account"]
    return json_result(payload, cap=RESULT_CAP_BYTES)


# ---- disconnect_remarkable -------------------------------------------------------------------


class DisconnectArgs(ToolArgs):
    keep_archive: bool = Field(
        default=True,
        description="Keep the archive readable on this server (the default). false removes "
        "it from Scriptorium's reach in two steps: a dry run, then a call with its token.",
    )
    confirmation_token: str | None = Field(
        default=None,
        max_length=512,
        description="With keep_archive=false: the token the dry run returned (valid 10 "
        "minutes). Without it the call is a dry run that changes nothing.",
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


REMOVE_TOOL: Final = "disconnect_remarkable"
REMOVE_LOCK_WAIT_S: Final = 10.0
"""How long the execute step waits for a running poll of the account to end."""
_REMOVE_LOCK_STEP_S: Final = 0.2


def _removal_target(svc: Services, user: User) -> tuple[str, AccountRow]:
    """The account keep_archive=false acts on: the linked one, else the latest disconnected
    one that was not removed (so an archive kept earlier can still be removed)."""
    uid = svc.uid(user)
    rows = [r for r in svc.store.accounts(uid) if r.removed_at is None]
    active = [r for r in rows if r.active]
    row = active[-1] if active else (rows[-1] if rows else None)
    if row is None:
        raise not_linked()
    return uid, row


def _bound_args(acct: str) -> dict[str, object]:
    """What a removal token is bound to besides the user and the tool: the arguments and the
    account, so a token from a dry run never removes an account linked after it."""
    return {"keep_archive": False, "account": acct}


def _archive_summary(paths: AccountPaths, index: Index) -> dict[str, int]:
    """Counts only (§11.1): documents (any state), pages, blob files, bytes on disk."""
    counts = index.counts()
    blobs = 0
    total = 0
    for directory, _dirs, files in os.walk(paths.root, followlinks=False):
        in_blobs = Path(directory).is_relative_to(paths.blobs)
        for name in files:
            try:
                info = os.lstat(os.path.join(directory, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                blobs += in_blobs
    return {
        "documents": counts["documents"] + counts["trashed"] + counts["tombstoned"],
        "pages": counts["pages"],
        "blobs": blobs,
        "bytes": total,
    }


def _removal_dry_run(svc: Services, user: User) -> dict[str, object]:
    uid, row = _removal_target(svc, user)
    paths = account_paths(svc.settings.data_dir, uid, row.acct)
    summary = _archive_summary(paths, svc.index(uid, row.acct))
    now = svc.clock()
    token = issue_confirmation_token(
        svc.settings, uid=uid, tool=REMOVE_TOOL, args=_bound_args(row.acct), now=now
    )
    return {
        "dry_run": True,
        "account": row.acct,
        "linked": row.active,
        "summary": summary,
        "confirmation_token": token,
        "expires_at": iso_utc(now + timedelta(seconds=CONFIRMATION_TTL_SECONDS)),
        "message": (
            "Nothing was changed. Calling disconnect_remarkable again with keep_archive=false "
            "and this confirmation_token within 10 minutes stops polling, deletes the device "
            "token from this server and moves this account's archive (the counts above, "
            "trashed and deleted documents included) out of Scriptorium's reach: no tool can "
            "read it afterwards. Nothing is deleted from the server's disk by this; the "
            "operator can reclaim the space."
        ),
    }


def _busy() -> ToolError:
    return ToolError(
        ErrorCode.REFUSED,
        "A sync of this account is running.",
        "Call disconnect_remarkable again in a minute with the same token.",
        extra={"reason": "busy"},
    )


@dataclass(frozen=True)
class _Removal:
    """A verified execute step: the account it removes and when it was asked."""

    uid: str
    row: AccountRow
    paths: AccountPaths
    token: str
    now: datetime


def _removal_checked(svc: Services, user: User, token: str) -> _Removal:
    refuse_spent_token(svc.store, token)  # before the target: a used token's account is gone
    uid, row = _removal_target(svc, user)
    now = svc.clock()
    verify_confirmation_token(
        svc.settings, token, uid=uid, tool=REMOVE_TOOL, args=_bound_args(row.acct), now=now
    )
    paths = account_paths(svc.settings.data_dir, uid, row.acct)
    return _Removal(uid, row, paths, token, now)


def _try_remove(svc: Services, removal: _Removal) -> dict[str, object] | None:
    """Remove the account if no poll holds its lock; None if one does (nothing changed)."""
    # Not blocking: a long first sync would otherwise hold a pool thread for its duration.
    with poll_lock(removal.paths, blocking=False) as state:
        if state == "held":
            return _remove_locked(svc, removal)
        if state == "gone":
            raise ToolError(
                ErrorCode.REFUSED,
                "This account's archive is not where it should be.",
                "Call account_status; if the account is still listed, ask the operator "
                "to check the server.",
            )
    return None


async def _remove(svc: Services, user: User, token: str) -> dict[str, object]:
    """The execute step, under the user's connect lock (so a concurrent connect_remarkable
    cannot seal a token for the account being removed, and one user runs one removal at a
    time). While a poll runs, it waits on the event loop, not in a pool thread."""
    uid = await asyncio.to_thread(svc.uid, user)
    async with svc.connect_lock(uid):
        removal = await asyncio.to_thread(_removal_checked, svc, user, token)
        deadline = time.monotonic() + REMOVE_LOCK_WAIT_S
        while True:
            payload = await asyncio.to_thread(_try_remove, svc, removal)
            if payload is not None:
                return payload
            if time.monotonic() >= deadline:
                raise _busy()  # the token is not spent: it can be used again
            await asyncio.sleep(_REMOVE_LOCK_STEP_S)


def _after_failed_move(
    svc: Services, removal: _Removal, destination: Path, exc: Exception
) -> ToolError | None:
    """What a failure during the removal left behind, read from the filesystem rather than
    from the exception (a step can fail after the rename): None when the directory reached
    ``removed/``, so the removal stands; otherwise the error to raise.

    Only an account whose directory is still in place is put back (``removed_at`` cleared):
    putting back one whose directory is elsewhere would let the next :meth:`Services.index`
    create an empty account directory and index in its place.
    """
    uid, acct = removal.uid, removal.row.acct
    reason = type(exc).__name__
    if os.path.lexists(destination):
        log_event(logger, "account_move_incomplete", level=logging.ERROR, acct=acct, reason=reason)
        return None
    log_event(logger, "account_move_failed", level=logging.ERROR, acct=acct, reason=reason)
    if not os.path.lexists(removal.paths.root):
        # Neither here nor there: it stays removed (and refused) until the operator looks.
        return ToolError(
            ErrorCode.REFUSED,
            "The account was disconnected, but its archive is not where it should be.",
            "Ask the operator to check the server logs.",
            extra={"reason": "move_failed"},
        )
    svc.store.unmark_removed(uid, acct)
    return ToolError(
        ErrorCode.REFUSED,
        "The account was disconnected, but its archive could not be moved.",
        "It stays readable with list(account=...). Call disconnect_remarkable with "
        "keep_archive=false again to retry, or ask the operator to check the logs.",
        extra={"reason": "move_failed"},
    )


def _remove_locked(svc: Services, removal: _Removal) -> dict[str, object]:
    """Under the poll lock: no poll runs and none starts until the directory has moved."""
    uid, row, paths, now = removal.uid, removal.row, removal.paths, removal.now
    try:
        destination = removal_destination(svc.settings.data_dir, uid, row.acct, at=now)
    except RefusedError:
        raise ToolError(
            ErrorCode.REFUSED,
            "This account's archive cannot be moved aside on this server.",
            "Nothing was changed. Ask the operator to check the server logs.",
        ) from None
    spend_confirmation_token(svc.store, removal.token, now=now)
    # Removed before the move, so nothing reopens the account (and recreates its directory)
    # meanwhile. The steps that can be undone come first: until the rename, a failure puts
    # the account back whole, device token included.
    svc.store.mark_removed(uid, row.acct, at=now)
    try:
        svc.evict_index(uid, row.acct)
        move_aside(paths, destination)
    except Exception as exc:
        failed = _after_failed_move(svc, removal, destination, exc)
        if failed is not None:
            raise failed from None
    log_event(logger, "account_moved", acct=row.acct)
    # Last, as on any disconnect. If it fails the account stays removed (its directory is in
    # removed/, and no loop polls a removed account); the caller is told, not refused.
    forget_failed = False
    try:
        SecretBox(svc.settings, uid).forget(device_token_name(row.acct))
    except Exception as exc:
        forget_failed = True
        log_event(
            logger,
            "account_token_forget_failed",
            level=logging.ERROR,
            acct=row.acct,
            reason=type(exc).__name__,
        )
    if forget_failed:
        done = (
            "Polling stopped and the archive was moved out of Scriptorium's reach, but the "
            "device token could not be deleted from this server: ask the operator to check "
            "the server logs."
        )
    else:
        done = (
            "Polling stopped, the device token was deleted from this server and the archive "
            "was moved out of Scriptorium's reach."
        )
    return {
        "removed": True,
        "account": row.acct,
        "archive_kept": False,
        "token_forget_failed": forget_failed,
        "message": (
            f"{done} Scriptorium cannot revoke the token at reMarkable: remove the device "
            f'named "{DEVICE_DESCRIPTION}" at {DEVICES_URL}.'
        ),
    }


async def disconnect_remarkable(ctx: ToolContext, args: DisconnectArgs) -> CallToolResult:
    """Unlink your reMarkable account: stop polling and delete the device token from this
    server. By default the archive is kept and stays readable. With keep_archive=false the
    archive is also taken out of Scriptorium's reach, in two steps: a first call without a
    confirmation_token is a dry run that changes nothing and returns how many documents,
    pages, blobs and bytes would go, with a token; a second call with that token within 10
    minutes does it (nothing is deleted from the server's disk; the operator can reclaim
    the space). Scriptorium cannot revoke the token at reMarkable, so the reply tells you
    where to remove the device yourself."""
    svc = services(ctx)
    if args.keep_archive:
        if args.confirmation_token is not None:
            raise invalid_argument("A confirmation token is only used with keep_archive=false.")
        payload = await asyncio.to_thread(_disconnect, svc, ctx.user)
        accounts = payload["disconnected"]
        if isinstance(accounts, list) and len(accounts) == 1:
            ctx.audit["acct"] = accounts[0]
        return json_result(payload, cap=RESULT_CAP_BYTES)
    if args.confirmation_token is None:
        payload = await asyncio.to_thread(_removal_dry_run, svc, ctx.user)
    else:
        payload = await _remove(svc, ctx.user, args.confirmation_token)
    ctx.audit["acct"] = payload["account"]
    return json_result(payload, cap=RESULT_CAP_BYTES)


# ---- set_sync_scope ----------------------------------------------------------------------------


class ScopeArgs(ToolArgs):
    mode: Literal["all", "allow", "deny"] = Field(
        description="all: mirror every document (the default). allow: only documents that "
        "carry one of tags, or sit in a folder that does. deny: every document except those.",
    )
    tags: list[Annotated[str, Field(min_length=1, max_length=4 * MAX_TAG_LEN)]] = Field(
        default_factory=list,
        max_length=MAX_SCOPE_TAGS,
        description="reMarkable tag names, matched exactly after trimming; empty for mode all, "
        f"1 to {MAX_SCOPE_TAGS} for allow and deny.",
    )


def _scope_view(scope: SyncScope) -> dict[str, object]:
    return {"mode": scope.mode, "tags": sorted(scope.tags)}


def _set_scope(svc: Services, user: User, scope: SyncScope) -> dict[str, object]:
    uid = svc.uid(user)
    row = next((r for r in svc.store.accounts(uid) if r.active), None)
    if row is None:
        raise not_linked()
    index = svc.index(uid, row.acct)
    try:
        before: SyncScope | None = load_scope(index)
    except ScopeError:
        before = None  # a damaged setting: replaced below, which is how it gets repaired
    # "all" is the default: no row, as on an account that never had a scope.
    index.set_setting(SETTING_SYNC_SCOPE, None if scope.mode == "all" else dump_scope(scope))
    unmatched = sorted(tag for tag in scope.tags if not index.documents_tagged([tag]))
    if scope.mode == "all":
        message = (
            "Every document is mirrored again from the next sync on; documents excluded until "
            "now are downloaded then."
        )
    else:
        message = (
            "From the next sync on, documents outside this scope are listed as excluded and "
            "are not downloaded; documents that come into it are downloaded. Nothing archived "
            "here is deleted: what was mirrored before stays readable. Tags on single pages "
            "do not count, and a document whose tags cannot be read is left as it is until "
            "they can."
        )
    if unmatched:
        message += (
            " Some of these tags are on no document or folder archived here (check the "
            "spelling); they still apply to documents that carry them later."
        )
    return {
        "account": row.acct,
        "scope": _scope_view(scope),
        "previous": None if before is None else _scope_view(before),
        "changed": before != scope,
        "unmatched_tags": unmatched,
        "message": message,
    }


async def set_sync_scope(ctx: ToolContext, args: ScopeArgs) -> CallToolResult:
    """Choose which documents of your linked reMarkable account this server mirrors: all of
    them (mode "all", the default), only documents carrying one of the given reMarkable tags
    or sitting in a folder that carries one (mode "allow"), or every document except those
    (mode "deny"). Tags are matched exactly after trimming, and tags on single pages do not
    count. The worker applies the scope on its next sync: documents outside it are listed as
    excluded and never downloaded. Narrowing the scope never deletes anything already
    archived; those versions stay readable."""
    try:
        scope = make_scope(args.mode, args.tags)
    except ScopeError as exc:
        raise invalid_argument(
            f"That scope cannot be used: {exc}.",
            f'Mode "all" takes no tags; "allow" and "deny" take 1 to {MAX_SCOPE_TAGS} tag '
            "names without control characters.",
        ) from None
    payload = await asyncio.to_thread(_set_scope, services(ctx), ctx.user, scope)
    ctx.audit["acct"] = payload["account"]
    ctx.audit["count"] = len(scope.tags)
    return json_result(payload, cap=RESULT_CAP_BYTES)


# ---- OCR: the user's own Claude API key ----------------------------------------------------


class SetKeyArgs(ToolArgs):
    # A rejected key must not reach an error string, even one that is never sent anywhere.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    key: SecretStr = Field(
        min_length=20,
        max_length=512,
        description=f"Your Claude API key from {CONSOLE_URL}. Stored encrypted; never shown again.",
    )
    monthly_cap_usd: float | None = Field(
        default=None,
        ge=0,
        le=MAX_OWN_CAP_USD,
        allow_inf_nan=False,
        strict=True,
        description="Stop background transcription with this key after this many US dollars "
        "in a UTC calendar month; null for no cap (the key's own Console limits still apply).",
    )

    @field_validator("key", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("key")
    @classmethod
    def _shape(cls, value: SecretStr) -> SecretStr:
        if not API_KEY_RE.fullmatch(value.get_secret_value()):
            raise ValueError("this is not a Claude API key (letters, digits, - and _ only)")
        return value


def _key_refusal(exc: OcrRefused, model: str) -> ToolError:
    if exc.reason == "bad_key":
        message, hint = "The Claude API rejected this key.", f"Check the key at {CONSOLE_URL}."
    elif exc.reason == "billing":
        message = "The Claude account behind this key has a billing problem."
        hint = "Check billing in the Claude Console, then try again."
    elif exc.reason in ("forbidden", "model_unavailable"):
        message = f"This key cannot use the OCR model {model}."
        hint = f"Use a key from a workspace that can use {model}."
    else:
        message, hint = f"The key could not be checked: {exc.message}.", "Try again later."
    return ToolError(ErrorCode.REFUSED, f"{message} Nothing was stored.", hint)


def _set_key(svc: Services, user: User, key: SecretStr, cap_usd: float | None) -> dict[str, object]:
    uid = svc.uid(user)
    if not svc.allow_key_check(uid):
        raise ToolError(
            ErrorCode.REFUSED,
            "Too many keys were checked recently. Nothing was stored.",
            f"Try again in {KEY_CHECK_WINDOW_S // 60} minutes.",
        )
    model = svc.settings.ocr_model
    try:
        client = svc.ocr_client(svc.settings, key)
        try:
            client.validate_key()
        finally:
            client.close()
    except OcrRefused as exc:
        raise _key_refusal(exc, model) from None
    except OcrUnavailable:
        raise ToolError(
            ErrorCode.REFUSED,
            "The Claude API could not check the key just now. Nothing was stored.",
            "Try again in a few minutes.",
        ) from None
    # The cap first: if sealing then fails, a cap without a key is harmless, while a key
    # without the cap its owner asked for would spend more than they allowed.
    svc.store.set_user_ocr_cap(uid, cap_usd)
    SecretBox(svc.settings, uid).seal(USER_KEY_SECRET, key.get_secret_value().encode("ascii"))
    resume_ocr(svc, uid, forget_refusals=True)  # the worker may try the new key at once
    cap = f" up to ${cap_usd:g} a month" if cap_usd is not None else ""
    return {
        "stored": True,
        "monthly_cap_usd": cap_usd,
        "ocr": ocr_summary(svc, uid),
        "message": (
            "The key works and is stored encrypted on this server; it is never shown again. "
            f"Pages are transcribed in the background with it{cap}, and the spend appears in "
            "account_status. It replaces any key you stored before; clear_claude_api_key "
            f"removes it. You can also revoke it at any time at {CONSOLE_URL}."
        ),
    }


async def set_claude_api_key(ctx: ToolContext, args: SetKeyArgs) -> CallToolResult:
    """Store your own Claude API key so that your pages are transcribed in the background
    with it, optionally capped at monthly_cap_usd US dollars per UTC calendar month. The key
    is checked with a free API call first and then stored encrypted on this server; no tool
    ever shows it again. A dedicated key with its own spend limit in the Claude Console is
    best, since the key passes through this conversation. Calling again replaces the key and
    the cap."""
    payload = await asyncio.to_thread(
        _set_key, services(ctx), ctx.user, args.key, args.monthly_cap_usd
    )
    return json_result(payload, cap=RESULT_CAP_BYTES)


def _clear_key(svc: Services, user: User) -> dict[str, object]:
    uid = svc.uid(user)
    SecretBox(svc.settings, uid).forget(USER_KEY_SECRET)
    svc.store.set_user_ocr_cap(uid, None)
    resume_ocr(svc, uid, forget_refusals=True)  # the server key may apply now
    return {
        "cleared": True,
        "ocr": ocr_summary(svc, uid),
        "message": (
            "Your Claude API key is no longer stored on this server; transcripts and the spend "
            f"record are kept. To revoke the key itself, delete it at {CONSOLE_URL}."
        ),
    }


async def clear_claude_api_key(ctx: ToolContext, args: NoArgs) -> CallToolResult:
    """Delete your Claude API key from this server, together with the monthly cap you set on
    it. Transcripts made with it and the record of what it spent are kept. Pages are then
    transcribed with this server's key if you may use it, or by your own Claude session.
    Safe to call when no key is stored."""
    payload = await asyncio.to_thread(_clear_key, services(ctx), ctx.user)
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
                "reason": (
                    state[STATE_ANOMALY_REASON]
                    if is_anomaly_reason(state[STATE_ANOMALY_REASON])
                    else None
                ),
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
        "ocr": account_ocr(svc, account, account.uid),
        "quota": None,
        "worker": worker,
    }


def _other_account(row: AccountRow) -> dict[str, object]:
    item: dict[str, object] = {
        "account": row.acct,
        "active": row.active,
        "linked_at": iso_utc(row.linked_at),
    }
    if row.removed_at is not None:
        item["removed_at"] = iso_utc(row.removed_at)  # its archive is no longer served
    return item


def _account_status(svc: Services, user: User, acct: str | None) -> dict[str, object]:
    uid = svc.uid(user)
    rows = svc.store.accounts(uid)
    if acct is None and all(r.removed_at is not None for r in rows):
        payload: dict[str, object] = {
            "user": uid,
            "linked": False,
            "status": "not_linked",
            "message": (
                f"No reMarkable account is linked. Get a one-time code from {CONNECT_URL} "
                "and call connect_remarkable with it."
            ),
            "ocr": account_ocr(svc, None, uid),
        }
        if rows:
            payload["other_accounts"] = [_other_account(r) for r in rows]
        return payload
    account = open_account(svc, user, acct)
    payload = {"user": uid, **_status(svc, account)}
    payload["other_accounts"] = [_other_account(r) for r in rows if r.acct != account.acct]
    return payload


async def account_status(ctx: ToolContext, args: StatusArgs) -> CallToolResult:
    """Show the state of your linked reMarkable account: whether it is linked, when it last
    synced, how many documents, pages and transcripts are archived, pending and failed page
    renders, how pages are transcribed (the OCR mode and why, this month's spend and the caps
    that apply), and warnings (the account needs relinking, sync is paused by an anomaly such
    as a mass deletion, the worker is not running). Also shows your user id, which an
    administrator needs to change your settings. Works before linking too: it then explains
    how to connect."""
    payload = await asyncio.to_thread(_account_status, services(ctx), ctx.user, args.account)
    acct = payload.get("account")
    if isinstance(acct, str):
        ctx.audit["acct"] = acct
    return json_result(payload, cap=RESULT_CAP_BYTES)


# ---- set_auto_ocr ------------------------------------------------------------------------------


class AutoOcrArgs(ToolArgs):
    folders: list[Annotated[str, Field(min_length=1, max_length=IDENTIFIER_MAX)]] = Field(
        default_factory=list,
        max_length=AUTO_OCR_MAX_FOLDERS,
        description="Folder UUIDs or paths of names; their sub-folders are included.",
    )
    enabled: bool = Field(description="true to transcribe new pages in these folders.")
    account: str | None = Field(
        default=None, pattern=ACCT_PATTERN, description="Account id; the linked one by default."
    )


def _set_auto_ocr(svc: Services, account: Account, args: AutoOcrArgs) -> dict[str, object]:
    index = account.index
    found: dict[str, str] = {}
    for ident in args.folders:
        folder = resolve(index, ident, want=FOLDER)
        state = state_of(folder)
        if state != "live":
            raise invalid_argument(
                f"That folder is {state}; auto-OCR applies to live folders only.",
                "Pick a folder from list.",
            )
        found[folder.uuid] = display_name(folder.name)
    if args.enabled and not found:
        raise invalid_argument(
            "Name at least one folder to turn auto-OCR on.",
            'Pass folder UUIDs or paths from list, e.g. folders=["Notes"].',
        )
    setting = AutoOcr(args.enabled, tuple(sorted(found)))
    index.set_setting(AUTO_OCR_KEY, dump_auto_ocr(setting))
    mode, reason = ocr_mode(svc, account.uid)
    if not args.enabled:
        note = "Auto-OCR is off; transcribe and read_page still transcribe on request."
    elif mode in SERVER_MODES:
        note = "New and changed pages in these folders are transcribed after each sync."
    else:
        note = (
            "Saved, but no API key is available for your pages, so nothing is transcribed "
            "in the background until one is (set_claude_api_key, or ask the operator)."
        )
    return {
        "account": account.acct,
        "enabled": setting.enabled,
        "folders": [{"uuid": u, "name": found[u]} for u in setting.folders],
        "mode": mode,
        "reason": reason,
        "note": note,
    }


async def set_auto_ocr(ctx: ToolContext, args: AutoOcrArgs) -> CallToolResult:
    """Turn automatic transcription on or off for folders of your reMarkable library. When
    on, the worker transcribes new and changed pages in the given folders and their
    sub-folders after each sync, once a page has stopped changing, using your own Claude API
    key or the server's within its monthly caps; ocr_status shows progress and spend. The
    call replaces the previous folder list; enabled=false turns auto-OCR off."""
    account = await use_account(ctx, args.account)
    payload = await asyncio.to_thread(_set_auto_ocr, services(ctx), account, args)
    folders = payload["folders"]
    if isinstance(folders, list):
        ctx.audit["uuids"] = [f["uuid"] for f in folders]
        ctx.audit["count"] = len(folders)
    return json_result(payload, cap=RESULT_CAP_BYTES)


def register(registry: ToolRegistry) -> None:
    """Add the account tools."""
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
    registry.add(
        ToolSpec.from_handler(
            set_sync_scope,
            args=ScopeArgs,
            annotations=tool_annotations(read_only=False, idempotent=True),
        )
    )
    registry.add(
        ToolSpec.from_handler(
            set_claude_api_key,
            args=SetKeyArgs,
            annotations=tool_annotations(read_only=False, idempotent=True, open_world=True),
        )
    )
    registry.add(
        ToolSpec.from_handler(
            clear_claude_api_key,
            args=NoArgs,
            annotations=tool_annotations(read_only=False, destructive=True, idempotent=True),
        )
    )
    registry.add(
        ToolSpec.from_handler(
            set_auto_ocr,
            args=AutoOcrArgs,
            annotations=tool_annotations(read_only=False, idempotent=True),
        )
    )


__all__ = [
    "account_status",
    "clear_claude_api_key",
    "connect_remarkable",
    "disconnect_remarkable",
    "register",
    "set_auto_ocr",
    "set_claude_api_key",
    "set_sync_scope",
]
