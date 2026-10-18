"""Per-user secrets at rest: MultiFernet over ``SCRIPTORIUM_MASTER_KEYS`` (§6, §12, §14).

One file per user, ``users/<uid>/secrets.enc``: a small JSON object mapping a secret's name
(``device_token:<acct>``, ``claude_api_key``, ...) to a Fernet token. The file is written
atomically (temp file, ``fsync``, rename, directory ``fsync``) with mode 0600, under an
exclusive lock so the server and the worker cannot lose each other's updates.

Rotation: :meth:`SecretBox.seal` always encrypts with the first master key; :meth:`open`
accepts a token from any configured key. Each plaintext is bound to its user and name, so a
token copied into another user's file, or under another name, does not decrypt.

Nothing here logs, and no error message carries a name's value, a token or a key.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Final

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from scriptorium.errors import InvalidArgumentError, ScriptoriumError
from scriptorium.library.layout import resolve_inside
from scriptorium.users.store import user_dir

if sys.platform != "win32":
    import fcntl

if TYPE_CHECKING:
    from scriptorium.config import Settings

__all__ = ["SECRETS_FILE", "SecretBox"]

SECRETS_FILE: Final = "secrets.enc"
_LOCK_FILE: Final = ".secrets.lock"
_FORMAT_VERSION: Final = 1
_NAME_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_MAX_VALUE_BYTES: Final = 64 * 1024
_MAX_FILE_BYTES: Final = 1024 * 1024
_BINDING: Final = b"scriptorium-secret-v1\x00"
_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)


class SecretBox:
    """Encrypted name -> bytes store for one user (``users/<uid>/secrets.enc``)."""

    def __init__(self, settings: Settings, uid: str) -> None:
        self._uid = uid
        self._dir = user_dir(settings.data_dir, uid)
        self._path = self._dir / SECRETS_FILE
        resolve_inside(self._dir, self._path)
        self._fernet = MultiFernet([Fernet(key) for key in settings.master_key_list()])

    # ---- public API ----------------------------------------------------------------------

    def seal(self, name: str, value: bytes) -> None:
        """Store ``value`` under ``name``, encrypted with the first master key."""
        _check_name(name)
        if not isinstance(value, bytes) or len(value) > _MAX_VALUE_BYTES:
            raise InvalidArgumentError("secret value must be bytes of at most 64 KiB")
        token = self._fernet.encrypt(self._bind(name) + value).decode("ascii")
        with self._locked():
            entries = self._read()
            entries[name] = token
            self._write(entries)

    def open(self, name: str) -> bytes | None:
        """The secret stored under ``name``, or None if there is none.

        Raises :class:`ScriptoriumError` (``internal``) if the stored token does not decrypt
        with any configured master key (a key was removed, or the file was tampered with).
        """
        _check_name(name)
        token = self._read().get(name)
        if token is None:
            return None
        try:
            plain = self._fernet.decrypt(token.encode("ascii"))
        except (InvalidToken, UnicodeEncodeError):
            raise _undecryptable() from None
        prefix = self._bind(name)
        if not plain.startswith(prefix):
            raise _undecryptable()
        return plain[len(prefix) :]

    def forget(self, name: str) -> None:
        """Remove ``name``; a no-op if it is not stored."""
        _check_name(name)
        with self._locked():
            entries = self._read()
            if entries.pop(name, None) is not None:
                self._write(entries)

    # ---- file handling -------------------------------------------------------------------

    def _bind(self, name: str) -> bytes:
        return _BINDING + self._uid.encode("ascii") + b"\x00" + name.encode("ascii") + b"\x00"

    def _read(self) -> dict[str, str]:
        try:
            fd = os.open(self._path, os.O_RDONLY | _NOFOLLOW)
        except FileNotFoundError:
            return {}
        except OSError:  # e.g. ELOOP: a symlink planted in place of the file
            raise ScriptoriumError("the secrets file cannot be opened") from None
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(_MAX_FILE_BYTES + 1)
        if len(raw) > _MAX_FILE_BYTES:
            raise ScriptoriumError("the secrets file is too large")
        try:
            doc = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ScriptoriumError("the secrets file is unreadable") from None
        entries = doc.get("secrets") if isinstance(doc, dict) else None
        if (
            not isinstance(doc, dict)
            or doc.get("v") != _FORMAT_VERSION
            or not isinstance(entries, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in entries.items())
        ):
            raise ScriptoriumError("the secrets file has an unknown format")
        return dict(entries)

    def _write(self, entries: dict[str, str]) -> None:
        data = json.dumps(
            {"v": _FORMAT_VERSION, "secrets": dict(sorted(entries.items()))},
            separators=(",", ":"),
        ).encode("ascii")
        tmp = self._dir / f".{SECRETS_FILE}.{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fchmod(fh.fileno(), 0o600)
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        _fsync_dir(self._dir)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self._dir / _LOCK_FILE, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)
        try:
            if sys.platform != "win32":
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the lock


def _check_name(name: str) -> None:
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        raise InvalidArgumentError("secret name is malformed")


def _undecryptable() -> ScriptoriumError:
    return ScriptoriumError(
        "a stored secret cannot be decrypted with the configured master keys",
        hint="was a master key removed from SCRIPTORIUM_MASTER_KEYS?",
    )


def _fsync_dir(path: Path) -> None:
    if sys.platform == "win32":  # directories cannot be opened for fsync there
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
