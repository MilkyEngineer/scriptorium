"""Issuer discovery, JWKS cache and JWT validation (Architecture §5.1, §5.2).

Validation is deliberately narrow: algorithms pinned to RS256/ES256 (and to the
key's own type), ``iss`` byte-exact, ``aud`` membership, ``exp`` required,
``exp``/``nbf`` with 60 s leeway. Unknown ``kid`` values cause at most one JWKS
refetch per cooldown window shared by all requests, so a stream of junk tokens
cannot turn the server into a request amplifier against the issuer.

Metadata and the JWKS are read with a size cap enforced while streaming, and the
``jwks_uri`` must share the issuer's scheme, host and port.

Nothing here logs a token, an email or raw claims.
"""

from __future__ import annotations

import hashlib
import json
import logging
import string
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit

import anyio
import httpx
import jwt
from jwt.exceptions import (
    ExpiredSignatureError,
    ImmatureSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidKeyError,
    InvalidSignatureError,
    InvalidTokenError,
    MissingRequiredClaimError,
    PyJWKError,
)

from scriptorium.config import Settings
from scriptorium.logging import log_event, uid_prefix

logger = logging.getLogger(__name__)

ALLOWED_ALGORITHMS: Final = ("RS256", "ES256")
LEEWAY_SECONDS: Final = 60
JWKS_REFETCH_COOLDOWN_SECONDS: Final = 60.0
DISCOVERY_RETRY_SECONDS: Final = 10.0
HTTP_TIMEOUT_SECONDS: Final = 10.0
MAX_TOKEN_BYTES: Final = 16 * 1024
MAX_METADATA_BYTES: Final = 512 * 1024


# ---- errors ---------------------------------------------------------------------------


class AuthError(Exception):
    """Base class; ``reason`` is a short, log-safe code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TokenInvalid(AuthError):  # noqa: N818 - names fixed by the auth contract
    """Signature, structure or claims are wrong; the client must get a new token."""


class TokenExpired(TokenInvalid):
    """``exp`` is in the past (beyond leeway); the client should refresh."""


class UserNotAllowed(AuthError):  # noqa: N818 - names fixed by the auth contract
    """The token is valid but its subject is not on the allowlist."""


class InsufficientScope(AuthError):  # noqa: N818 - names fixed by the auth contract
    """The token is valid but lacks a required scope."""


class DiscoveryError(AuthError):
    """The issuer's metadata or JWKS could not be fetched or did not validate."""


# ---- the user ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class User:
    """An authenticated identity, keyed on ``(iss, sub)`` (§5.1)."""

    iss: str
    sub: str
    uid_hash: str
    display_name: str
    email: str | None = field(repr=False)
    email_verified: bool
    is_admin: bool
    scopes: frozenset[str]

    @property
    def log_uid(self) -> str:
        return uid_prefix(self.uid_hash)


def uid_hash_for(iss: str, sub: str) -> str:
    """SHA-256 of the issuer, a newline and the user claim, in hex (Architecture §6).

    The storage directory is the first 16 hex characters. The issuer cannot contain
    a newline (settings reject whitespace in URLs), so the pair is unambiguous.
    ``sub`` is hashed exactly as given: the only normalisation anywhere is
    :func:`ascii_lower` on an email used as the identity.
    """
    return hashlib.sha256((iss + "\n" + sub).encode()).hexdigest()


_ASCII_LOWER: Final = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def ascii_lower(value: str) -> str:
    """``value`` with A-Z lowered and every other character left byte-for-byte.

    Used wherever emails are compared or become identities. ``str.lower`` would
    apply Unicode case mapping (U+212A KELVIN SIGN lowers to ASCII ``k``), merging
    addresses the issuer treats as distinct into one user or one allowlist entry.
    No casefolding and no IDNA mapping, for the same reason.
    """
    return value.translate(_ASCII_LOWER)


def claim_at_path(claims: Mapping[str, Any], path: str) -> Any:
    """Value at a dotted claim path, e.g. ``realm_access.roles``.

    A namespaced claim name (``https://example.com/roles``, ``cognito:groups``) is
    looked up as a single top-level key. Otherwise the path is always walked, so a
    top-level claim literally named ``realm_access.roles`` cannot shadow the
    nested one.
    """
    if "/" in path or ":" in path:
        return claims.get(path)
    node: Any = claims
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _claim_contains(value: Any, wanted: str) -> bool:
    if isinstance(value, list | tuple):
        return any(isinstance(v, str | int) and str(v) == wanted for v in value)
    if isinstance(value, str | int) and not isinstance(value, bool):
        return str(value) == wanted
    return False


def _scopes(claims: Mapping[str, Any]) -> frozenset[str]:
    """Granted scopes: ``scope`` (RFC 9068, space-separated) and/or ``scp`` (Entra, Okta).

    Either claim may be a space-separated string or a list of strings.
    """
    found: set[str] = set()
    for name in ("scope", "scp"):
        value = claims.get(name)
        if isinstance(value, str):
            found.update(value.split())
        elif isinstance(value, list):
            found.update(s for s in value if isinstance(s, str) and s)
    return frozenset(found)


# ---- discovery --------------------------------------------------------------------------

_CONFIG_REASONS: Final = frozenset(
    {"issuer_mismatch", "bad_jwks_uri", "too_large", "not_json", "not_object"}
)


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    issuer: str
    jwks_uri: str


def discovery_urls(issuer: str) -> list[str]:
    """OIDC discovery first, then RFC 8414 (path-inserted) metadata."""
    base = issuer.rstrip("/")
    parts = urlsplit(issuer)
    path = parts.path.rstrip("/")
    origin = f"{parts.scheme}://{parts.netloc}"
    return [
        f"{base}/.well-known/openid-configuration",
        f"{origin}/.well-known/oauth-authorization-server{path}",
    ]


_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` of a plain http(s) URL; None for anything odd."""
    if any(c.isspace() or not c.isprintable() or c == "\\" for c in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return parts.scheme, parts.hostname, port or _DEFAULT_PORTS[parts.scheme]


def same_origin(issuer: str, url: str) -> bool:
    """``url`` has the issuer's scheme, host and port.

    Every URL Scriptorium fetches on the issuer's say-so (today only ``jwks_uri``)
    must pass, so a tampered or misconfigured metadata document can neither point
    key retrieval at another host nor downgrade it to plain http.
    """
    origin = _origin(url)
    return origin is not None and origin == _origin(issuer)


# ---- validator --------------------------------------------------------------------------


Clock = Callable[[], float]


class OIDCValidator:
    """Validates bearer tokens against one issuer and maps claims to a :class:`User`."""

    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Clock = time.monotonic,
        refetch_cooldown: float = JWKS_REFETCH_COOLDOWN_SECONDS,
        required_scopes: frozenset[str] | None = None,
    ) -> None:
        self._settings = settings
        self._issuer = settings.oidc_issuer
        self._owns_http = http is None
        # trust_env stays on so operators can supply SSL_CERT_FILE / HTTPS_PROXY.
        self._http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)
        self._clock = clock
        self._cooldown = refetch_cooldown
        # aud is the authoritative check (§5.1); scopes are an optional extra because
        # bring-your-own issuers name or omit the scope claim differently.
        self._required_scopes = (
            frozenset(settings.oidc_required_scopes) if required_scopes is None else required_scopes
        )
        # Entries containing "@" are emails and match only a verified email claim, never
        # the user claim: an issuer whose subject is a self-chosen email or username
        # must not let an unverified "alice@example.com" through.
        self._allowed = frozenset(u for u in settings.oidc_allowed_users if "@" not in u)
        self._allowed_emails = frozenset(
            ascii_lower(u) for u in settings.oidc_allowed_users if "@" in u
        )

        self._metadata: ProviderMetadata | None = None
        self._discovery_failed_at: float | None = None
        self._keys: dict[str, jwt.PyJWK] = {}
        self._unnamed_keys: list[jwt.PyJWK] = []
        self._jwks_fetched_at: float | None = None
        self._jwks_failed_at: float | None = None
        self._lock = anyio.Lock()
        self.jwks_fetch_count = 0  # observability and tests

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # -- fetching ---------------------------------------------------------------

    async def _get_json(self, url: str) -> dict[str, Any]:
        """GET a JSON object, reading no more than :data:`MAX_METADATA_BYTES` of body."""
        # identity: the cap then bounds what the issuer sends, not only what it inflates to.
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        async with self._http.stream("GET", url, headers=headers) as response:
            if response.status_code != 200:
                raise DiscoveryError(f"http_{response.status_code}")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_METADATA_BYTES:
                raise DiscoveryError("too_large")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw += chunk
                if len(raw) > MAX_METADATA_BYTES:
                    raise DiscoveryError("too_large")
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):
            raise DiscoveryError("not_json") from None
        if not isinstance(body, dict):
            raise DiscoveryError("not_object")
        return body

    async def discover(self) -> ProviderMetadata:
        """Fetch and validate the issuer's metadata (cached after success)."""
        if self._metadata is not None:
            return self._metadata
        async with self._lock:
            return await self._discover_locked()

    async def _discover_locked(self) -> ProviderMetadata:
        if self._metadata is not None:
            return self._metadata
        now = self._clock()
        if (
            self._discovery_failed_at is not None
            and now - self._discovery_failed_at < DISCOVERY_RETRY_SECONDS
        ):
            raise DiscoveryError("cooldown")
        reasons: list[str] = []
        for url in discovery_urls(self._issuer):
            try:
                doc = await self._get_json(url)
            except DiscoveryError as exc:
                reasons.append(exc.reason)
                continue
            except httpx.HTTPError as exc:
                reasons.append(type(exc).__name__)
                continue
            if doc.get("issuer") != self._issuer:
                # A metadata document for another issuer is never acceptable (RFC 8414 §3.3).
                reasons.append("issuer_mismatch")
                continue
            jwks_uri = doc.get("jwks_uri")
            if not isinstance(jwks_uri, str) or not same_origin(self._issuer, jwks_uri):
                reasons.append("bad_jwks_uri")
                continue
            self._metadata = ProviderMetadata(issuer=self._issuer, jwks_uri=jwks_uri)
            self._discovery_failed_at = None
            log_event(logger, "oidc_discovered", host=urlsplit(self._issuer).hostname)
            return self._metadata
        self._discovery_failed_at = now
        # A document that was found but rejected says more than a 404 from the fallback URL.
        reason = next((r for r in reasons if r in _CONFIG_REASONS), reasons[-1])
        log_event(
            logger,
            "oidc_discovery_failed",
            level=logging.WARNING,
            host=urlsplit(self._issuer).hostname,
            reason=reason,
        )
        raise DiscoveryError(reason)

    async def _fetch_jwks_locked(self) -> None:
        metadata = await self._discover_locked()
        self.jwks_fetch_count += 1
        try:
            doc = await self._get_json(metadata.jwks_uri)
        except httpx.HTTPError as exc:
            raise DiscoveryError(type(exc).__name__) from None
        raw_keys = doc.get("keys")
        if not isinstance(raw_keys, list):
            raise DiscoveryError("jwks_no_keys")
        keys: dict[str, jwt.PyJWK] = {}
        unnamed: list[jwt.PyJWK] = []
        for raw in raw_keys:
            if not isinstance(raw, dict) or raw.get("use", "sig") != "sig":
                continue
            try:
                key = jwt.PyJWK(raw)
            except (PyJWKError, InvalidKeyError, ValueError, TypeError):
                continue  # unsupported key types are skipped, not fatal
            if key.algorithm_name not in ALLOWED_ALGORITHMS:
                continue
            kid = raw.get("kid")
            if isinstance(kid, str) and kid:
                keys[kid] = key
            else:
                unnamed.append(key)
        self._keys = keys
        self._unnamed_keys = unnamed
        self._jwks_fetched_at = self._clock()
        self._jwks_failed_at = None
        log_event(
            logger,
            "oidc_jwks_fetched",
            host=urlsplit(metadata.jwks_uri).hostname,
            count=len(keys) + len(unnamed),
        )

    async def _key_for(self, kid: str | None) -> jwt.PyJWK:
        known = self._lookup(kid)
        if known is not None:
            return known
        async with self._lock:
            # Another request may have refreshed while we waited for the lock.
            known = self._lookup(kid)
            if known is not None:
                return known
            now = self._clock()
            if self._jwks_fetched_at is not None and now - self._jwks_fetched_at < self._cooldown:
                raise TokenInvalid("unknown_kid")
            if (
                self._jwks_failed_at is not None
                and now - self._jwks_failed_at < DISCOVERY_RETRY_SECONDS
            ):
                raise DiscoveryError("cooldown")
            try:
                await self._fetch_jwks_locked()
            except DiscoveryError:
                self._jwks_failed_at = now
                raise
        known = self._lookup(kid)
        if known is None:
            raise TokenInvalid("unknown_kid")
        return known

    def _lookup(self, kid: str | None) -> jwt.PyJWK | None:
        if kid is not None:
            return self._keys.get(kid)
        if len(self._unnamed_keys) + len(self._keys) == 1:
            return (self._unnamed_keys or list(self._keys.values()))[0]
        return None

    # -- validation ---------------------------------------------------------------

    async def validate(self, token: str) -> User:
        """Validate a bearer token and return its user, or raise an :class:`AuthError`."""
        try:
            user = await self._validate(token)
        except AuthError as exc:
            log_event(
                logger,
                "auth",
                level=logging.INFO,
                outcome=type(exc).__name__,
                reason=exc.reason,
            )
            raise
        log_event(logger, "auth", level=logging.DEBUG, outcome="ok", uid=user.log_uid)
        return user

    async def _validate(self, token: str) -> User:
        if not token or len(token) > MAX_TOKEN_BYTES or token.count(".") != 2:
            raise TokenInvalid("malformed")
        try:
            header = jwt.get_unverified_header(token)
        except InvalidTokenError:
            raise TokenInvalid("malformed") from None
        alg = header.get("alg")
        if alg not in ALLOWED_ALGORITHMS:
            raise TokenInvalid("bad_alg")
        kid = header.get("kid")
        if kid is not None and not isinstance(kid, str):
            raise TokenInvalid("malformed")

        key = await self._key_for(kid)
        if key.algorithm_name != alg:
            raise TokenInvalid("alg_key_mismatch")

        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=[alg],
                audience=self._settings.audience,
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud"], "verify_sub": False},
            )
        except ExpiredSignatureError:
            raise TokenExpired("expired") from None
        except ImmatureSignatureError:
            raise TokenInvalid("not_yet_valid") from None
        except InvalidAudienceError:
            raise TokenInvalid("bad_audience") from None
        except InvalidIssuerError:
            raise TokenInvalid("bad_issuer") from None
        except InvalidSignatureError:
            raise TokenInvalid("bad_signature") from None
        except MissingRequiredClaimError as exc:
            raise TokenInvalid(f"missing_{exc.claim}") from None
        except InvalidTokenError:
            raise TokenInvalid("invalid") from None

        # Belt and braces: PyJWT already checked these, but exactness matters here.
        if claims.get("iss") != self._issuer:
            raise TokenInvalid("bad_issuer")
        return self._user_from_claims(claims)

    def _user_from_claims(self, claims: Mapping[str, Any]) -> User:
        settings = self._settings
        sub = claims.get(settings.oidc_user_claim)
        if not isinstance(sub, str) or not sub:
            raise TokenInvalid(f"missing_{settings.oidc_user_claim}")

        email_claim = claims.get("email")
        email = email_claim if isinstance(email_claim, str) and email_claim else None
        email_verified = claims.get("email_verified") is True
        if settings.oidc_user_claim == "email":
            # An email is an identity only once the issuer has verified it, and ASCII
            # letter case must not split one mailbox into two users (two storage
            # directories). Non-ASCII stays as issued: see ascii_lower.
            if not email_verified:
                raise UserNotAllowed("email_not_verified")
            sub = ascii_lower(sub)

        allowed = (
            settings.allow_all_issuer_users
            or sub in self._allowed
            or (email is not None and email_verified and ascii_lower(email) in self._allowed_emails)
        )
        if not allowed:
            raise UserNotAllowed("not_on_allowlist")

        scopes = _scopes(claims)
        missing = self._required_scopes - scopes
        if missing:
            raise InsufficientScope("missing_scope")

        is_admin = False
        if settings.oidc_admin_claim and settings.oidc_admin_value:
            is_admin = _claim_contains(
                claim_at_path(claims, settings.oidc_admin_claim), settings.oidc_admin_value
            )

        uid_hash = uid_hash_for(self._issuer, sub)
        name_claim = claims.get(settings.oidc_name_claim)
        display_name = (
            # Never the email: the display name may be shown or logged where emails may not.
            name_claim if isinstance(name_claim, str) and name_claim else f"user-{uid_hash[:8]}"
        )
        return User(
            iss=self._issuer,
            sub=sub,
            uid_hash=uid_hash,
            display_name=display_name,
            email=email,
            email_verified=email_verified,
            is_admin=is_admin,
            scopes=scopes,
        )
