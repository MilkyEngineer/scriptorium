"""Which key, if any, transcribes a user's pages (Architecture §8.1).

Order: the user's own key (while their own cap has room) → the server key (if it is set, the
user may use it, and both server caps have room) → ``lazy``, where the user's Claude session
transcribes. :class:`Resolution` carries the key for the one call that needs it; it never
reaches a log, a tool result or an audit row, and callers drop it after the call.

Who may use the server key: a user an admin granted with ``admin_allow_server_key``
(``users.server_key_allowed``), or one listed in ``SERVER_KEY_USERS``. An entry there is
``*`` (everyone), a 16-character uid, or an email. An email entry matches only an email the
issuer verified: the server records the SHA-256 of that address (ASCII-lowered) at sign-in
while it is listed, and resolution compares it with the list as it is now, so removing an
entry revokes at once and the worker needs no token to decide.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal, get_args

from pydantic import SecretStr

from scriptorium.auth.oidc import ascii_lower
from scriptorium.config import API_KEY_RE
from scriptorium.errors import NotFoundError, ScriptoriumError

if TYPE_CHECKING:
    from scriptorium.auth.oidc import User
    from scriptorium.config import Settings
    from scriptorium.ocr.ledger import SpendLedger
    from scriptorium.users.secrets import SecretBox
    from scriptorium.users.store import GlobalStore, UserRow

__all__ = [
    "REASONS",
    "USER_KEY_SECRET",
    "Mode",
    "Reason",
    "Resolution",
    "email_digest",
    "listed_email_digest",
    "read_user_key",
    "resolve",
    "server_key_listed",
    "server_key_permitted",
]

Mode = Literal["user", "server", "lazy"]
Reason = Literal[
    "user_key",
    "server_key",
    "no_key",
    "server_key_not_allowed",
    "cap_reached_global",
    "cap_reached_user",
    "user_cap_reached",
]
REASONS: Final[frozenset[str]] = frozenset(get_args(Reason))

#: The SecretBox name of a user's own Claude API key (``set_claude_api_key``).
USER_KEY_SECRET: Final = "claude_api_key"  # noqa: S105 - a SecretBox entry name, not a secret
WILDCARD: Final = "*"


@dataclass(frozen=True)
class Resolution:
    """The mode for the next call, why, and (for ``user``/``server``) the key to make it with.

    ``reason``: ``user_key`` / ``server_key`` (a key applies); ``no_key`` (neither key exists);
    ``server_key_not_allowed`` (a server key exists, the user may not use it);
    ``cap_reached_global`` / ``cap_reached_user`` (a server cap is spent this month);
    ``user_cap_reached`` (the user's own cap on their own key is spent and no server key
    applies).
    """

    mode: Mode
    reason: Reason
    api_key: SecretStr | None = field(default=None, repr=False, compare=False)


def email_digest(email: str) -> str:
    """SHA-256 (hex) of an email, ASCII-lowered as everywhere emails are compared."""
    return hashlib.sha256(ascii_lower(email).encode("utf-8")).hexdigest()


def _listed_emails(settings: Settings) -> frozenset[str]:
    return frozenset(ascii_lower(e) for e in settings.server_key_users if "@" in e)


def listed_email_digest(settings: Settings, user: User) -> str | None:
    """The digest to record for ``user``: only a verified email that SERVER_KEY_USERS lists."""
    if not user.email or not user.email_verified:
        return None
    email = ascii_lower(user.email)
    return email_digest(email) if email in _listed_emails(settings) else None


def server_key_listed(settings: Settings, row: UserRow) -> bool:
    """Whether SERVER_KEY_USERS, as configured now, admits this user."""
    entries = settings.server_key_users
    if WILDCARD in entries:
        return True
    if row.uid in entries:  # an entry without "@" is a uid; it never matches a sub or email
        return True
    if row.server_key_email_sha256 is None:
        return False
    return row.server_key_email_sha256 in {email_digest(e) for e in _listed_emails(settings)}


def server_key_permitted(settings: Settings, row: UserRow) -> bool:
    """Granted by an admin or listed in SERVER_KEY_USERS (whether or not a key is set)."""
    return row.server_key_allowed or server_key_listed(settings, row)


def read_user_key(secrets: SecretBox) -> SecretStr | None:
    """The user's own key from their SecretBox, or None if they stored none.

    Raises ScriptoriumError (``internal``) if it cannot be decrypted or is not a key: the
    caller must not quietly fall back to the server key for a user who set their own.
    """
    raw = secrets.open(USER_KEY_SECRET)
    if raw is None:
        return None
    try:
        key = raw.decode("ascii")
    except UnicodeDecodeError:
        key = ""
    if not API_KEY_RE.fullmatch(key):
        raise ScriptoriumError(
            "the stored Claude API key cannot be read", hint="set it again with set_claude_api_key"
        )
    return SecretStr(key)


def resolve(
    settings: Settings,
    store: GlobalStore,
    secrets: SecretBox,
    ledger: SpendLedger,
    uid: str,
    now: datetime,
) -> Resolution:
    """The OCR mode for ``uid`` at ``now`` (§8.1). ``secrets`` is that user's SecretBox.

    Raises NotFoundError for an unknown uid, and ScriptoriumError (``internal``) when the
    user's stored key cannot be decrypted (rather than spending the server key instead).
    """
    row = store.user(uid)
    if row is None:
        raise NotFoundError("no such user")
    own_key = read_user_key(secrets)
    if own_key is not None:
        if not ledger.allowance(settings, store, uid, now, mode="user").degraded:
            return Resolution("user", "user_key", own_key)
    if settings.anthropic_api_key is None:
        return Resolution("lazy", "user_cap_reached" if own_key else "no_key")
    if not server_key_permitted(settings, row):
        return Resolution("lazy", "user_cap_reached" if own_key else "server_key_not_allowed")
    degraded = ledger.allowance(settings, store, uid, now, mode="server").degraded
    if degraded:
        if own_key is not None:
            return Resolution("lazy", "user_cap_reached")
        return Resolution(
            "lazy", "cap_reached_global" if degraded == "global" else "cap_reached_user"
        )
    return Resolution("server", "server_key", settings.anthropic_api_key)
