"""Invariants of the Keycloak reference realm (Architecture §5.3).

Pure JSON assertions; no Keycloak needed. S2 checks the same realm against a
running server.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

DOCKER_DIR = Path(__file__).resolve().parents[2] / "docker"
REALM_PATH = DOCKER_DIR / "keycloak" / "realm-scriptorium.json"
DOTENV_EXAMPLE = DOCKER_DIR / ".env.example"

CLIENT_REDIRECTS = {
    "scriptorium-claude": ["https://claude.ai/api/mcp/auth_callback"],
    "scriptorium-claude-code": ["http://localhost/callback", "http://127.0.0.1/callback"],
}

# Every ${...} the import-time replacer will see. Anything else would be filled
# from (or leak) an arbitrary container environment variable.
ALLOWED_PLACEHOLDERS = {"SCRIPTORIUM_AUDIENCE", "KC_HOSTNAME", "client_id"}
PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")


@pytest.fixture(scope="module")
def realm() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(REALM_PATH.read_text(encoding="utf-8"))
    return data


def _client(realm: dict[str, Any], client_id: str) -> dict[str, Any]:
    matches = [c for c in realm["clients"] if c["clientId"] == client_id]
    assert len(matches) == 1, client_id
    return matches[0]


def _scope(realm: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [s for s in realm["clientScopes"] if s["name"] == name]
    assert len(matches) == 1, name
    return matches[0]


def _keycloak_substitute(text: str, env: dict[str, str]) -> str:
    """Mimic Keycloak 26.4's StringPropertyReplacer with a getenv resolver."""

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in env:
            return env[key]
        name, sep, default = key.partition(":")
        if sep and name in env:
            return env[name]
        if sep:
            return default
        return match.group(0)

    return PLACEHOLDER.sub(repl, text)


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def test_realm_identity(realm: dict[str, Any]) -> None:
    assert realm["realm"] == "scriptorium"
    assert realm["enabled"] is True
    assert realm["sslRequired"] == "external"


def test_registration_and_login_settings(realm: dict[str, Any]) -> None:
    assert realm["registrationAllowed"] is False
    assert realm["loginWithEmailAllowed"] is True
    assert realm["verifyEmail"] is True
    assert realm["bruteForceProtected"] is True
    assert realm["duplicateEmailsAllowed"] is False


def test_lifetimes_are_exact(realm: dict[str, Any]) -> None:
    assert realm["accessTokenLifespan"] == 900
    assert realm["ssoSessionIdleTimeout"] == 43200
    assert realm["ssoSessionMaxLifespan"] == 604800
    assert realm["offlineSessionIdleTimeout"] == 2592000
    assert realm["offlineSessionMaxLifespanEnabled"] is False
    assert realm["revokeRefreshToken"] is True
    assert realm["refreshTokenMaxReuse"] == 1


def test_signature_algorithm_is_one_scriptorium_accepts(realm: dict[str, Any]) -> None:
    assert realm["defaultSignatureAlgorithm"] in {"RS256", "ES256"}


def test_no_users_and_no_identity_providers(realm: dict[str, Any]) -> None:
    assert not realm.get("users")
    assert not realm.get("federatedUsers")
    assert not realm.get("identityProviders")
    assert not realm.get("identityProviderMappers")


def test_admin_role_exists(realm: dict[str, Any]) -> None:
    names = [r["name"] for r in realm["roles"]["realm"]]
    assert "scriptorium-admin" in names


def test_exactly_the_two_clients(realm: dict[str, Any]) -> None:
    assert sorted(c["clientId"] for c in realm["clients"]) == sorted(CLIENT_REDIRECTS)


@pytest.mark.parametrize("client_id", sorted(CLIENT_REDIRECTS))
def test_client_is_public_pkce_s256_code_flow_only(realm: dict[str, Any], client_id: str) -> None:
    client = _client(realm, client_id)
    assert client["enabled"] is True
    assert client["protocol"] == "openid-connect"
    assert client["publicClient"] is True
    assert client.get("bearerOnly", False) is False
    assert client["standardFlowEnabled"] is True
    assert client["implicitFlowEnabled"] is False
    assert client["directAccessGrantsEnabled"] is False
    assert client["serviceAccountsEnabled"] is False
    assert client["consentRequired"] is False
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["attributes"].get("oauth2.device.authorization.grant.enabled") == "false"
    assert client["attributes"].get("oidc.ciba.grant.enabled") == "false"
    assert client["webOrigins"] == ["+"]


@pytest.mark.parametrize(("client_id", "redirects"), sorted(CLIENT_REDIRECTS.items()))
def test_client_redirect_uris_are_exact(
    realm: dict[str, Any], client_id: str, redirects: list[str]
) -> None:
    client = _client(realm, client_id)
    assert client["redirectUris"] == redirects
    for uri in client["redirectUris"]:
        assert "*" not in uri


def test_loopback_redirects_have_no_port(realm: dict[str, Any]) -> None:
    # Keycloak treats a port-less http loopback redirect as matching any port.
    for uri in _client(realm, "scriptorium-claude-code")["redirectUris"]:
        assert re.fullmatch(r"http://(localhost|127\.0\.0\.1)/callback", uri), uri


@pytest.mark.parametrize("client_id", sorted(CLIENT_REDIRECTS))
def test_client_scopes(realm: dict[str, Any], client_id: str) -> None:
    client = _client(realm, client_id)
    assert "scriptorium" in client["defaultClientScopes"]
    assert "basic" in client["defaultClientScopes"]
    assert "offline_access" in client["optionalClientScopes"]
    defined = {s["name"] for s in realm["clientScopes"]} | {"offline_access"}
    for name in client["defaultClientScopes"] + client["optionalClientScopes"]:
        assert name in defined, name


def test_scriptorium_scope_is_a_realm_default(realm: dict[str, Any]) -> None:
    assert "scriptorium" in realm["defaultDefaultClientScopes"]
    assert "scriptorium" not in realm.get("defaultOptionalClientScopes", [])
    for name in realm["defaultDefaultClientScopes"]:
        _scope(realm, name)


def test_offline_access_left_to_keycloak(realm: dict[str, Any]) -> None:
    # Keycloak creates offline_access (scope + role + scope mapping) itself when the
    # export does not define it; defining it half-way would lose the role mapping.
    assert "offline_access" not in {s["name"] for s in realm["clientScopes"]}
    assert "offline_access" not in {r["name"] for r in realm["roles"]["realm"]}


def test_scriptorium_scope_mappers(realm: dict[str, Any]) -> None:
    mappers = _scope(realm, "scriptorium")["protocolMappers"]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for mapper in mappers:
        by_type.setdefault(mapper["protocolMapper"], []).append(mapper)

    (audience,) = by_type["oidc-audience-mapper"]
    assert audience["config"]["included.custom.audience"] == "${SCRIPTORIUM_AUDIENCE}"
    assert audience["config"]["access.token.claim"] == "true"
    assert "included.client.audience" not in audience["config"]

    (roles,) = by_type["oidc-usermodel-realm-role-mapper"]
    assert roles["config"]["claim.name"] == "realm_access.roles"
    assert roles["config"]["multivalued"] == "true"
    assert roles["config"]["access.token.claim"] == "true"

    claims = {
        m["config"]["claim.name"]: m
        for m in mappers
        if m["protocolMapper"]
        in {"oidc-usermodel-attribute-mapper", "oidc-usermodel-property-mapper"}
    }
    assert claims["email"]["config"]["user.attribute"] == "email"
    assert claims["email"]["config"]["access.token.claim"] == "true"
    assert claims["email_verified"]["config"]["user.attribute"] == "emailVerified"
    assert claims["email_verified"]["config"]["jsonType.label"] == "boolean"
    assert claims["email_verified"]["config"]["access.token.claim"] == "true"


def test_admin_role_reaches_token_without_full_scope(realm: dict[str, Any]) -> None:
    mappings = [m for m in realm["scopeMappings"] if m.get("clientScope") == "scriptorium"]
    assert any("scriptorium-admin" in m["roles"] for m in mappings)


def test_basic_scope_carries_sub(realm: dict[str, Any]) -> None:
    types = {m["protocolMapper"] for m in _scope(realm, "basic")["protocolMappers"]}
    assert "oidc-sub-mapper" in types


def test_frontend_url_placeholder(realm: dict[str, Any]) -> None:
    assert realm["attributes"]["frontendUrl"] == "${KC_HOSTNAME}"


def test_only_known_placeholders() -> None:
    found = set(PLACEHOLDER.findall(REALM_PATH.read_text(encoding="utf-8")))
    assert found == ALLOWED_PLACEHOLDERS


def test_example_env_substitutes_to_the_example_audience() -> None:
    env = _dotenv(DOTENV_EXAMPLE)
    rendered = _keycloak_substitute(REALM_PATH.read_text(encoding="utf-8"), env)
    realm = json.loads(rendered)
    (audience,) = [
        m
        for m in _scope(realm, "scriptorium")["protocolMappers"]
        if m["protocolMapper"] == "oidc-audience-mapper"
    ]
    assert audience["config"]["included.custom.audience"] == "https://mcp.example.com/mcp"
    assert realm["attributes"]["frontendUrl"] == "https://auth.example.com"
    # Keycloak's own per-client template must survive the import-time pass.
    assert "resource_access.${client_id}.roles" in rendered


def test_no_secrets_in_realm(realm: dict[str, Any]) -> None:
    for client in realm["clients"]:
        assert "secret" not in client
    text = REALM_PATH.read_text(encoding="utf-8").lower()
    assert '"password"' not in text
    assert '"credentials"' not in text
