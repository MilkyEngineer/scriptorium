"""Account tools (§11.1): connect_remarkable, disconnect_remarkable, account_status, and the
user's own Claude API key (set_claude_api_key, clear_claude_api_key).

``connect_remarkable`` is the one cloud write in M1: it exchanges a one-time code for a device
token (a new device at reMarkable on every call). The token is sealed with the master keys and
never leaves this process again; the worker picks the account up from the registry on its
next supervision pass.

A Claude API key is checked with the cheapest API call before it is sealed (SecretBox name
``claude_api_key``) and is never returned, logged or audited afterwards, not even a prefix:
results say only whether a key is stored (§8.1).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Final

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
)
from scriptorium.auth.oidc import User
from scriptorium.cloud.sync import (
    STATE_ANOMALY,
    STATE_ANOMALY_REASON,
    STATE_LAST_FULL_LISTING_OK,
    STATE_TOMBSTONING_SUSPENDED_FAILED,
    STATE_TOMBSTONING_SUSPENDED_SINCE,
)
from scriptorium.config import API_KEY_RE
from scriptorium.db.connect import from_db_time
from scriptorium.errors import RefusedError, ScriptoriumError
from scriptorium.ocr.errors import OcrRefused, OcrUnavailable
from scriptorium.ocr.ledger import month_bounds
from scriptorium.ocr.modes import USER_KEY_SECRET, read_user_key, server_key_permitted
from scriptorium.ocr.modes import resolve as resolve_mode
from scriptorium.ocr.pipeline import (
    AUTO_OCR_KEY,
    AUTO_OCR_MAX_FOLDERS,
    SERVER_MODES,
    AutoOcr,
    dump_auto_ocr,
)
from scriptorium.tools import ToolContext, ToolRegistry, ToolSpec
from scriptorium.tools._conventions import (
    READ_ONLY,
    RESULT_CAP_BYTES,
    ErrorCode,
    NoArgs,
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
from scriptorium.tools._services import KEY_CHECK_WINDOW_S, Services
from scriptorium.tools.ocr import ocr_mode, ocr_queue
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import worker_heartbeat_path
from scriptorium.worker import HEARTBEAT_STALE_SECONDS, heartbeat_age, read_heartbeat

DEVICES_URL: Final = "https://my.remarkable.com/"
CONSOLE_URL: Final = "https://console.anthropic.com/settings/keys"
OCR_LAZY_NOTE: Final = (
    "Pages are transcribed by your own Claude session: read_page returns a page's image "
    "with instructions, and save_transcript stores the result. With your own Claude API key "
    "(or the server's, if the operator allows it) pages are transcribed in the background."
)
OCR_SERVER_NOTE: Final = (
    "Pages are transcribed in the background; read_page waits briefly for a page's "
    "transcript and otherwise answers pending. ocr_status shows the queue and the spend."
)
MAX_OWN_CAP_USD: Final = 100_000.0


def _ocr_summary(svc: Services, account: Account | None, uid: str) -> dict[str, object]:
    """F's key/cap summary plus G's queue numbers (Architecture §11.1 ``account_status.ocr``)."""
    summary = ocr_summary(svc, uid)
    if summary.get("mode") in SERVER_MODES:
        summary["note"] = OCR_SERVER_NOTE
    if account is not None:
        summary["queue"] = ocr_queue(svc, account)
    return summary


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


# ---- OCR: the user's own Claude API key ----------------------------------------------------


def _usd(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _ocr_note(reason: str, resets: str, cap_usd: float | None) -> str:
    day = resets[:10]
    if reason == "user_key":
        cap = f", up to ${cap_usd:g} a month" if cap_usd is not None else ""
        return f"Pages are transcribed in the background with your own Claude API key{cap}."
    if reason == "server_key":
        return (
            "Pages are transcribed in the background with this server's Claude API key, "
            "within a monthly allowance per user."
        )
    if reason == "key_unreadable":
        return (
            "Your stored Claude API key cannot be decrypted on this server, so nothing is "
            "transcribed in the background. Store it again with set_claude_api_key, or remove "
            "it with clear_claude_api_key."
        )
    more = {
        "no_key": "To transcribe in the background, store your own key with set_claude_api_key.",
        "server_key_not_allowed": "This server has a Claude API key an administrator can let "
        "you use (they need your user id from account_status); or store your own key with "
        "set_claude_api_key.",
        "cap_reached_global": f"This server's monthly OCR budget is spent until {day} (UTC); "
        "your own key (set_claude_api_key) is not affected by it.",
        "cap_reached_user": f"Your monthly allowance on this server's key is spent until {day} "
        "(UTC); your own key (set_claude_api_key) is not affected by it.",
        "user_cap_reached": f"The monthly cap you set on your own key is spent until {day} "
        "(UTC); call set_claude_api_key again with a higher monthly_cap_usd to raise it.",
    }
    return f"{OCR_LAZY_NOTE} {more[reason]}" if reason in more else OCR_LAZY_NOTE


def ocr_summary(svc: Services, uid: str) -> dict[str, object]:
    """``account_status.ocr`` (§8.1, §11.1): the mode and why, this UTC month's spend, and the
    caps that apply to this user. Says whether a key is stored, never anything about it."""
    settings, store, ledger = svc.settings, svc.store, svc.ledger
    now = svc.clock()
    start, end = month_bounds(now)
    box = SecretBox(settings, uid)
    try:
        has_key = read_user_key(box) is not None  # presence only; the key is dropped here
        resolution = resolve_mode(settings, store, box, ledger, uid, now)
        mode, reason = resolution.mode, str(resolution.reason)
        del resolution
    except ScriptoriumError as exc:
        if exc.code != "internal":
            raise
        # resolve_mode() fails closed rather than spend the server key for this user (§8.1).
        mode, reason, has_key = "lazy", "key_unreadable", True
    own = ledger.month(uid=uid, mode="user", at=now)
    server = ledger.month(uid=uid, mode="server", at=now)
    allowance = ledger.allowance(settings, store, uid, now)
    row = store.user(uid)
    own_cap = row.ocr_cap_usd if row is not None else None
    permitted = (
        settings.anthropic_api_key is not None
        and row is not None
        and server_key_permitted(settings, row)
    )
    return {
        "mode": mode,
        "reason": reason,
        "own_key_stored": has_key,
        "month": start.strftime("%Y-%m"),
        "resets_at": iso_utc(end),
        "spend_this_month": {
            "usd": _usd(own.cost_usd + server.cost_usd),
            "pages": own.pages + server.pages,
            "own_key": {"usd": _usd(own.cost_usd), "pages": own.pages},
            "server_key": {"usd": _usd(server.cost_usd), "pages": server.pages},
        },
        "caps": {
            "own_key": (
                {"monthly_cap_usd": own_cap, "remaining_usd": _usd(allowance.own_remaining_usd)}
                if has_key
                else None
            ),
            "server_key": (
                {
                    "monthly_cap_usd": settings.ocr_user_monthly_cap_usd,
                    "remaining_usd": _usd(allowance.user_remaining_usd),
                    "server_budget_spent": allowance.global_remaining_usd <= 0,
                }
                if permitted
                else None
            ),
        },
        "note": _ocr_note(reason, iso_utc(end), own_cap),
    }


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
        "ocr": _ocr_summary(svc, account, account.uid),
        "quota": None,
        "worker": worker,
    }


def _account_status(svc: Services, user: User, acct: str | None) -> dict[str, object]:
    uid = svc.uid(user)
    rows = svc.store.accounts(uid)
    if not rows:
        return {
            "user": uid,
            "linked": False,
            "status": "not_linked",
            "message": (
                f"No reMarkable account is linked. Get a one-time code from {CONNECT_URL} "
                "and call connect_remarkable with it."
            ),
            "ocr": _ocr_summary(svc, None, uid),
        }
    account = open_account(svc, user, acct)
    payload = {"user": uid, **_status(svc, account)}
    payload["other_accounts"] = [
        {"account": r.acct, "active": r.active, "linked_at": iso_utc(r.linked_at)}
        for r in rows
        if r.acct != account.acct
    ]
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
]
