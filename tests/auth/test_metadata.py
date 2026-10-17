"""Protected-resource metadata and the exact WWW-Authenticate strings (§5.1)."""

from __future__ import annotations

import json

from scriptorium.auth.metadata import (
    forbidden_response,
    insufficient_scope_response,
    invalid_token_response,
    missing_token_response,
    protected_resource_metadata,
    unavailable_response,
    www_authenticate,
)
from scriptorium.config import Settings
from tests.conftest import make_settings

PRM = "https://mcp.example.com/.well-known/oauth-protected-resource"


def test_prm_exact_json(settings: Settings) -> None:
    document = protected_resource_metadata(settings)
    assert json.dumps(document, separators=(",", ":")) == (
        '{"resource":"https://mcp.example.com/mcp",'
        '"authorization_servers":["https://auth.example.com/realms/scriptorium"],'
        '"scopes_supported":["scriptorium"],'
        '"bearer_methods_supported":["header"],'
        '"resource_documentation":"https://github.com/MilkyEngineer/scriptorium"}'
    )


def test_prm_uses_public_url_byte_exact() -> None:
    settings = make_settings(public_url="https://mcp.example.com:8443/mcp")
    assert protected_resource_metadata(settings)["resource"] == "https://mcp.example.com:8443/mcp"
    assert www_authenticate(settings).startswith(
        'Bearer resource_metadata="https://mcp.example.com:8443/.well-known/'
    )


def test_www_authenticate_no_token(settings: Settings) -> None:
    response = missing_token_response(settings)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{PRM}", scope="scriptorium"'
    )
    assert response.headers["cache-control"] == "no-store"


def test_www_authenticate_invalid_token(settings: Settings) -> None:
    response = invalid_token_response(settings)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{PRM}", scope="scriptorium", error="invalid_token"'
    )


def test_www_authenticate_insufficient_scope(settings: Settings) -> None:
    response = insufficient_scope_response(settings)
    assert response.status_code == 403
    assert response.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{PRM}", scope="scriptorium", error="insufficient_scope"'
    )


def test_forbidden_has_no_challenge() -> None:
    response = forbidden_response()
    assert response.status_code == 403
    assert "www-authenticate" not in response.headers


def test_unavailable_asks_to_retry() -> None:
    response = unavailable_response()
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
