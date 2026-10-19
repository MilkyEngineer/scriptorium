"""Per-account reMarkable cloud client, read side (Architecture §4.3, §7, §14).

The only module that imports the fork (``remarkable_mcp.sync``). It takes from the fork
the protocol definitions (endpoints, the ``rm-filename`` header, index formats),
``register_device`` and, for the write path that arrives in M3, an explicit
``ClientConfig``. It does **not** read through the fork's ``RemarkableClient``:
``fetch_blob``/``read_root_entries`` buffer whole bodies before anything can bound them,
hard-code their timeouts, report HTTP status only inside exception text, and share one
thread-local ``requests.Session`` (pool and cookies) between every client in a thread.
Each ``CloudClient`` instead owns an ``httpx.Client``: per-account connection state, a
timeout from the caller, streamed reads with a byte cap and a transfer deadline, and a
typed exception per failure class of §7 step 7. There is no retry loop here; the worker
is the one retry layer.

Every string from the cloud is untrusted: root and index bodies are parsed strictly
(``protocol.py``), file names pass the allowlist before anyone can join them to a path,
blobs are verified against their SHA-256 while they stream, and tokens, names and bodies
never appear in a log line or an exception message.
"""

from __future__ import annotations

import hashlib
import io
import threading
import time
from collections.abc import Buffer, Iterator
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import BinaryIO, Final

import httpx
import jwt
from remarkable_mcp import sync as _fork

from scriptorium.cloud.errors import (
    BlobVerificationError,
    CloudNotFoundError,
    CloudProtocolError,
    CloudRequestError,
    RateLimitedError,
    ResponseTooLargeError,
    TokenRejectedError,
    TransientCloudError,
)
from scriptorium.cloud.protocol import (
    MAX_BLOB_BYTES,
    MAX_DOCUMENT_FILES,
    MAX_INDEX_BYTES,
    MAX_POINTER_BYTES,
    MAX_ROOT_ENTRIES,
    DocumentIndex,
    RootInfo,
    is_sha256,
    is_uuid,
    parse_document_index,
    parse_root,
    parse_root_pointer,
)
from scriptorium.errors import InvalidArgumentError, RefusedError

ROOT_URL: Final[str] = _fork.ROOT_URL
FILES_URL: Final[str] = _fork.FILES_URL
USER_TOKEN_URL: Final[str] = _fork.USER_TOKEN_URL

MAX_DEVICE_TOKEN_BYTES: Final = 16 * 1024
MIN_TRANSFER_RATE: Final = 64 * 1024
"""Bytes per second below which a transfer counts as stalled (slow-drip defence)."""
MAX_RETRY_AFTER_S: Final = 3600.0
_CHUNK: Final = 64 * 1024
_IDENTITY_CLAIM: Final = "auth0-userid"  # S0 step 6
_TOKEN_CHARS: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~+/="
)


def _looks_like_token(value: str, limit: int) -> bool:
    return 0 < len(value) <= limit and set(value) <= _TOKEN_CHARS


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds from a ``Retry-After`` header (delay or HTTP date), clamped; else None."""
    header = response.headers.get("retry-after")
    if header is None:
        return None
    try:
        seconds = float(header)
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            return None
        seconds = when.timestamp() - time.time()
    if seconds != seconds or seconds < 0:  # NaN or in the past
        return None
    return min(seconds, MAX_RETRY_AFTER_S)


def _require_https(request: httpx.Request) -> None:
    """Request hook: every request goes over HTTPS."""
    if request.url.scheme != "https":
        raise CloudRequestError("refused a request that is not HTTPS")


def _require_identity(response: httpx.Response, what: str) -> None:
    """Refuse a compressed body before reading it.

    httpx decodes any ``Content-Encoding`` (stacked ones too) a whole network chunk at a
    time, before a size cap can see the result, so a few kilobytes of gzip could expand
    to gigabytes. Every request asks for ``identity``; anything else is refused unread.
    """
    encoding = response.headers.get("content-encoding")
    if encoding is not None and encoding.strip().lower() not in ("", "identity"):
        raise CloudProtocolError(f"{what}: compressed response refused")


class _VerifiedStream(io.RawIOBase):
    """Bytes of one blob as they arrive, capped and checked against their SHA-256.

    The final read (end of stream) raises instead of returning EOF when the hash does
    not match, so a consumer that copies to EOF never sees a clean end on a bad blob.
    """

    def __init__(
        self, chunks: Iterator[bytes], sha256: str, max_bytes: int, deadline: float
    ) -> None:
        super().__init__()
        self._chunks = chunks
        self._expected = sha256
        self._max = max_bytes
        self._deadline = deadline
        self._hasher = hashlib.sha256()
        self._pending = b""
        self._count = 0
        self.verified = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        view = memoryview(buffer).cast("B")
        while not self._pending:
            if self.verified:
                return 0
            if time.monotonic() > self._deadline:
                raise TransientCloudError("blob transfer stalled")
            chunk = next(self._chunks, None)
            if chunk is None:
                if self._hasher.hexdigest() != self._expected:
                    raise BlobVerificationError("blob does not match its hash")
                self.verified = True
                return 0
            self._count += len(chunk)
            if self._count > self._max:
                raise ResponseTooLargeError(f"blob is larger than its {self._max} bytes")
            self._hasher.update(chunk)
            self._pending = chunk
        n = min(len(view), len(self._pending))
        view[:n] = self._pending[:n]
        self._pending = self._pending[n:]
        return n


class CloudClient:
    """Reads one reMarkable account through the sync 1.5 API. Never writes."""

    def __init__(
        self,
        device_token: str,
        *,
        cache_dir: Path,
        http_timeout_s: float = 30,
        transport: httpx.BaseTransport | None = None,
        max_index_bytes: int = MAX_INDEX_BYTES,
        max_root_entries: int = MAX_ROOT_ENTRIES,
        max_document_files: int = MAX_DOCUMENT_FILES,
    ) -> None:
        if not _looks_like_token(device_token, MAX_DEVICE_TOKEN_BYTES):
            raise InvalidArgumentError("the device token is not a token")
        if not 0 < http_timeout_s <= 600:
            raise InvalidArgumentError("http_timeout_s must be in (0, 600]")
        self._device_token = device_token
        self._timeout_s = float(http_timeout_s)
        self._max_index_bytes = min(max_index_bytes, MAX_INDEX_BYTES)
        self._max_root_entries = min(max_root_entries, MAX_ROOT_ENTRIES)
        self._max_document_files = min(max_document_files, MAX_DOCUMENT_FILES)
        # The fork client for the M3 write path is built from this config, never from
        # the environment: its own cache directory and its retry loop switched off,
        # because the worker is Scriptorium's one retry layer (§7).
        self.fork_config: Final = _fork.ClientConfig(
            cache_dir=cache_dir,
            cache_enabled=False,
            retry_attempts=1,
            retry_delay=0.0,
            sync_workers=1,
        )
        self._http = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(self._timeout_s),
            # Never follow a redirect: httpx would read and decode each hop's body itself,
            # unbounded, and a hop could aim a request at a host inside the network.
            follow_redirects=False,
            event_hooks={"request": [_require_https]},
            trust_env=False,  # no proxy or .netrc from the environment
            headers={"accept-encoding": "identity", "user-agent": "scriptorium"},
        )
        self._lock = threading.Lock()
        self._user_token: str | None = None
        self._last_root: RootInfo | None = None

    @classmethod
    def from_device_token(
        cls,
        token: str,
        *,
        cache_dir: Path,
        http_timeout_s: float = 30,
        transport: httpx.BaseTransport | None = None,
    ) -> CloudClient:
        return cls(token, cache_dir=cache_dir, http_timeout_s=http_timeout_s, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CloudClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __repr__(self) -> str:  # never the token
        return "CloudClient(<device token>)"

    # ---- identity and registration -----------------------------------------------------

    def account_identity(self) -> str | None:
        """The device token's ``auth0-userid`` claim (S0), or None.

        The token is HS256 under reMarkable's secret, so it is decoded without
        verification: the value only keys the one-account rule and is stored hashed.
        Never log it.
        """
        try:
            claims = jwt.decode(self._device_token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return None
        value = claims.get(_IDENTITY_CLAIM)
        if isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable():
            return value
        return None

    @staticmethod
    def register_device(code: str, description: str) -> str:
        """Exchange a one-time code for a device token: the only cloud write (E's).

        Wraps the fork's ``register_device``. Each call registers a new device on the
        account. The fork's own retry count still comes from ``REMARKABLE_RETRY_*``.
        """
        if not (code.isascii() and code.isalnum() and 4 <= len(code) <= 32):
            raise InvalidArgumentError("the one-time code is not a code")
        if not (description.isascii() and 0 < len(description) <= 64):
            raise InvalidArgumentError("the device description is not valid")
        if not all(c.isalnum() or c in "-_." for c in description):
            raise InvalidArgumentError("the device description is not valid")
        try:
            result = _fork.register_device(code, device_desc=description)
        except (RuntimeError, OSError):
            raise RefusedError(
                "reMarkable did not accept the code",
                hint="Codes expire after a few minutes and work once; get a new one.",
            ) from None
        token = result.get("devicetoken", "")
        if not isinstance(token, str) or not _looks_like_token(token, MAX_DEVICE_TOKEN_BYTES):
            raise RefusedError("reMarkable returned no device token")
        return token

    # ---- reads ---------------------------------------------------------------------------

    def root(self) -> RootInfo:
        """The current root (hash, generation, documents)."""
        with self._get(ROOT_URL, filename=None) as response:
            body = self._read(response, MAX_POINTER_BYTES, "root pointer")
        if not body.strip():
            raise TransientCloudError("empty root pointer")
        root_hash, generation = parse_root_pointer(body)
        last = self._last_root
        if last is not None and last.hash == root_hash:
            # Content-addressed: same hash, same entries.
            return RootInfo(root_hash, generation, last.entries, last.rejected)
        try:
            with self._get(f"{FILES_URL}/{root_hash}", filename="root.docSchema") as response:
                body = self._read(response, self._max_index_bytes, "root index")
        except CloudNotFoundError:
            raise TransientCloudError("root index not found yet", status=404) from None
        info = parse_root(
            body, root_hash=root_hash, generation=generation, max_entries=self._max_root_entries
        )
        self._last_root = info
        return info

    def document_index(self, uuid: str, hash: str) -> DocumentIndex:
        """A document's files: name -> (sha256, size), allowlisted names only."""
        if not is_uuid(uuid) or not is_sha256(hash):
            raise InvalidArgumentError("document_index needs a UUID and a SHA-256")
        with self._get(f"{FILES_URL}/{hash}", filename=f"{uuid}.docSchema") as response:
            body = self._read(response, self._max_index_bytes, "document index")
        return parse_document_index(body, uuid=uuid, hash=hash, max_files=self._max_document_files)

    @contextmanager
    def open_blob(
        self, sha256: str, *, max_bytes: int = MAX_BLOB_BYTES, filename: str | None = None
    ) -> Iterator[BinaryIO]:
        """Stream one blob, verified against ``sha256`` and capped at ``max_bytes``.

        ``max_bytes`` is the size the document index declares. The stream raises
        ``BlobVerificationError`` at its end if the content does not hash to
        ``sha256``, and ``ResponseTooLargeError`` as soon as it passes the cap.
        """
        if not is_sha256(sha256):
            raise InvalidArgumentError("open_blob needs a SHA-256")
        if not 0 <= max_bytes <= MAX_BLOB_BYTES:
            raise ResponseTooLargeError(f"blob is declared larger than {MAX_BLOB_BYTES} bytes")
        with self._get(f"{FILES_URL}/{sha256}", filename=filename) as response:
            self._check_length(response, max_bytes, "blob")
            raw = _VerifiedStream(
                response.iter_bytes(_CHUNK), sha256, max_bytes, self._deadline(max_bytes)
            )
            with io.BufferedReader(raw, _CHUNK) as stream:
                yield stream
                if not raw.verified:
                    # The consumer stopped before the end: finish, so the hash is checked.
                    while stream.read(_CHUNK):
                        pass

    # ---- HTTP -----------------------------------------------------------------------------

    def _deadline(self, size: int) -> float:
        return time.monotonic() + self._timeout_s + size / MIN_TRANSFER_RATE

    def user_token(self, *, renew: bool = False) -> str:
        """The current user token, exchanged from the device token if there is none (or,
        with ``renew``, always): what the notifications websocket authenticates with (§7).

        Blocking. Raises :class:`TokenRejectedError` on 401/403 from the token endpoint and
        :class:`TransientCloudError` for anything else. Never log the token or its claims:
        they carry the user's name and email.
        """
        with self._lock:
            if renew or self._user_token is None:
                self._user_token = self._renew()
            return self._user_token

    def _token(self) -> str:
        with self._lock:
            if self._user_token is None:
                self._user_token = self._renew()
            return self._user_token

    def _forget(self, token: str) -> None:
        with self._lock:
            if self._user_token == token:
                self._user_token = None

    def _renew(self) -> str:
        """Device token → user token. 401/403 here is the §7 "token rejected" class."""
        headers = {"authorization": f"Bearer {self._device_token}"}
        try:
            with self._http.stream("POST", USER_TOKEN_URL, headers=headers) as response:
                status = response.status_code
                if status in (401, 403):
                    raise TokenRejectedError("the device token was rejected", status=status)
                if status != 200:
                    # Not a rejection by §7's definition: back off and ask again later.
                    error = RateLimitedError if status == 429 else TransientCloudError
                    raise error(
                        f"token: HTTP {status}",
                        status=status,
                        retry_after_s=_retry_after(response),
                    )
                try:
                    _require_identity(response, "user token")
                except CloudProtocolError:  # not a rejection: back off
                    raise TransientCloudError(
                        "user token: compressed response refused", status=status
                    ) from None
                body = self._read(response, MAX_POINTER_BYTES, "user token")
        except httpx.HTTPError as exc:  # network, timeout, redirects: not a rejection
            raise TransientCloudError(f"token request failed ({type(exc).__name__})") from None
        token = body.decode("ascii", errors="replace").strip()
        if not _looks_like_token(token, MAX_POINTER_BYTES):
            raise TransientCloudError("the token endpoint returned no token")
        return token

    @contextmanager
    def _get(self, url: str, *, filename: str | None) -> Iterator[httpx.Response]:
        """One authenticated GET; a 401 renews the user token and retries once."""
        for attempt in range(2):
            token = self._token()
            headers = {"authorization": f"Bearer {token}"}
            if filename is not None:
                headers["rm-filename"] = filename
            try:
                with self._http.stream("GET", url, headers=headers) as response:
                    if response.status_code == 401 and attempt == 0:
                        self._forget(token)
                        continue
                    self._check_status(response, "sync")
                    _require_identity(response, "sync")
                    yield response
                    return
            except httpx.TransportError as exc:
                raise TransientCloudError(f"request failed ({type(exc).__name__})") from None
            except httpx.HTTPError as exc:  # a malformed response
                raise CloudProtocolError(f"request failed ({type(exc).__name__})") from None
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _check_status(response: httpx.Response, what: str) -> None:
        status = response.status_code
        if status == 200:
            return
        if status == 429:
            raise RateLimitedError(
                f"{what}: rate limited", status=status, retry_after_s=_retry_after(response)
            )
        if status >= 500 or status in (401, 403, 408):
            # 401/403 after a fresh user token: access, not the device token, is refused.
            raise TransientCloudError(
                f"{what}: HTTP {status}", status=status, retry_after_s=_retry_after(response)
            )
        if status in (404, 410):
            raise CloudNotFoundError(f"{what}: HTTP {status}", status=status)
        if 300 <= status < 400:
            raise CloudRequestError(f"{what}: redirect refused", status=status)
        raise CloudRequestError(f"{what}: HTTP {status}", status=status)

    @staticmethod
    def _check_length(response: httpx.Response, limit: int, what: str) -> None:
        """Refuse a body whose declared length is over ``limit`` before reading any of it.

        The header is the cloud's (attacker-controlled): only plain ASCII digits count, and
        they are compared by length before ``int`` (whose digit limit and Unicode digits,
        such as a Latin-1 superscript, would otherwise raise). Anything else is ignored; the
        streamed read enforces ``limit`` regardless.
        """
        declared = response.headers.get("content-length")
        if declared is None or not (declared.isascii() and declared.isdigit()):
            return
        digits = declared.lstrip("0")
        if len(digits) > len(str(limit)) or (digits and int(digits) > limit):
            raise ResponseTooLargeError(f"{what} is larger than {limit} bytes")

    def _read(self, response: httpx.Response, limit: int, what: str) -> bytes:
        """The whole body, refusing it as soon as it passes ``limit`` bytes."""
        self._check_length(response, limit, what)
        deadline = self._deadline(limit)
        out = bytearray()
        for chunk in response.iter_bytes(_CHUNK):
            out += chunk
            if len(out) > limit:
                raise ResponseTooLargeError(f"{what} is larger than {limit} bytes")
            if time.monotonic() > deadline:
                raise TransientCloudError(f"{what} transfer stalled")
        return bytes(out)
