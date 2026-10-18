"""The ASGI application served by ``scriptorium serve`` (Architecture §4.1, §5.1, §11.0).

Request flow, outermost first:

1. ``ForwardedHeadersMiddleware``: the client address and scheme, believing
   ``X-Forwarded-*`` only from ``TRUSTED_PROXIES``.
2. ``AccessLogMiddleware``: one structured line per request, capability tokens redacted.
3. Scriptorium's own routes, which need no token: protected-resource metadata,
   ``/healthz`` and ``/dl/<token>``.
4. ``/mcp``: ``BearerAuthMiddleware`` (the 401/403 contract, then 405 for
   anything but POST), which binds the user to the request, then the MCP SDK's
   Streamable HTTP transport in stateless mode.

Request bodies on ``/mcp`` are limited by the SDK's body-limit middleware to
:data:`MCP_MAX_REQUEST_BODY_BYTES`; nothing else here reads a body.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, Protocol

from mcp.server import Server, ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
)
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from scriptorium import __version__
from scriptorium.auth.context import maybe_current_user, user_context
from scriptorium.auth.metadata import (
    PRM_PATH,
    forbidden_response,
    insufficient_scope_response,
    invalid_token_response,
    missing_token_response,
    protected_resource_metadata,
    unavailable_response,
)
from scriptorium.auth.oidc import (
    DiscoveryError,
    InsufficientScope,
    OIDCValidator,
    TokenInvalid,
    User,
    UserNotAllowed,
)
from scriptorium.config import IPNetwork, Settings
from scriptorium.errors import AmbiguousError, ScriptoriumError
from scriptorium.logging import log_event, uid_prefix
from scriptorium.tools import DownloadMinter, ToolContext, ToolRegistry
from scriptorium.tools._conventions import ErrorCode, ToolError, error_result
from scriptorium.tools._services import Services

logger = logging.getLogger(__name__)

MCP_PATH: Final = "/mcp"
MCP_MAX_REQUEST_BODY_BYTES: Final = 4 * 1024 * 1024
"""Largest accepted JSON-RPC request body on /mcp (S6 needs ≥ 2 MB tool arguments)."""

DOWNLOAD_TTL_SECONDS: Final = 600
DOWNLOAD_STORE_MAX_BYTES: Final = 256 * 1024 * 1024
DOWNLOAD_OWNER_MAX_BYTES: Final = 64 * 1024 * 1024
DOWNLOAD_OWNER_MAX_ITEMS: Final = 64
_DOWNLOAD_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SAFE_FILENAME_RE: Final = re.compile(r"[^A-Za-z0-9._-]")

SERVER_INSTRUCTIONS: Final = (
    "Scriptorium keeps the user's reMarkable library on their own server. Tools read, "
    "search and transcribe notes, push documents to the tablet and restore after a wipe. "
    "Pages are numbered from 1; errors carry a code, a message and a hint."
)


# ---- signed downloads (/dl/<token>) ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Download:
    data: bytes
    media_type: str
    owner: str  # uid hash
    filename: str
    expires_at: float


class DownloadStore(DownloadMinter, Protocol):
    """Capability-token store behind ``/dl/<token>``; replaceable by a durable one."""

    def get(self, token: str) -> Download | None: ...


class InMemoryDownloadStore:
    """Process-local download store: 256-bit tokens, 10-minute expiry, bounded memory.

    Tokens are kept only as SHA-256 digests. Each owner may hold at most
    ``max_owner_items`` downloads and ``max_owner_bytes``; beyond that the owner's
    own oldest entries go first, so one user minting at volume cannot push out
    another user's links. Only when the global byte budget is exceeded (which takes
    several owners at their caps) are the oldest entries overall evicted.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DOWNLOAD_TTL_SECONDS,
        max_bytes: int = DOWNLOAD_STORE_MAX_BYTES,
        max_owner_bytes: int = DOWNLOAD_OWNER_MAX_BYTES,
        max_owner_items: int = DOWNLOAD_OWNER_MAX_ITEMS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_bytes = max_bytes
        self._max_owner_bytes = max_owner_bytes
        self._max_owner_items = max_owner_items
        self._clock = clock
        self._items: OrderedDict[bytes, Download] = OrderedDict()
        self._bytes = 0
        # owner -> {key: size}, oldest first; each holds at most max_owner_items entries.
        self._owners: dict[str, OrderedDict[bytes, int]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> bytes:
        return hashlib.sha256(token.encode("ascii")).digest()

    def mint(self, data: bytes, *, media_type: str, owner: str, filename: str) -> str:
        size = len(data)
        if size > min(self._max_bytes, self._max_owner_bytes):
            raise ValueError("download larger than the store's budget")
        token = secrets.token_urlsafe(32)
        safe_name = _SAFE_FILENAME_RE.sub("_", filename)[:100] or "download"
        item = Download(data, media_type, owner, safe_name, self._clock() + self._ttl)
        with self._lock:
            self._purge_locked()
            # The owner's quota first: an owner over it loses only their own oldest links.
            while (mine := self._owners.get(owner)) and (
                len(mine) >= self._max_owner_items
                or sum(mine.values()) + size > self._max_owner_bytes
            ):
                self._remove_locked(next(iter(mine)))
            # Then the global budget, oldest first across owners.
            while self._items and self._bytes + size > self._max_bytes:
                self._remove_locked(next(iter(self._items)))
            key = self._key(token)
            self._items[key] = item
            self._bytes += size
            self._owners.setdefault(owner, OrderedDict())[key] = size
        return token

    def get(self, token: str) -> Download | None:
        if not _DOWNLOAD_TOKEN_RE.fullmatch(token):
            return None
        with self._lock:
            self._purge_locked()
            return self._items.get(self._key(token))

    def _remove_locked(self, key: bytes) -> None:
        item = self._items.pop(key)
        self._bytes -= len(item.data)
        mine = self._owners[item.owner]
        del mine[key]
        if not mine:
            del self._owners[item.owner]

    def _purge_locked(self) -> None:
        now = self._clock()
        for key in [k for k, v in self._items.items() if v.expires_at <= now]:
            self._remove_locked(key)


# ---- client address -----------------------------------------------------------------------


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    value = value.strip()
    if value.startswith("[") and "]" in value:  # "[::1]:443"
        value = value[1 : value.index("]")]
    elif value.count(":") == 1:  # "192.0.2.1:443"
        value = value.split(":", 1)[0]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _trusted(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address, nets: Sequence[IPNetwork]
) -> bool:
    return any(addr.version == net.version and addr in net for net in nets)


def resolve_client(
    peer: str | None,
    forwarded_for: Sequence[str],
    forwarded_proto: Sequence[str],
    scheme: str,
    trusted: Sequence[IPNetwork],
) -> tuple[str | None, str]:
    """Client IP and scheme, honouring forwarding headers only from trusted proxies.

    ``X-Forwarded-For`` is read right to left, skipping trusted proxies; the first
    untrusted hop is the client (the "rightmost untrusted" rule). Entries to its
    left were supplied by the client and are ignored.
    """
    peer_addr = _parse_ip(peer) if peer else None
    if peer_addr is None or not _trusted(peer_addr, trusted):
        return peer, scheme
    hops = [h for value in forwarded_for for h in value.split(",") if h.strip()]
    client = peer_addr
    for hop in reversed(hops):
        addr = _parse_ip(hop)
        if addr is None:
            break  # garbage can only have come from the client; keep the last good hop
        client = addr
        if not _trusted(addr, trusted):
            break
    protos = [p.strip().lower() for value in forwarded_proto for p in value.split(",")]
    proto = protos[-1] if protos and protos[-1] in ("http", "https") else scheme
    return str(client), proto


class ForwardedHeadersMiddleware:
    """Puts ``client_ip`` and ``scheme`` on the request state (for logs and rate limits).

    A trusted proxy's ``X-Forwarded-Proto`` also becomes the ASGI scope's scheme, so
    any URL Starlette derives from the request says https behind a TLS proxy.
    """

    def __init__(self, app: ASGIApp, trusted: Sequence[IPNetwork]) -> None:
        self.app = app
        self.trusted = tuple(trusted)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            client = scope.get("client")
            ip, proto = resolve_client(
                client[0] if client else None,
                headers.getlist("x-forwarded-for"),
                headers.getlist("x-forwarded-proto"),
                scope.get("scheme", "http"),
                self.trusted,
            )
            state = scope.setdefault("state", {})
            state["client_ip"] = ip
            state["scheme"] = proto
            # resolve_client returns the scope's own scheme unless a trusted proxy said
            # http or https, so this changes nothing for anyone else.
            scope["scheme"] = proto
        await self.app(scope, receive, send)


# ---- access log -------------------------------------------------------------------------

_KNOWN_PATHS: Final = frozenset(
    {MCP_PATH, MCP_PATH + "/", "/healthz", PRM_PATH, PRM_PATH + MCP_PATH}
)


def loggable_path(path: str) -> str:
    """The request path as it may appear in logs: no capability tokens, no junk."""
    if path in _KNOWN_PATHS:
        return path
    if path.startswith("/dl/"):
        return "/dl/<token>"
    return "<other>"


class AccessLogMiddleware:
    """One ``http`` event per request; the uid is filled in by the auth layer."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = loggable_path(scope.get("path", ""))
            log_event(
                logger,
                "http",
                level=logging.DEBUG if path == "/healthz" else logging.INFO,
                method=str(scope.get("method", ""))[:10],
                path=path,
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                uid=scope.get("state", {}).get("uid"),
            )


# ---- bearer auth -------------------------------------------------------------------------


def _bearer_token(scope: Scope) -> str | None:
    """The bearer token, ``""`` for a malformed Bearer header, None when absent."""
    values = Headers(scope=scope).getlist("authorization")
    if not values:
        return None
    if len(values) > 1:
        return ""
    scheme, _, token = values[0].partition(" ")
    if scheme.lower() != "bearer":
        return None  # another scheme counts as "no token": start the OAuth flow
    return token.strip()


class BearerAuthMiddleware:
    """The §5.1 contract in front of the MCP SDK; binds the user for the request."""

    def __init__(self, app: ASGIApp, validator: OIDCValidator, settings: Settings) -> None:
        self.app = app
        self.validator = validator
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = _bearer_token(scope)
        response: Response | None = None
        user: User | None = None
        if token is None:
            response = missing_token_response(self.settings)
        else:
            try:
                user = await self.validator.validate(token)
            except TokenInvalid:  # includes TokenExpired
                response = invalid_token_response(self.settings)
            except InsufficientScope:
                response = insufficient_scope_response(self.settings)
            except UserNotAllowed:
                response = forbidden_response()
            except DiscoveryError:
                response = unavailable_response()
        if response is not None or user is None:
            await (response or unavailable_response())(scope, receive, send)
            return
        scope.setdefault("state", {})["uid"] = user.log_uid
        if scope.get("method") != "POST":
            # Stateless JSON mode never opens a server-initiated SSE stream (GET) and has
            # no session to delete (DELETE); a GET would otherwise hold a stream open.
            await _method_not_allowed()(scope, receive, send)
            return
        with user_context(user):
            await self.app(scope, receive, send)


def _method_not_allowed() -> Response:
    return Response(
        "Method Not Allowed",
        status_code=405,
        media_type="text/plain",
        headers={"Allow": "POST", "Cache-Control": "no-store"},
    )


# ---- MCP -------------------------------------------------------------------------------------


def _describe_validation_error(exc: ValidationError) -> str:
    problems = []
    for err in exc.errors(include_input=False, include_url=False)[:10]:
        loc = ".".join(str(p) for p in err["loc"]) or "arguments"
        problems.append(f"{loc}: {err['msg']}")
    return "Invalid arguments: " + "; ".join(problems)


def _scriptorium_error_result(exc: ScriptoriumError) -> CallToolResult:
    """A library error as a tool error; its message and hint are client-safe by contract."""
    extra: dict[str, object] = {}
    if isinstance(exc, AmbiguousError):
        extra["candidates"] = list(exc.candidates)
    return error_result(ErrorCode(exc.code), exc.message, exc.hint, extra=extra)


def _write_audit(
    services: Services,
    user: User,
    tool: str,
    duration_ms: float,
    outcome: str,
    identifiers: Mapping[str, Any],
) -> None:
    """One audit row per call (§11.0); a malformed identifier drops the identifiers, not the
    row, and the log names only the tool."""
    try:
        services.store.audit(
            uid_hash=user.uid_hash,
            tool=tool,
            duration_ms=duration_ms,
            outcome=outcome,
            identifiers=identifiers,
        )
    except ValueError:
        log_event(logger, "audit_identifiers_rejected", level=logging.ERROR, tool=tool)
        services.store.audit(
            uid_hash=user.uid_hash,
            tool=tool,
            duration_ms=duration_ms,
            outcome=outcome,
            identifiers={},
        )


def build_mcp_server(
    settings: Settings,
    registry: ToolRegistry,
    downloads: DownloadStore,
    services: Services | None = None,
) -> Server[dict[str, Any]]:
    """The low-level MCP server: per-request tool listing, dispatch and the audit row."""

    async def list_tools(
        ctx: ServerRequestContext[dict[str, Any]], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        user = _require_user()
        tools = [spec.to_mcp() for spec in registry.visible_to(user)]
        # Per-user lists must never be served from a shared cache.
        return ListToolsResult(tools=tools, ttl_ms=0, cache_scope="private")

    async def call_tool(
        ctx: ServerRequestContext[dict[str, Any]], params: CallToolRequestParams
    ) -> CallToolResult:
        user = _require_user()
        started = time.perf_counter()
        spec = registry.get(params.name)
        tool_name = spec.name if spec is not None else "<unknown>"
        outcome = "ok"
        tool_ctx: ToolContext | None = None
        try:
            if spec is None:
                outcome = "unknown_tool"
                raise MCPError(code=INVALID_PARAMS, message="Unknown tool")
            if not spec.visible_to(user):
                outcome = ErrorCode.REFUSED.value
                return error_result(
                    ErrorCode.REFUSED,
                    "This tool is for administrators only.",
                    "Ask the operator if you need it.",
                )
            tool_ctx = ToolContext(
                user=user, settings=settings, downloads=downloads, services=services
            )
            try:
                result = await spec.invoke(tool_ctx, params.arguments or {})
            except ValidationError as exc:
                outcome = ErrorCode.INVALID_ARGUMENT.value
                return error_result(
                    ErrorCode.INVALID_ARGUMENT,
                    _describe_validation_error(exc),
                    "Check the tool's input schema.",
                )
            except ToolError as exc:
                outcome = exc.code.value
                return error_result(exc.code, exc.message, exc.hint, extra=exc.extra)
            except ScriptoriumError as exc:
                if exc.code == "internal":
                    outcome = "internal_error"
                    log_event(
                        logger,
                        "tool_failed",
                        level=logging.ERROR,
                        exc_info=True,
                        uid=user.log_uid,
                        tool=tool_name,
                    )
                    raise MCPError(code=INTERNAL_ERROR, message="Internal error") from None
                outcome = exc.code
                return _scriptorium_error_result(exc)
            except Exception:
                outcome = "internal_error"
                log_event(
                    logger,
                    "tool_failed",
                    level=logging.ERROR,
                    exc_info=True,
                    uid=user.log_uid,
                    tool=tool_name,
                )
                # A generic protocol error: exception text never reaches the client.
                raise MCPError(code=INTERNAL_ERROR, message="Internal error") from None
            if result.is_error:
                outcome = "error"
            return result
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            log_event(
                logger,
                "tool_call",
                uid=user.log_uid,
                tool=tool_name,
                duration_ms=duration_ms,
                outcome=outcome,
            )
            if services is not None and spec is not None:
                identifiers = dict(tool_ctx.audit) if tool_ctx is not None else {}
                try:
                    await asyncio.to_thread(
                        _write_audit, services, user, spec.name, duration_ms, outcome, identifiers
                    )
                except Exception:
                    log_event(
                        logger, "audit_failed", level=logging.ERROR, exc_info=True, tool=tool_name
                    )

    server: Server[dict[str, Any]] = Server(
        "scriptorium",
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        website_url="https://github.com/MilkyEngineer/scriptorium",
        get_tool_input_schema=registry.input_schema,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    # The SDK's default OpenTelemetry middleware is a no-op without an exporter;
    # drop it so nothing can start exporting tool names or errors by accident.
    server.middleware.clear()
    return server


def _require_user() -> User:
    user = maybe_current_user()
    if user is None:
        # Unreachable behind BearerAuthMiddleware; fail closed if it ever is not.
        raise MCPError(code=INTERNAL_ERROR, message="Not authenticated")
    return user


def build_registry(settings: Settings, services: Services | None = None) -> ToolRegistry:
    """The tools this deployment offers: the library, account and admin tools when the server
    has ``/data`` open (``services``), and the S6 spike tools when ``SPIKE_TOOLS`` is on."""
    registry = ToolRegistry()
    if services is not None:
        from scriptorium.tools import account, admin, admin_sync, library, ocr

        account.register(registry)
        library.register(registry)
        ocr.register(registry)
        admin.register(registry)
        admin_sync.register(registry)
    if settings.spike_tools:
        from scriptorium.tools.spike import register_spike_tools

        register_spike_tools(registry)
    return registry


# ---- the application -------------------------------------------------------------------------


def create_app(
    settings: Settings,
    *,
    validator: OIDCValidator | None = None,
    downloads: DownloadStore | None = None,
    registry: ToolRegistry | None = None,
    services: Services | None = None,
) -> Starlette:
    """Build the ASGI app. Dependencies are injectable for tests and later milestones.

    ``services`` (the opened, migrated ``/data``; the caller owns and closes it) enables the
    library tools and the audit log; without it only the spike tools can be offered.
    """
    owns_validator = validator is None
    oidc = validator or OIDCValidator(settings)
    store: DownloadStore = downloads or InMemoryDownloadStore()
    tools = registry or build_registry(settings, services)

    mcp_server = build_mcp_server(settings, tools, store, services)
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        stateless=True,
        json_response=True,
        # Host/Origin checks guard cookie-less localhost servers against DNS rebinding;
        # here every request needs a bearer token, which a rebinding page cannot obtain.
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        max_request_body_size=MCP_MAX_REQUEST_BODY_BYTES,
    )
    mcp_app = BearerAuthMiddleware(StreamableHTTPASGIApp(session_manager), oidc, settings)

    prm = protected_resource_metadata(settings)

    async def protected_resource(request: Request) -> Response:
        return JSONResponse(
            prm, headers={"Cache-Control": "max-age=3600", "Access-Control-Allow-Origin": "*"}
        )

    async def healthz(request: Request) -> Response:
        return JSONResponse(
            {"status": "ok", "version": __version__}, headers={"Cache-Control": "no-store"}
        )

    async def download(request: Request) -> Response:
        token = request.path_params["token"]
        item = store.get(token)
        if item is None:
            log_event(logger, "download", outcome="not_found")
            return Response("Not Found", status_code=404, headers={"Cache-Control": "no-store"})
        log_event(
            logger, "download", outcome="ok", uid=uid_prefix(item.owner), bytes=len(item.data)
        )
        return Response(
            item.data,
            media_type=item.media_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{item.filename}"',
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            log_event(logger, "server_start", version=__version__)
            try:
                yield
            finally:
                if owns_validator:
                    await oidc.aclose()

    routes = [
        Route(PRM_PATH, protected_resource, methods=["GET"]),
        Route(PRM_PATH + MCP_PATH, protected_resource, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/dl/{token}", download, methods=["GET"]),
        Route(MCP_PATH, mcp_app),
        Route(MCP_PATH + "/", mcp_app),
    ]
    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(ForwardedHeadersMiddleware, trusted=settings.trusted_proxies),
            Middleware(AccessLogMiddleware),
        ],
        lifespan=lifespan,
    )
    # A slash redirect builds its Location from the client's Host header (an open
    # redirect, and /dl tokens in URLs), and no client needs one: every path here is
    # exact. Starlette 0.52's constructor does not take this flag.
    app.router.redirect_slashes = False
    return app
