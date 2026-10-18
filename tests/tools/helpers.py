"""The M1 tools served by the real app over a temporary ``/data`` (tool tests).

``serve`` wraps a :class:`~tests.worker.helpers.Linked` account (synced from the fake cloud by
the real worker) in :class:`Services` and the ASGI app, and calls tools through the MCP SDK
client with tokens from the fake issuer: the same path a Claude client takes.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp_types import CallToolResult, ImageContent, TextContent
from starlette.applications import Starlette

from scriptorium.auth.oidc import OIDCValidator
from scriptorium.config import Settings
from scriptorium.errors import RefusedError
from scriptorium.server import InMemoryDownloadStore, create_app
from scriptorium.tools._services import Services
from tests.cloud.fake_cloud import DEVICE_TOKEN
from tests.conftest import FakeIssuer, running
from tests.test_server import mcp_session

IDENTITY = "a" * 64  # what tests.worker.helpers.link records for the linked account
BAD_CODE = "expired1"


@dataclass
class Registrar:
    """Stands in for reMarkable's device registration: counts calls, never sees a network."""

    calls: list[str] = field(default_factory=list)
    token: str = DEVICE_TOKEN

    def __call__(self, code: str, description: str) -> str:
        self.calls.append(description)
        if code == BAD_CODE:
            raise RefusedError("reMarkable did not accept the code", hint="Get a new code.")
        return self.token


@dataclass
class Served:
    settings: Settings
    services: Services
    app: Starlette
    downloads: InMemoryDownloadStore
    issuer: FakeIssuer
    registrar: Registrar
    identities: dict[str, str | None]

    async def call(
        self, name: str, arguments: dict[str, Any] | None = None, *, token: str | None = None
    ) -> CallToolResult:
        async with mcp_session(self.app, token or self.issuer.token()) as (client, _):
            return await client.call_tool(name, arguments or {})

    async def ok(
        self, name: str, arguments: dict[str, Any] | None = None, *, token: str | None = None
    ) -> dict[str, Any]:
        """Call a JSON tool and return its structured result; fails on a tool error."""
        result = await self.call(name, arguments, token=token)
        assert not result.is_error, text_of(result)
        assert result.structured_content is not None
        return dict(result.structured_content)

    async def error(
        self, name: str, arguments: dict[str, Any] | None = None, *, token: str | None = None
    ) -> dict[str, Any]:
        """Call a tool that must fail and return its ``{code, message, hint, ...}``."""
        result = await self.call(name, arguments, token=token)
        assert result.is_error, text_of(result)
        payload = json.loads(text_of(result))
        assert isinstance(payload, dict)
        return payload

    def audit_rows(self) -> list[tuple[Any, ...]]:
        """Every audit row, every column, as stored."""
        import sqlite3

        from scriptorium.users.store import global_db_path

        conn = sqlite3.connect(global_db_path(self.settings.data_dir))
        try:
            return list(conn.execute("SELECT * FROM audit ORDER BY rowid"))
        finally:
            conn.close()


def text_of(result: CallToolResult, index: int = 0) -> str:
    block = result.content[index]
    assert isinstance(block, TextContent), block
    return block.text


def image_of(result: CallToolResult) -> bytes:
    images = [block for block in result.content if isinstance(block, ImageContent)]
    assert len(images) == 1, result.content
    assert images[0].mime_type == "image/png"
    return base64.b64decode(images[0].data)


def meta_of(result: CallToolResult) -> dict[str, Any]:
    """The JSON object in a result's text blocks (read_page puts it after its note)."""
    for block in result.content:
        if isinstance(block, TextContent):
            start = block.text.find("{")
            if start != -1:
                try:
                    value = json.loads(block.text[start:])
                except ValueError:
                    continue
                if isinstance(value, dict):
                    return value
    raise AssertionError("no JSON object in the result")


@asynccontextmanager
async def serve(
    settings: Settings,
    issuer: FakeIssuer,
    make_validator: Callable[..., OIDCValidator],
    **services: Any,
) -> AsyncIterator[Served]:
    """The app with the M1 tools over ``settings.data_dir`` (migrated by Services.open)."""
    registrar = Registrar()
    identities: dict[str, str | None] = {DEVICE_TOKEN: IDENTITY}

    def identify(token: str) -> str | None:
        return identities.get(token)

    opened = Services.open(
        settings,
        register_device=registrar,
        identify=identify,
        poll_interval_s=0.01,
        **services,
    )
    downloads = InMemoryDownloadStore()
    app = create_app(
        settings, validator=make_validator(settings), downloads=downloads, services=opened
    )
    try:
        async with running(app):
            yield Served(settings, opened, app, downloads, issuer, registrar, identities)
    finally:
        opened.close()
