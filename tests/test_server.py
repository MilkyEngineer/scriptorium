"""The ASGI app: route ordering, the 401/403 contract, MCP round trips, /dl, client IPs."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import ipaddress
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import httpx2
import pytest
from cryptography.fernet import Fernet
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import ImageContent, ResourceLink, TextContent
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from starlette.applications import Starlette

from scriptorium import __version__, cli
from scriptorium.auth.oidc import OIDCValidator
from scriptorium.server import (
    MCP_MAX_REQUEST_BODY_BYTES,
    InMemoryDownloadStore,
    build_registry,
    create_app,
    loggable_path,
    resolve_client,
)
from tests.conftest import (
    PUBLIC_URL,
    FakeClock,
    FakeIssuer,
    make_settings,
    running,
)

PRM_URL = "https://mcp.example.com/.well-known/oauth-protected-resource"
CHALLENGE = f'Bearer resource_metadata="{PRM_URL}", scope="scriptorium"'
USER_TOOLS = {
    "spike_whoami",
    "spike_image",
    "spike_resource_link",
    "spike_download_url",
    "spike_text",
    "spike_echo_size",
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture
def store(clock: FakeClock) -> InMemoryDownloadStore:
    return InMemoryDownloadStore(clock=clock)


@pytest.fixture
async def app(
    make_validator: Callable[..., OIDCValidator], store: InMemoryDownloadStore
) -> AsyncIterator[Starlette]:
    settings = make_settings()
    built = create_app(settings, validator=make_validator(settings), downloads=store)
    async with running(built):
        yield built


@pytest.fixture
async def http(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example.com") as c:
        yield c


@asynccontextmanager
async def mcp_session(
    app: Starlette, token: str, *, mode: str = "legacy"
) -> AsyncIterator[tuple[Client, httpx2.AsyncClient]]:
    """An SDK client speaking Streamable HTTP to the app in-process."""
    transport = httpx2.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(transport=transport, headers=headers) as http_client:
        streams = streamable_http_client(PUBLIC_URL, http_client=http_client)
        async with Client(streams, mode=mode, cache=None) as client:
            yield client, http_client


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **MCP_HEADERS}


def _text(content: object) -> str:
    assert isinstance(content, TextContent)
    return content.text


# ---- own routes before auth ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"],
)
async def test_prm_served_without_token(http: httpx.AsyncClient, path: str) -> None:
    response = await http.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "max-age=3600"
    assert response.json() == {
        "resource": "https://mcp.example.com/mcp",
        "authorization_servers": ["https://auth.example.com/realms/scriptorium"],
        "scopes_supported": ["scriptorium"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://github.com/MilkyEngineer/scriptorium",
    }


async def test_healthz_without_token(http: httpx.AsyncClient) -> None:
    response = await http.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
async def test_mcp_without_token_is_401_with_challenge(http: httpx.AsyncClient, path: str) -> None:
    response = await http.post(path, json=_rpc("tools/list"), headers=MCP_HEADERS)
    assert response.status_code == 401
    assert response.headers.get_list("www-authenticate") == [CHALLENGE]


async def test_other_auth_scheme_counts_as_no_token(http: httpx.AsyncClient) -> None:
    headers = {"Authorization": "Basic dXNlcjpwYXNz", **MCP_HEADERS}
    response = await http.post("/mcp", json=_rpc("tools/list"), headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == CHALLENGE


async def test_mcp_invalid_token_is_401_invalid_token(http: httpx.AsyncClient) -> None:
    response = await http.post("/mcp", json=_rpc("tools/list"), headers=_bearer("not.a.jwt"))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == f'{CHALLENGE}, error="invalid_token"'


async def test_mcp_expired_token_is_401_invalid_token(
    http: httpx.AsyncClient, issuer: FakeIssuer
) -> None:
    claims = issuer.claims()
    claims["exp"] = claims["iat"] - 3600
    response = await http.post(
        "/mcp", json=_rpc("tools/list"), headers=_bearer(issuer.token(claims=claims))
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == f'{CHALLENGE}, error="invalid_token"'


async def test_mcp_user_not_on_allowlist_is_403(
    http: httpx.AsyncClient, issuer: FakeIssuer
) -> None:
    token = issuer.token(sub="99999999-0000-4000-8000-000000000000")
    response = await http.post("/mcp", json=_rpc("tools/list"), headers=_bearer(token))
    assert response.status_code == 403
    assert "www-authenticate" not in response.headers
    assert response.json()["error"] == "access_denied"


async def test_mcp_issuer_down_is_503(http: httpx.AsyncClient, issuer: FakeIssuer) -> None:
    issuer.down = True
    response = await http.post("/mcp", json=_rpc("tools/list"), headers=_bearer(issuer.token()))
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"


async def test_insufficient_scope_is_403(
    issuer: FakeIssuer, make_validator: Callable[..., OIDCValidator]
) -> None:
    settings = make_settings(oidc_required_scopes="scriptorium")
    validator = make_validator(settings)
    app = create_app(settings, validator=validator)
    token = issuer.token(scope="openid email")
    async with (
        running(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.com"
        ) as http,
    ):
        response = await http.post("/mcp", json=_rpc("tools/list"), headers=_bearer(token))
    assert response.status_code == 403
    assert response.headers["www-authenticate"] == f'{CHALLENGE}, error="insufficient_scope"'


async def test_default_app_accepts_token_without_scope_claim(
    app: Starlette, issuer: FakeIssuer
) -> None:
    """aud is the authoritative check; no scope is required unless configured."""
    async with mcp_session(app, issuer.token(scope=None)) as (client, _):
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == USER_TOOLS


async def test_required_scope_accepted_from_scp(
    issuer: FakeIssuer, make_validator: Callable[..., OIDCValidator]
) -> None:
    settings = make_settings(oidc_required_scopes="scriptorium")
    app = create_app(settings, validator=make_validator(settings))
    async with (
        running(app),
        mcp_session(app, issuer.token(scope=None, scp=["scriptorium"])) as (
            client,
            _,
        ),
    ):
        assert {tool.name for tool in (await client.list_tools()).tools} == USER_TOOLS


async def test_unknown_path_is_404_without_auth(http: httpx.AsyncClient) -> None:
    response = await http.get("/admin")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/mcp//",
        "/healthz/",
        f"/dl/{'A' * 43}/",
        "/.well-known/oauth-protected-resource/",
        "/.well-known/oauth-protected-resource/mcp/",
    ],
)
@pytest.mark.parametrize("host", ["mcp.example.com", "evil.example.com"])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_no_trailing_slash_redirects(
    http: httpx.AsyncClient, path: str, host: str, method: str
) -> None:
    """A slash redirect would echo the Host header into Location (an open redirect)."""
    response = await http.request(method, path, headers={"Host": host})
    assert response.status_code == 404
    assert "location" not in response.headers


def test_spike_tools_only_when_enabled() -> None:
    assert len(build_registry(make_settings(spike_tools=False))) == 0
    assert len(build_registry(make_settings(spike_tools=True))) == 7


# ---- MCP round trips -----------------------------------------------------------------------


# "legacy" is the initialize handshake (stateless legacy path); "auto" negotiates the
# 2026-07-28 single-exchange era. The user context must reach the handler in both.
ERAS = pytest.mark.parametrize("mode", ["legacy", "auto"])


@ERAS
async def test_initialize_and_list_tools(app: Starlette, issuer: FakeIssuer, mode: str) -> None:
    async with mcp_session(app, issuer.token(), mode=mode) as (client, _):
        assert client.server_info is not None
        assert client.server_info.name == "scriptorium"
        modern = client.protocol_version in MODERN_PROTOCOL_VERSIONS
        assert modern is (mode == "auto")
        result = await client.list_tools()
    names = {tool.name for tool in result.tools}
    assert names == USER_TOOLS
    for tool in result.tools:
        assert tool.description
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True


@ERAS
async def test_admin_sees_and_calls_admin_tool(
    app: Starlette, issuer: FakeIssuer, mode: str
) -> None:
    async with mcp_session(app, issuer.admin_token(), mode=mode) as (client, _):
        names = {tool.name for tool in (await client.list_tools()).tools}
        result = await client.call_tool("admin_spike_ping", {})
    assert names == USER_TOOLS | {"admin_spike_ping"}
    assert not result.is_error
    assert _text(result.content[0]) == "pong"


async def test_non_admin_is_refused_admin_tool(app: Starlette, issuer: FakeIssuer) -> None:
    async with mcp_session(app, issuer.token()) as (client, _):
        result = await client.call_tool("admin_spike_ping", {})
    assert result.is_error
    assert json.loads(_text(result.content[0]))["code"] == "refused"
    assert result.structured_content is not None
    assert result.structured_content["code"] == "refused"


@ERAS
async def test_tool_list_follows_the_token_without_reconnect(
    app: Starlette, issuer: FakeIssuer, mode: str
) -> None:
    """Stateless: each request is authorised on its own, so the list changes at once."""
    async with mcp_session(app, issuer.token(), mode=mode) as (client, http_client):
        before = {tool.name for tool in (await client.list_tools()).tools}
        http_client.headers["Authorization"] = f"Bearer {issuer.admin_token()}"
        after = {tool.name for tool in (await client.list_tools()).tools}
        http_client.headers["Authorization"] = f"Bearer {issuer.token()}"
        again = {tool.name for tool in (await client.list_tools()).tools}
    assert "admin_spike_ping" not in before
    assert "admin_spike_ping" in after
    assert "admin_spike_ping" not in again


@ERAS
async def test_concurrent_users_do_not_share_identity(
    app: Starlette, issuer: FakeIssuer, mode: str
) -> None:
    """Interleaved requests from two users each see only their own identity."""

    async def whoami(token: str) -> list[bool]:
        async with mcp_session(app, token, mode=mode) as (client, _):
            seen = []
            for _ in range(5):
                result = await client.call_tool("spike_whoami", {})
                assert result.structured_content is not None
                seen.append(bool(result.structured_content["is_admin"]))
                await asyncio.sleep(0)
            return seen

    plain, admin = await asyncio.gather(whoami(issuer.token()), whoami(issuer.admin_token()))
    assert plain == [False] * 5
    assert admin == [True] * 5


async def test_whoami_reports_hash_not_email(app: Starlette, issuer: FakeIssuer) -> None:
    token = issuer.token(email="reader@example.com", email_verified=True)
    async with mcp_session(app, token) as (client, _):
        result = await client.call_tool("spike_whoami", {})
    payload = result.structured_content
    assert payload is not None
    assert payload["is_admin"] is False
    assert payload["display_name"] == "reader"
    assert len(payload["uid_hash"]) == 64
    assert "reader@example.com" not in _text(result.content[0])


async def test_admin_whoami(app: Starlette, issuer: FakeIssuer) -> None:
    async with mcp_session(app, issuer.admin_token()) as (client, _):
        result = await client.call_tool("spike_whoami", {})
    assert result.structured_content is not None
    assert result.structured_content["is_admin"] is True


async def test_invalid_arguments_are_invalid_argument(app: Starlette, issuer: FakeIssuer) -> None:
    async with mcp_session(app, issuer.token()) as (client, _):
        too_small = await client.call_tool("spike_image", {"size_kb": 10})
        extra = await client.call_tool("spike_whoami", {"user_id": "someone-else"})
        too_long = await client.call_tool("spike_text", {"kb": 65})
    for result in (too_small, extra, too_long):
        assert result.is_error
        assert json.loads(_text(result.content[0]))["code"] == "invalid_argument"
    # The rejected value is not echoed back.
    assert "someone-else" not in _text(extra.content[0])


async def test_unknown_tool_is_a_protocol_error(app: Starlette, issuer: FakeIssuer) -> None:
    async with mcp_session(app, issuer.token()) as (client, _):
        with pytest.raises(MCPError):
            await client.call_tool("no_such_tool", {})


async def test_spike_image_size(app: Starlette, issuer: FakeIssuer) -> None:
    async with mcp_session(app, issuer.token()) as (client, _):
        result = await client.call_tool("spike_image", {"size_kb": 200})
    image = result.content[0]
    assert isinstance(image, ImageContent)
    assert image.mime_type == "image/png"
    png = base64.b64decode(image.data)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert 190 * 1024 <= len(png) <= 200 * 1024


async def test_spike_text_exact_size(app: Starlette, issuer: FakeIssuer) -> None:
    async with mcp_session(app, issuer.token()) as (client, _):
        result = await client.call_tool("spike_text", {"kb": 64})
    assert not result.is_error
    assert len(_text(result.content[0]).encode()) == 64 * 1024


async def test_spike_echo_size_two_mib(app: Starlette, issuer: FakeIssuer) -> None:
    payload = "x" * (2 * 1024 * 1024)
    async with mcp_session(app, issuer.token()) as (client, _):
        result = await client.call_tool("spike_echo_size", {"payload": payload})
    assert result.structured_content == {"bytes": 2 * 1024 * 1024}


async def test_body_over_limit_is_413(http: httpx.AsyncClient, issuer: FakeIssuer) -> None:
    payload = "x" * (MCP_MAX_REQUEST_BODY_BYTES + 1)
    body = _rpc("tools/call", {"name": "spike_echo_size", "arguments": {"payload": payload}})
    response = await http.post(
        "/mcp", content=json.dumps(body), headers={**_bearer(issuer.token())}
    )
    assert response.status_code == 413


async def test_resource_link_downloads_once_minted(
    app: Starlette, http: httpx.AsyncClient, issuer: FakeIssuer
) -> None:
    async with mcp_session(app, issuer.token()) as (client, _):
        result = await client.call_tool("spike_resource_link", {"size_kb": 50})
    link = result.content[0]
    assert isinstance(link, ResourceLink)
    assert link.uri.startswith("https://mcp.example.com/dl/")
    assert link.mime_type == "image/png"
    response = await http.get(link.uri.removeprefix("https://mcp.example.com"))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert len(response.content) == link.size


async def test_download_url_is_text(app: Starlette, issuer: FakeIssuer) -> None:
    async with mcp_session(app, issuer.token()) as (client, _):
        result = await client.call_tool("spike_download_url", {"size_kb": 50})
    assert "https://mcp.example.com/dl/" in _text(result.content[0])


# ---- /dl ----------------------------------------------------------------------------------


async def test_dl_unknown_token_is_404(http: httpx.AsyncClient) -> None:
    unknown = "A" * 43
    assert (await http.get(f"/dl/{unknown}")).status_code == 404
    assert (await http.get("/dl/short")).status_code == 404


async def test_dl_expired_token_is_404(
    http: httpx.AsyncClient, store: InMemoryDownloadStore, clock: FakeClock
) -> None:
    token = store.mint(b"data", media_type="text/plain", owner="0" * 64, filename="a.txt")
    assert (await http.get(f"/dl/{token}")).status_code == 200
    clock.advance(601)
    assert (await http.get(f"/dl/{token}")).status_code == 404


def test_download_store_tokens_and_eviction(clock: FakeClock) -> None:
    store = InMemoryDownloadStore(clock=clock, max_bytes=10)
    first = store.mint(b"123456", media_type="text/plain", owner="u", filename="../../x y.txt")
    assert len(first) == 43  # 256 bits, urlsafe base64
    item = store.get(first)
    assert item is not None
    assert item.filename == ".._.._x_y.txt"
    second = store.mint(b"789012", media_type="text/plain", owner="u", filename="b")
    assert store.get(first) is None  # evicted to stay within the budget
    assert store.get(second) is not None
    with pytest.raises(ValueError, match="budget"):
        store.mint(b"x" * 11, media_type="text/plain", owner="u", filename="c")


def test_loggable_path_redacts_tokens() -> None:
    assert loggable_path("/dl/abcdef") == "/dl/<token>"
    assert loggable_path("/mcp") == "/mcp"
    assert loggable_path("/etc/passwd") == "<other>"


# ---- client address ----------------------------------------------------------------------

TRUSTED = (ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("fd00::/8"))


@pytest.mark.parametrize(
    ("peer", "xff", "expected"),
    [
        # Untrusted peer: headers are ignored entirely.
        ("203.0.113.9", ["198.51.100.1"], "203.0.113.9"),
        # Trusted proxy: the client is the rightmost untrusted hop.
        ("172.18.0.2", ["198.51.100.1"], "198.51.100.1"),
        # A spoofed left-hand entry does not win.
        ("172.18.0.2", ["10.9.9.9, 198.51.100.1"], "198.51.100.1"),
        # Chained trusted proxies are skipped.
        ("172.18.0.2", ["198.51.100.1, 172.20.0.5"], "198.51.100.1"),
        # Several header lines are concatenated in order.
        ("172.18.0.2", ["192.0.2.50", "198.51.100.1"], "198.51.100.1"),
        # Garbage stops the walk at the last good hop.
        ("172.18.0.2", ["198.51.100.1, not-an-ip, 172.20.0.5"], "172.20.0.5"),
        # Trusted peer, no header: the peer itself.
        ("172.18.0.2", [], "172.18.0.2"),
        # IPv6 with ports and brackets.
        ("fd00::1", ["[2001:db8::7]:4711"], "2001:db8::7"),
        ("172.18.0.2", ["198.51.100.1:5555"], "198.51.100.1"),
    ],
)
def test_resolve_client_rightmost_untrusted(peer: str, xff: list[str], expected: str) -> None:
    ip, _ = resolve_client(peer, xff, [], "http", TRUSTED)
    assert ip == expected


def test_resolve_client_scheme() -> None:
    assert resolve_client("172.18.0.2", [], ["https"], "http", TRUSTED)[1] == "https"
    assert resolve_client("172.18.0.2", [], ["gopher"], "http", TRUSTED)[1] == "http"
    assert resolve_client("203.0.113.9", [], ["https"], "http", TRUSTED)[1] == "http"


async def test_client_ip_on_request_state(issuer: FakeIssuer) -> None:
    seen: dict[str, Any] = {}

    from starlette.requests import Request
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from scriptorium.server import ForwardedHeadersMiddleware

    async def probe(request: Request) -> PlainTextResponse:
        seen.update(request.state._state)
        seen["url"] = str(request.url)
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/", probe)])
    wrapped = ForwardedHeadersMiddleware(inner, TRUSTED)
    forwarded = {"X-Forwarded-For": "198.51.100.1", "X-Forwarded-Proto": "https"}
    transport = httpx.ASGITransport(app=wrapped, client=("172.18.0.2", 4000))
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp.example.com") as c:
        await c.get("/", headers=forwarded)
    assert seen["client_ip"] == "198.51.100.1"
    assert seen["scheme"] == "https"
    assert seen["url"] == "https://mcp.example.com/"  # the scope's scheme follows the proxy

    # From an untrusted peer the same headers change nothing.
    transport = httpx.ASGITransport(app=wrapped, client=("203.0.113.9", 4000))
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp.example.com") as c:
        await c.get("/", headers=forwarded)
    assert seen["client_ip"] == "203.0.113.9"
    assert seen["scheme"] == "http"
    assert seen["url"] == "http://mcp.example.com/"


# ---- the command line (serve's entry point) -----------------------------------------------


def test_worker_is_a_stub(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["worker"]) == 2
    assert "not implemented" in capsys.readouterr().err


def test_gen_master_key_is_a_fernet_key(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["gen-master-key"]) == 0
    key = capsys.readouterr().out.strip()
    Fernet(key)  # raises if malformed
    assert len(base64.urlsafe_b64decode(key)) == 32


def test_serve_refuses_bad_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["serve"]) == 2  # the environment is empty under test
    err = capsys.readouterr().err
    assert "SCRIPTORIUM_PUBLIC_URL" in err


@pytest.mark.parametrize(
    "licence",
    [
        "MIT",
        "Apache-2.0",
        "BSD License",
        "MPL-2.0",
        "Apache-2.0 OR GPL-2.0-or-later",
        "MIT AND BSD-3-Clause",
        "MIT License; BSD License",
        "Historical Permission Notice and Disclaimer (HPND)",
    ],
)
def test_licence_acceptable(licence: str) -> None:
    assert cli.licence_acceptable(licence)


@pytest.mark.parametrize(
    "licence",
    [
        "GPL-3.0-only",
        "AGPL-3.0-or-later",
        "LGPL-2.1",
        "GNU General Public License v3 (GPLv3)",
        "GNU Lesser General Public License v3 (LGPLv3)",
        "GNU Affero General Public License v3",
        "MIT AND GPL-3.0",
        "MIT License; GNU General Public License v2 (GPLv2)",
        "UNKNOWN",
        "",
    ],
)
def test_licence_rejected(licence: str) -> None:
    assert not cli.licence_acceptable(licence)


def test_licence_problems() -> None:
    packages = [
        cli.PackageLicence("good", "1", "MIT"),
        cli.PackageLicence("bad", "1", "GPL-3.0"),
        cli.PackageLicence("PyMuPDF", "1", "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex"),
        cli.PackageLicence("resvg_py", "1", "UNKNOWN"),
    ]
    problems, notes = cli.licence_problems(packages)
    assert any(p.startswith("bad 1:") for p in problems)
    assert any(p.startswith("PyMuPDF 1:") for p in problems)
    assert not any("good" in p or "resvg" in p for p in problems)
    assert len(notes) == 1 and "resvg_py" in notes[0]


def test_licence_problems_forbidden_module(monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.util.find_spec

    def find_spec(name: str, *args: Any) -> Any:
        return object() if name == "ebooklib" else real(name, *args)

    monkeypatch.setattr(cli.importlib.util, "find_spec", find_spec)
    problems, _ = cli.licence_problems([])
    assert problems == ["module 'ebooklib' is importable; it must never be installed"]


def test_licence_check_passes_on_this_environment(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["licence-check"]) == 0
    out = capsys.readouterr().out
    assert "licence-check passed" in out
    assert "allowlisted resvg" in out


def test_metadata_fallback_agrees_with_pip_licenses() -> None:
    via_pip = cli._from_pip_licenses()
    assert via_pip is not None
    via_metadata = cli._from_metadata()
    assert cli.licence_problems(via_metadata)[0] == []
    assert {cli.normalise_name(p.name) for p in via_metadata} >= {
        cli.normalise_name(p.name) for p in via_pip
    }
