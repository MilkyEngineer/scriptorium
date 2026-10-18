"""Shared fixtures: settings, a fake OIDC issuer with RSA and EC keys, and the app."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from starlette.applications import Starlette

from scriptorium.auth.oidc import OIDCValidator
from scriptorium.config import Settings

PUBLIC_URL = "https://mcp.example.com/mcp"
ISSUER = "https://auth.example.com/realms/scriptorium"
JWKS_URI = f"{ISSUER}/protocol/openid-connect/certs"
OIDC_DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
RFC8414_DISCOVERY = (
    "https://auth.example.com/.well-known/oauth-authorization-server/realms/scriptorium"
)
ALLOWED_SUB = "11111111-aaaa-4bbb-8ccc-000000000001"
ADMIN_SUB = "11111111-aaaa-4bbb-8ccc-000000000002"
ALLOWED_EMAIL = "reader@example.com"
MASTER_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see the developer's SCRIPTORIUM_* environment."""
    import os

    for name in list(os.environ):
        if name.upper().startswith("SCRIPTORIUM_"):  # settings match names in any case
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_claude_api(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches the Claude API (M2 breakdown §4): the anthropic SDK's transport is
    httpx2's, which refuses here. Only tests marked ``live_ocr`` (tests/live, behind
    SCRIPTORIUM_LIVE_OCR_TEST=1) may use it; mocked transports are unaffected."""
    if request.node.get_closest_marker("live_ocr") is not None:
        return
    import httpx2

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a test tried to reach the network through httpx2")

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", refuse)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "public_url": PUBLIC_URL,
        "oidc_issuer": ISSUER,
        "master_keys": MASTER_KEY,
        "oidc_allowed_users": f"{ALLOWED_SUB},{ADMIN_SUB},{ALLOWED_EMAIL}",
        "oidc_admin_claim": "realm_access.roles",
        "oidc_admin_value": "scriptorium-admin",
        "spike_tools": True,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


# ---- keys and a fake issuer ---------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_private() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def rsa_private_2() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def ec_private() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unsigned_token(header: dict[str, Any], claims: dict[str, Any], signature: bytes = b"") -> str:
    """A hand-built JWT (for alg=none and similar attacks)."""
    head = _b64url(json.dumps(header).encode())
    body = _b64url(json.dumps(claims).encode())
    return f"{head}.{body}.{_b64url(signature)}"


@dataclass
class FakeClock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeIssuer:
    """Serves OIDC discovery and a JWKS; counts every request by URL."""

    keys: dict[str, tuple[Any, str]] = field(default_factory=dict)  # kid -> (private, alg)
    published: set[str] = field(default_factory=set)
    hits: Counter[str] = field(default_factory=Counter)
    oidc_issuer_value: str = ISSUER
    jwks_uri_value: str = JWKS_URI
    jwks_headers: dict[str, str] = field(default_factory=dict)
    serve_oidc: bool = True
    serve_rfc8414: bool = True
    down: bool = False

    def add_key(self, kid: str, private: Any, alg: str, *, publish: bool = True) -> None:
        self.keys[kid] = (private, alg)
        if publish:
            self.published.add(kid)

    def jwks(self) -> dict[str, Any]:
        out = []
        for kid in sorted(self.published):
            private, alg = self.keys[kid]
            algo = RSAAlgorithm if alg == "RS256" else ECAlgorithm
            jwk = algo.to_jwk(private.public_key(), as_dict=True)
            jwk.update({"kid": kid, "use": "sig", "alg": alg})
            out.append(jwk)
        return {"keys": out}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.hits[url] += 1
        if self.down:
            return httpx.Response(503)
        if url == OIDC_DISCOVERY and self.serve_oidc:
            return httpx.Response(
                200, json={"issuer": self.oidc_issuer_value, "jwks_uri": self.jwks_uri_value}
            )
        if url == RFC8414_DISCOVERY and self.serve_rfc8414:
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": self.jwks_uri_value})
        if url == JWKS_URI:
            return httpx.Response(200, json=self.jwks(), headers=self.jwks_headers)
        return httpx.Response(404)

    @property
    def jwks_hits(self) -> int:
        return self.hits[JWKS_URI]

    def claims(self, sub: str = ALLOWED_SUB, **extra: Any) -> dict[str, Any]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": [PUBLIC_URL, "account"],
            "sub": sub,
            "iat": now,
            "exp": now + 900,
            "preferred_username": "reader",
            "scope": "openid scriptorium email",
        }
        claims.update(extra)
        return {k: v for k, v in claims.items() if v is not None}

    def token(self, kid: str = "rsa-1", claims: dict[str, Any] | None = None, **extra: Any) -> str:
        private, alg = self.keys[kid]
        body = claims if claims is not None else self.claims(**extra)
        return jwt.encode(body, private, algorithm=alg, headers={"kid": kid})

    def admin_token(self) -> str:
        return self.token(sub=ADMIN_SUB, realm_access={"roles": ["scriptorium-admin"]})


@pytest.fixture
def issuer(rsa_private: rsa.RSAPrivateKey, ec_private: ec.EllipticCurvePrivateKey) -> FakeIssuer:
    fake = FakeIssuer()
    fake.add_key("rsa-1", rsa_private, "RS256")
    fake.add_key("ec-1", ec_private, "ES256")
    return fake


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def make_validator(
    issuer: FakeIssuer, clock: FakeClock
) -> AsyncIterator[Callable[..., OIDCValidator]]:
    """Builds validators that talk to the fake issuer and read the fake clock."""
    clients: list[httpx.AsyncClient] = []

    def build(settings: Settings | None = None, **kwargs: Any) -> OIDCValidator:
        client = httpx.AsyncClient(transport=httpx.MockTransport(issuer.handler))
        clients.append(client)
        return OIDCValidator(settings or make_settings(), http=client, clock=clock, **kwargs)

    yield build
    for client in clients:
        await client.aclose()


@pytest.fixture
def validator(make_validator: Callable[..., OIDCValidator]) -> OIDCValidator:
    return make_validator()


# ---- the application ----------------------------------------------------------------------


@asynccontextmanager
async def running(app: Starlette) -> AsyncIterator[Starlette]:
    """Run the app's lifespan (the MCP session manager) around a test.

    The lifespan runs in its own task, as under uvicorn: pytest-asyncio may set up
    and tear down a fixture in different tasks, and the SDK's task group must be
    exited by the task that entered it.
    """
    started = asyncio.Event()
    stop = asyncio.Event()

    async def lifespan() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(lifespan())
    waiter = asyncio.create_task(started.wait())
    await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        waiter.cancel()
        task.result()  # re-raise a startup failure
    try:
        yield app
    finally:
        stop.set()
        await task
