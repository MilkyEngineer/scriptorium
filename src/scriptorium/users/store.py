"""Where a user's data lives, and the global tables users / accounts / audit (§6, §6.2).

This module is the only place that turns an identity into a filesystem path (CLAUDE.md,
tenancy). A user's directory is named by ``uid``, the first 16 hex characters of
``User.uid_hash``; an account's by ``acct``, a ULID minted when the account is linked. Both
are validated by full-string regex before they touch a path, and every path handed out is
asserted to stay inside its root with symlinks resolved (:meth:`AccountPaths.inside`).

:class:`GlobalStore` owns three tables of ``scriptorium.sqlite``; their schema is in
``db/schema_global.py`` (``ocr_spend``, in the same file, belongs to ``ocr/ledger.py``). Audit
rows carry only allowlisted,
shape-checked identifiers (:data:`AUDIT_IDENTIFIER_KEYS`), never arguments or content.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from scriptorium.db import migrations
from scriptorium.db.connect import open_sqlite
from scriptorium.db.schema_global import SCRIPTORIUM_STEPS
from scriptorium.errors import InvalidArgumentError, NotFoundError, RefusedError
from scriptorium.library.layout import is_uuid, resolve_inside

if TYPE_CHECKING:
    from scriptorium.auth.oidc import User

__all__ = [
    "ACCT_RE",
    "AUDIT_IDENTIFIER_KEYS",
    "UID_RE",
    "AccountPaths",
    "AccountRow",
    "GlobalStore",
    "UserRow",
    "account_paths",
    "global_db_path",
    "new_ulid",
    "uid_for",
    "user_dir",
    "worker_heartbeat_path",
    "worker_lock_path",
]

#: A user's directory name: ``uid_hash[:16]``.
UID_RE: Final = re.compile(r"[0-9a-f]{16}")
#: An account's directory name: a ULID in Crockford base32 (no I, L, O, U).
ACCT_RE: Final = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_UID_HASH_RE: Final = re.compile(r"[0-9a-f]{64}")
_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _check_uid(uid: str) -> str:
    if not isinstance(uid, str) or UID_RE.fullmatch(uid) is None:
        raise InvalidArgumentError("user id is malformed")
    return uid


def _check_acct(acct: str) -> str:
    if not isinstance(acct, str) or ACCT_RE.fullmatch(acct) is None:
        raise InvalidArgumentError("account id is malformed", hint="an account id is 26 characters")
    return acct


def uid_for(uid_hash: str) -> str:
    """The directory name of the user with this ``User.uid_hash`` (§6)."""
    if not isinstance(uid_hash, str) or _UID_HASH_RE.fullmatch(uid_hash) is None:
        raise InvalidArgumentError("user hash is malformed")
    return uid_hash[:16]


# ---- paths -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountPaths:
    """The directories of one linked reMarkable account (§6)."""

    root: Path  # <data>/users/<uid>/accounts/<acct>
    blobs: Path
    library: Path
    tombstones: Path
    pages: Path
    staging: Path
    index: Path  # root / "index.sqlite"

    def inside(self, p: Path) -> Path:
        """``p`` resolved (symlinks followed), asserted inside :attr:`root`.

        Raises :class:`~scriptorium.errors.PathEscapeError` on any escape, a relative path or
        a NUL byte.
        """
        return resolve_inside(self.root, p)


def global_db_path(data_dir: Path) -> Path:
    """``<data>/scriptorium.sqlite``."""
    return Path(os.path.abspath(data_dir)) / "scriptorium.sqlite"


def worker_heartbeat_path(data_dir: Path) -> Path:
    """``<data>/worker.heartbeat``: the worker's liveness file (its healthcheck reads it)."""
    return Path(os.path.abspath(data_dir)) / "worker.heartbeat"


def worker_lock_path(data_dir: Path) -> Path:
    """``<data>/worker.lock``: held (``flock``) by the one worker process running on ``/data``."""
    return Path(os.path.abspath(data_dir)) / "worker.lock"


def user_dir(data_dir: Path, uid: str) -> Path:
    """``<data>/users/<uid>``; ``uid`` must match :data:`UID_RE`."""
    base = Path(os.path.abspath(data_dir)) / "users"
    path = base / _check_uid(uid)
    resolve_inside(base, path)
    return path


def account_paths(data_dir: Path, uid: str, acct: str) -> AccountPaths:
    """The paths of account ``acct`` of user ``uid``; both are validated. Creates nothing."""
    accounts = user_dir(data_dir, uid) / "accounts"
    root = accounts / _check_acct(acct)
    resolve_inside(accounts, root)
    return AccountPaths(
        root=root,
        blobs=root / "blobs",
        library=root / "library",
        tombstones=root / "tombstones",
        pages=root / "pages",
        staging=root / "staging",
        index=root / "index.sqlite",
    )


def new_ulid(now_ms: int | None = None) -> str:
    """A fresh ULID: 48-bit millisecond time and 80 random bits, Crockford base32."""
    ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    if not 0 <= ms < 1 << 48:
        raise ValueError("timestamp out of ULID range")
    value = (ms << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


# ---- global tables ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserRow:
    uid: str
    iss: str
    sub: str
    display_name: str
    created_at: datetime
    disabled_at: datetime | None
    quota_gb: float | None
    server_key_allowed: bool
    ocr_cap_usd: float | None = None
    """The user's own monthly cap on their own Claude key (§8.1); None = no cap."""
    server_key_email_sha256: str | None = field(default=None, repr=False)
    """SHA-256 of the verified email, kept only while it is listed in SERVER_KEY_USERS."""


@dataclass(frozen=True)
class AccountRow:
    acct: str
    uid: str
    rm_identity: str | None
    active: bool
    linked_at: datetime
    device_desc: str
    removed_at: datetime | None = None
    """Set once ``disconnect_remarkable(keep_archive=false)`` moved the account's directory to
    ``users/<uid>/removed/``: the account is never opened, polled or relinked again."""


_TOOL_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_OUTCOME_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
_HOST_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}|\[[0-9A-Fa-f:.]{2,45}\]")
_SNAPSHOT_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}")
_MAX_LIST: Final = 100
_USER_UPDATES: Final[Mapping[str, str]] = {
    "server_key_allowed": "UPDATE users SET server_key_allowed = ? WHERE uid = ?",
    "ocr_cap_usd": "UPDATE users SET ocr_cap_usd = ? WHERE uid = ?",
    "server_key_email_sha256": "UPDATE users SET server_key_email_sha256 = ? WHERE uid = ?",
}
_ACCOUNT_COLUMNS: Final = (
    "acct, uid, rm_account_identity, active, linked_at, device_desc, removed_at"
)
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
_CONFIRMATION_KEEP_SECONDS: Final = 2 * 600
_MAX_DEVICE_DESC: Final = 128
_MAX_IDENTITY: Final = 256


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _list_of(check: Callable[[object], bool]) -> Callable[[object], bool]:
    def ok(value: object) -> bool:
        return (
            isinstance(value, list | tuple)
            and len(value) <= _MAX_LIST
            and all(check(v) for v in value)
        )

    return ok


def _matches(pattern: re.Pattern[str]) -> Callable[[object], bool]:
    return lambda value: isinstance(value, str) and pattern.fullmatch(value) is not None


#: Audit identifiers: the only keys an audit row may carry, each with the shape its value must
#: have. Shapes are tight on purpose (a UUID, a ULID, an integer, a host name), so a document
#: name, a transcript or a token cannot pass as an identifier (Architecture §6.2, M1 #11).
AUDIT_IDENTIFIER_KEYS: Final[Mapping[str, Callable[[object], bool]]] = {
    "acct": _matches(ACCT_RE),
    "uuid": is_uuid,
    "uuids": _list_of(is_uuid),
    "page": _is_int,
    "pages": _list_of(_is_int),
    "job": _is_int,
    "bytes": _is_int,
    "count": _is_int,
    "generation": _is_int,
    "host": _matches(_HOST_RE),
    "snapshot": _matches(_SNAPSHOT_RE),
    # The target of an admin tool: a 16-character uid, never an email or a sub.
    "user": _matches(UID_RE),
    # What an admin grant or withdrawal set (admin_allow_server_key); a flag, nothing else.
    "allowed": lambda value: isinstance(value, bool),
    "reason_chars": _is_int,  # an administrator's free-text reason is audited by length only
}


def _audit_identifiers(identifiers: Mapping[str, Any]) -> str:
    """The identifiers as canonical JSON; raises ValueError on any key or value off the list.

    Like ``log_event``, this fails loudly: a caller passing something else is a bug to fix,
    not data to store. The message names the key, never the value.
    """
    for key, value in identifiers.items():
        check = AUDIT_IDENTIFIER_KEYS.get(key)
        if check is None:
            raise ValueError(f"audit identifier not allowed: {key!r}")
        if value is not None and not check(value):
            raise ValueError(f"audit identifier {key!r} does not have the expected shape")
    clean = {k: list(v) if isinstance(v, tuple) else v for k, v in identifiers.items()}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


def _iso(at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_ts(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GlobalStore:
    """Users, linked accounts and the audit log in ``scriptorium.sqlite`` (§6.2).

    Every method opens a short-lived connection (WAL, ``synchronous=NORMAL``,
    ``busy_timeout=5000``, foreign keys on), so one instance may be shared by the server's
    thread pool. Writes run in ``BEGIN IMMEDIATE`` transactions.
    """

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self._path = Path(path)
        self._clock = clock or _utcnow

    @classmethod
    def open(
        cls, path: Path, *, migrate: bool, clock: Callable[[], datetime] | None = None
    ) -> GlobalStore:
        """Open the global database; ``migrate=True`` (the server only) applies migrations.

        Without ``migrate`` a database at another schema version raises
        :class:`~scriptorium.db.migrations.SchemaMismatch`.
        """
        store = cls(path, clock=clock)
        if not migrate and not store._path.is_file():
            raise migrations.SchemaMismatch(0, len(SCRIPTORIUM_STEPS))
        with store._connect() as conn:
            if migrate:
                migrations.migrate(conn, SCRIPTORIUM_STEPS)
            else:
                migrations.check_version(conn, SCRIPTORIUM_STEPS)
        return store

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = open_sqlite(self._path)  # the §6.1 pragmas, WAL verified
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # ---- users ---------------------------------------------------------------------------

    def ensure_user(self, user: User) -> UserRow:
        """The user's row, created on first sight; the display name follows the token.

        Refuses if another ``(iss, sub)`` already owns this ``uid`` (a 64-bit prefix
        collision), because the two would share one storage directory.
        """
        uid = uid_for(user.uid_hash)
        with self._write() as conn:
            row = conn.execute(
                "SELECT iss, sub, display_name FROM users WHERE uid = ?", (uid,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO users (uid, iss, sub, display_name, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (uid, user.iss, user.sub, user.display_name, _iso(self._clock())),
                )
            elif (row[0], row[1]) != (user.iss, user.sub):
                raise RefusedError("this identity cannot be given storage", hint="ask the operator")
            elif row[2] != user.display_name:
                conn.execute(
                    "UPDATE users SET display_name = ? WHERE uid = ?", (user.display_name, uid)
                )
            out = self._user(conn, uid)
        assert out is not None
        return out

    def user(self, uid: str) -> UserRow | None:
        """The row of ``uid``, or None."""
        with self._connect() as conn:
            return self._user(conn, _check_uid(uid))

    @staticmethod
    def _user(conn: sqlite3.Connection, uid: str) -> UserRow | None:
        row = conn.execute(
            "SELECT uid, iss, sub, display_name, created_at, disabled_at, quota_gb, "
            "server_key_allowed, ocr_cap_usd, server_key_email_sha256 FROM users WHERE uid = ?",
            (uid,),
        ).fetchone()
        if row is None:
            return None
        created = _parse_ts(row[4])
        assert created is not None
        return UserRow(
            uid=row[0],
            iss=row[1],
            sub=row[2],
            display_name=row[3],
            created_at=created,
            disabled_at=_parse_ts(row[5]),
            quota_gb=row[6],
            server_key_allowed=bool(row[7]),
            ocr_cap_usd=row[8],
            server_key_email_sha256=row[9],
        )

    def _update_user(self, uid: str, column: str, value: object) -> None:
        sql = _USER_UPDATES[column]  # fixed statements: a column name never comes from input
        with self._write() as conn:
            cur = conn.execute(sql, (value, uid))
            if cur.rowcount == 0:
                raise NotFoundError("no such user", hint="the user must have signed in once")

    def set_server_key_allowed(self, uid: str, allowed: bool) -> None:
        """Grant or withdraw the server's Claude key for ``uid`` (``admin_allow_server_key``).

        Raises NotFoundError for a uid that has never signed in.
        """
        if not isinstance(allowed, bool):
            raise InvalidArgumentError("allowed must be true or false")
        self._update_user(_check_uid(uid), "server_key_allowed", int(allowed))

    def set_user_ocr_cap(self, uid: str, cap_usd: float | None) -> None:
        """Set (or clear, with None) the user's own monthly cap on their own key (§8.1)."""
        if cap_usd is not None and (
            isinstance(cap_usd, bool)
            or not isinstance(cap_usd, int | float)
            or not math.isfinite(cap_usd)
            or cap_usd < 0
        ):
            raise InvalidArgumentError("the monthly cap must be a non-negative number of USD")
        value = None if cap_usd is None else float(cap_usd)
        self._update_user(_check_uid(uid), "ocr_cap_usd", value)

    def set_server_key_email(self, uid: str, digest: str | None) -> None:
        """Record (or clear) the SHA-256 of the user's listed email (§8.1, SERVER_KEY_USERS).

        Only the digest of an email that SERVER_KEY_USERS lists is ever stored; the worker
        compares it with the digests of the list's current entries, so an email removed from
        the list stops matching at once.
        """
        if digest is not None and (
            not isinstance(digest, str) or _UID_HASH_RE.fullmatch(digest) is None
        ):
            raise InvalidArgumentError("the email digest must be 64 hex characters")
        self._update_user(_check_uid(uid), "server_key_email_sha256", digest)

    # ---- accounts ------------------------------------------------------------------------

    def accounts(self, uid: str) -> list[AccountRow]:
        """Every account the user ever linked, oldest first (inactive ones included)."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE uid = ? "  # noqa: S608
                "ORDER BY linked_at, acct",
                (_check_uid(uid),),
            ).fetchall()
        return [_account_row(r) for r in rows]

    def account(self, uid: str, acct: str) -> AccountRow | None:
        """Account ``acct`` if it belongs to ``uid`` (active or not), else None."""
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE uid = ? AND acct = ?",  # noqa: S608
                (_check_uid(uid), _check_acct(acct)),
            ).fetchone()
        return None if row is None else _account_row(row)

    def active_accounts(self) -> list[AccountRow]:
        """Every active account of a user who is not disabled: what the worker polls."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.acct, a.uid, a.rm_account_identity, a.active, a.linked_at, "
                "a.device_desc, a.removed_at FROM accounts a JOIN users u ON u.uid = a.uid "
                "WHERE a.active = 1 AND a.removed_at IS NULL AND u.disabled_at IS NULL "
                "ORDER BY a.linked_at, a.acct"
            ).fetchall()
        return [_account_row(r) for r in rows]

    def all_accounts(self) -> list[AccountRow]:
        """Every account ever linked, by anyone (the server migrates their indexes at start);
        removed ones included, with ``removed_at`` set."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM accounts ORDER BY linked_at, acct"  # noqa: S608
            ).fetchall()
        return [_account_row(r) for r in rows]

    def link_account(
        self, uid: str, rm_identity: str | None, device_desc: str, *, replace: bool = False
    ) -> AccountRow:
        """Record a newly registered device; returns the account it belongs to.

        - The same reMarkable account (``rm_identity``) linked again by the same user reuses
          its ``acct`` (one directory per reMarkable account, §6) and reactivates it.
        - Otherwise a new ``acct`` ULID is minted.
        - Refuses (``refused``) when ``rm_identity`` is actively linked to another user, or
          when the user already has a different active account and ``replace`` is false.
          With ``replace=True`` (``connect_remarkable(replace=true)``, §11.1) that account is
          deactivated in the same transaction, after the other-user check, so a refused link
          leaves the old account active. With ``rm_identity=None`` (identity not derivable,
          S0) a relink always gets a new ``acct``.
        """
        _check_uid(uid)
        if rm_identity is not None and (
            not isinstance(rm_identity, str)
            or not rm_identity
            or len(rm_identity) > _MAX_IDENTITY
            or not rm_identity.isprintable()
        ):
            raise InvalidArgumentError("account identity is malformed")
        if (
            not isinstance(device_desc, str)
            or len(device_desc) > _MAX_DEVICE_DESC
            or not device_desc.isprintable()
        ):
            raise InvalidArgumentError("device description is malformed")
        now = _iso(self._clock())
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM users WHERE uid = ?", (uid,)).fetchone() is None:
                raise NotFoundError("unknown user")
            if rm_identity is not None:
                other = conn.execute(
                    "SELECT 1 FROM accounts WHERE rm_account_identity = ? AND uid != ? "
                    "AND active = 1",
                    (rm_identity, uid),
                ).fetchone()
                if other is not None:
                    raise RefusedError(
                        "this reMarkable account is linked to another user",
                        hint="it must be disconnected there first",
                    )
            active = conn.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE uid = ? AND active = 1",  # noqa: S608
                (uid,),
            ).fetchone()
            reuse = None
            if rm_identity is not None:
                # A removed account's directory is in removed/: relinking gets a new one.
                reuse = conn.execute(
                    f"SELECT {_ACCOUNT_COLUMNS} FROM accounts "  # noqa: S608
                    "WHERE uid = ? AND rm_account_identity = ? AND removed_at IS NULL "
                    "ORDER BY linked_at DESC LIMIT 1",
                    (uid, rm_identity),
                ).fetchone()
            if active is not None and (reuse is None or reuse[0] != active[0]):
                if not replace:
                    raise RefusedError(
                        "a reMarkable account is already linked",
                        hint="disconnect it first, or connect with replace=true",
                    )
                conn.execute("UPDATE accounts SET active = 0 WHERE acct = ?", (active[0],))
            if reuse is not None:
                acct = str(reuse[0])
                conn.execute(
                    "UPDATE accounts SET active = 1, device_desc = ?, linked_at = ? WHERE acct = ?",
                    (device_desc, now, acct),
                )
            else:
                acct = new_ulid()
                conn.execute(
                    "INSERT INTO accounts (acct, uid, rm_account_identity, active, linked_at, "
                    "device_desc) VALUES (?, ?, ?, 1, ?, ?)",
                    (acct, uid, rm_identity, now, device_desc),
                )
            row = conn.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE acct = ?",  # noqa: S608
                (acct,),
            ).fetchone()
        return _account_row(row)

    def deactivate_account(self, uid: str, acct: str) -> None:
        """Mark the account inactive; its directory stays and remains readable (§6)."""
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE accounts SET active = 0 WHERE uid = ? AND acct = ?",
                (_check_uid(uid), _check_acct(acct)),
            )
            if cur.rowcount == 0:
                raise NotFoundError("no such account")

    def mark_removed(self, uid: str, acct: str, *, at: datetime) -> None:
        """Deactivate the account and record that its directory is being moved to
        ``removed/`` (``disconnect_remarkable(keep_archive=false)``). Set before the move, so
        no reader recreates the directory meanwhile; :meth:`unmark_removed` undoes it if the
        move fails."""
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE accounts SET active = 0, removed_at = ? "
                "WHERE uid = ? AND acct = ? AND removed_at IS NULL",
                (_iso(at), _check_uid(uid), _check_acct(acct)),
            )
            if cur.rowcount == 0:
                raise NotFoundError("no such account")

    def unmark_removed(self, uid: str, acct: str) -> None:
        """Clear ``removed_at`` after a move that did not happen (the account stays inactive)."""
        with self._write() as conn:
            conn.execute(
                "UPDATE accounts SET removed_at = NULL WHERE uid = ? AND acct = ?",
                (_check_uid(uid), _check_acct(acct)),
            )

    # ---- confirmation tokens -------------------------------------------------------------

    def spend_confirmation(self, digest: str, *, now: datetime) -> bool:
        """Record an executed confirmation token (§11.0); False if it was recorded before.

        ``digest`` is the SHA-256 of the token (``tools._conventions.confirmation_digest``).
        Rows older than twice the token lifetime are pruned in the same transaction: such a
        token fails its expiry check before it gets here.
        """
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise InvalidArgumentError("confirmation digest is malformed")
        stamp = _iso(now)
        horizon = _iso(now - timedelta(seconds=_CONFIRMATION_KEEP_SECONDS))
        with self._write() as conn:
            conn.execute("DELETE FROM confirmations WHERE used_at < ?", (horizon,))
            cur = conn.execute(
                "INSERT INTO confirmations (sha256, used_at) VALUES (?, ?) "
                "ON CONFLICT (sha256) DO NOTHING",
                (digest, stamp),
            )
            return cur.rowcount == 1

    # ---- audit ---------------------------------------------------------------------------

    def audit(
        self,
        *,
        uid_hash: str,
        tool: str,
        duration_ms: float,
        outcome: str,
        identifiers: Mapping[str, Any],
    ) -> None:
        """Append one audit row (§11.0): who, which tool, how long, how it ended, and ids.

        ``uid_hash`` is ``User.uid_hash`` (stored as the 16-character ``uid``; a 16-character
        ``uid`` is accepted too). ``tool`` and ``outcome`` are snake_case names;
        ``identifiers`` may only use :data:`AUDIT_IDENTIFIER_KEYS`. Anything else raises
        ValueError, so a tool argument can never end up in the log.
        """
        uid = uid_hash if UID_RE.fullmatch(uid_hash or "") else uid_for(uid_hash)
        if not _TOOL_RE.fullmatch(tool or ""):
            raise ValueError("audit tool name is not a snake_case identifier")
        if not _OUTCOME_RE.fullmatch(outcome or ""):
            raise ValueError("audit outcome is not a snake_case identifier")
        if not isinstance(duration_ms, int | float) or not 0 <= duration_ms < float("inf"):
            raise ValueError("audit duration must be a finite non-negative number")
        payload = _audit_identifiers(identifiers)
        with self._write() as conn:
            conn.execute(
                "INSERT INTO audit (ts, uid, tool, duration_ms, outcome, identifiers) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_iso(self._clock()), uid, tool, float(duration_ms), outcome, payload),
            )


def _account_row(row: sqlite3.Row) -> AccountRow:
    linked = _parse_ts(row[4])
    assert linked is not None
    return AccountRow(
        acct=row[0],
        uid=row[1],
        rm_identity=row[2],
        active=bool(row[3]),
        linked_at=linked,
        device_desc=row[5],
        removed_at=_parse_ts(row[6]),
    )
