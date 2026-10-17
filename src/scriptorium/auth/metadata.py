"""Protected-resource metadata (RFC 9728) and the 401/403 contract (Architecture §5.1).

The JSON and the ``WWW-Authenticate`` values here are part of the wire contract
with Claude's OAuth client; tests pin them byte-for-byte.
"""

from __future__ import annotations

from typing import Final, Literal

from starlette.responses import JSONResponse

from scriptorium.config import Settings

SCOPE: Final = "scriptorium"
DOCUMENTATION_URL: Final = "https://github.com/MilkyEngineer/scriptorium"
PRM_PATH: Final = "/.well-known/oauth-protected-resource"

BearerError = Literal["invalid_token", "insufficient_scope"]


def protected_resource_metadata(settings: Settings) -> dict[str, object]:
    """The exact protected-resource metadata document from §5.1."""
    return {
        "resource": settings.public_url,
        "authorization_servers": [settings.oidc_issuer],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_documentation": DOCUMENTATION_URL,
    }


def resource_metadata_url(settings: Settings) -> str:
    """Where clients find the metadata (advertised in ``WWW-Authenticate``)."""
    return f"{settings.public_origin}{PRM_PATH}"


def www_authenticate(settings: Settings, error: BearerError | None = None) -> str:
    """A single ``WWW-Authenticate`` value for the bearer challenge."""
    value = f'Bearer resource_metadata="{resource_metadata_url(settings)}", scope="{SCOPE}"'
    if error is not None:
        value += f', error="{error}"'
    return value


_NO_STORE: Final = {"Cache-Control": "no-store"}


def missing_token_response(settings: Settings) -> JSONResponse:
    """401 for a request without a bearer token: start the OAuth flow."""
    return JSONResponse(
        {"error": "unauthorized", "error_description": "A bearer token is required."},
        status_code=401,
        headers={"WWW-Authenticate": www_authenticate(settings), **_NO_STORE},
    )


def invalid_token_response(settings: Settings) -> JSONResponse:
    """401 for an invalid or expired token: the client should refresh."""
    return JSONResponse(
        {"error": "invalid_token", "error_description": "The bearer token is not valid."},
        status_code=401,
        headers={"WWW-Authenticate": www_authenticate(settings, "invalid_token"), **_NO_STORE},
    )


def insufficient_scope_response(settings: Settings) -> JSONResponse:
    """403 for a valid token that lacks the required scope."""
    return JSONResponse(
        {"error": "insufficient_scope", "error_description": "The token lacks a required scope."},
        status_code=403,
        headers={
            "WWW-Authenticate": www_authenticate(settings, "insufficient_scope"),
            **_NO_STORE,
        },
    )


def forbidden_response() -> JSONResponse:
    """403 for a valid token whose user is not on the allowlist.

    No bearer challenge: refreshing or re-consenting cannot help, only the operator can.
    """
    return JSONResponse(
        {
            "error": "access_denied",
            "error_description": "This account is not allowed on this server; ask its operator.",
        },
        status_code=403,
        headers=_NO_STORE,
    )


def unavailable_response() -> JSONResponse:
    """503 when the issuer cannot be reached to validate tokens."""
    return JSONResponse(
        {
            "error": "temporarily_unavailable",
            "error_description": "The identity provider is not reachable; try again shortly.",
        },
        status_code=503,
        headers={"Retry-After": "30", **_NO_STORE},
    )
