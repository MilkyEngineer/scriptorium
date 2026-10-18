"""The OCR numbers the account and OCR tools share (§8.1, §11.1, §11.3).

``account_status.ocr``, ``ocr_status``, ``set_claude_api_key``, ``clear_claude_api_key``,
``transcribe``, ``read_page`` and ``set_auto_ocr`` all take the mode, the spend and the caps
from here, through the process's one set of OCR ports (``Services.ports``), so every tool
reports the same numbers the worker spends against. Nothing here returns or logs a key: the
key ``resolve`` hands over is dropped where it is received.
"""

from __future__ import annotations

from typing import Final

from scriptorium.errors import ScriptoriumError
from scriptorium.jobs.kinds import OCR_KINDS
from scriptorium.ocr.ledger import month_bounds
from scriptorium.ocr.modes import read_user_key, server_key_permitted
from scriptorium.ocr.pipeline import LAZY, SERVER_MODES, clear_key_refusal, resolve_mode
from scriptorium.tools._conventions import iso_utc
from scriptorium.tools._library import Account
from scriptorium.tools._services import Services
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import account_paths

__all__ = [
    "OCR_LAZY_NOTE",
    "OCR_SERVER_NOTE",
    "account_ocr",
    "forget_key_refusals",
    "ocr_mode",
    "ocr_queue",
    "ocr_summary",
]

OCR_LAZY_NOTE: Final = (
    "Pages are transcribed by your own Claude session: read_page returns a page's image "
    "with instructions, and save_transcript stores the result. With your own Claude API key "
    "(or the server's, if the operator allows it) pages are transcribed in the background."
)
OCR_SERVER_NOTE: Final = (
    "Pages are transcribed in the background; read_page waits briefly for a page's "
    "transcript and otherwise answers pending. ocr_status shows the queue and the spend."
)


def ocr_mode(svc: Services, uid: str) -> tuple[str, str]:
    """``(mode, reason)`` for ``uid`` now; the key ``resolve`` returns is dropped here."""
    choice = resolve_mode(svc.ports, svc.settings, svc.store, uid, svc.clock())
    mode, reason = choice.mode, choice.reason
    del choice
    return (mode, reason) if mode in SERVER_MODES else (LAZY, reason)


def ocr_queue(svc: Services, account: Account) -> dict[str, dict[str, int]]:
    """``{kind: {queued, running, retrying, done, dead}}`` for the account's OCR jobs."""
    counts = svc.queue.counts(account.uid, acct=account.acct)
    out: dict[str, dict[str, int]] = {}
    for kind in OCR_KINDS:
        by_state = counts.get(kind, {})
        out[kind] = {
            "queued": by_state.get("queued", 0),
            "running": by_state.get("leased", 0),
            "retrying": by_state.get("failed", 0),
            "done": by_state.get("done", 0),
            "dead": by_state.get("dead", 0),
        }
    return out


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
    settings, store, ports = svc.settings, svc.store, svc.ports
    ledger = ports.ledger
    now = svc.clock()
    start, end = month_bounds(now)
    # resolve_mode() fails closed (lazy, key_unreadable) rather than spend the server key
    # for a user whose own key cannot be decrypted (§8.1).
    mode, reason = ocr_mode(svc, uid)
    try:
        has_key = read_user_key(SecretBox(settings, uid)) is not None  # presence only
    except ScriptoriumError as exc:
        if exc.code != "internal":
            raise
        has_key = True
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


def account_ocr(svc: Services, account: Account | None, uid: str) -> dict[str, object]:
    """F's key/cap summary plus G's queue numbers (Architecture §11.1 ``account_status.ocr``)."""
    summary = ocr_summary(svc, uid)
    if summary.get("mode") in SERVER_MODES:
        summary["note"] = OCR_SERVER_NOTE
    if account is not None:
        summary["queue"] = ocr_queue(svc, account)
    return summary


def forget_key_refusals(svc: Services, uid: str) -> None:
    """Drop every account's note that the user's own key was refused (a new key, or none)."""
    for row in svc.store.accounts(uid):
        if account_paths(svc.settings.data_dir, uid, row.acct).index.is_file():
            clear_key_refusal(svc.index(uid, row.acct), "user")
