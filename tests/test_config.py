"""Settings: default deny, *_FILE secrets, validation without echoing values."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from scriptorium.config import ConfigError, Settings, load_settings
from tests.conftest import ISSUER, MASTER_KEY, PUBLIC_URL

BASE_ENV = {
    "SCRIPTORIUM_PUBLIC_URL": PUBLIC_URL,
    "SCRIPTORIUM_OIDC_ISSUER": ISSUER,
    "SCRIPTORIUM_MASTER_KEYS": MASTER_KEY,
    "SCRIPTORIUM_OIDC_ALLOWED_USERS": "sub-1, reader@example.com ,",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_loads_from_environment(env: pytest.MonkeyPatch) -> None:
    settings = load_settings()
    assert settings.public_url == PUBLIC_URL
    assert settings.oidc_allowed_users == ("sub-1", "reader@example.com")
    assert settings.audience == PUBLIC_URL
    assert settings.public_origin == "https://mcp.example.com"
    assert settings.bind_host == "0.0.0.0"  # noqa: S104 - the documented default
    assert settings.bind_port == 8000
    assert settings.trusted_proxies == (ipaddress.ip_network("172.16.0.0/12"),)
    assert settings.data_dir == Path("/data")
    assert settings.oidc_user_claim == "sub"
    assert settings.oidc_name_claim == "preferred_username"
    assert settings.log_level == "info"
    assert settings.max_inline_kb == 256
    assert settings.spike_tools is False
    assert settings.allow_all_issuer_users is False


def test_secrets_and_allowlist_stay_out_of_repr(env: pytest.MonkeyPatch) -> None:
    settings = load_settings()
    text = repr(settings) + str(settings)
    assert MASTER_KEY not in text
    assert "reader@example.com" not in text


# ---- default deny ---------------------------------------------------------------------------


def test_empty_allowlist_refused(env: pytest.MonkeyPatch) -> None:
    env.delenv("SCRIPTORIUM_OIDC_ALLOWED_USERS")
    with pytest.raises(ConfigError) as info:
        load_settings()
    message = str(info.value)
    assert "SCRIPTORIUM_OIDC_ALLOWED_USERS" in message
    assert "SCRIPTORIUM_ALLOW_ALL_ISSUER_USERS" in message


def test_allowlist_of_only_commas_refused(env: pytest.MonkeyPatch) -> None:
    env.setenv("SCRIPTORIUM_OIDC_ALLOWED_USERS", " , ,")
    with pytest.raises(ConfigError, match="OIDC_ALLOWED_USERS"):
        load_settings()


def test_explicit_opt_out_allows_empty_allowlist(env: pytest.MonkeyPatch) -> None:
    env.delenv("SCRIPTORIUM_OIDC_ALLOWED_USERS")
    env.setenv("SCRIPTORIUM_ALLOW_ALL_ISSUER_USERS", "true")
    assert load_settings().allow_all_issuer_users is True


def test_missing_master_keys_refused(env: pytest.MonkeyPatch) -> None:
    env.delenv("SCRIPTORIUM_MASTER_KEYS")
    with pytest.raises(ConfigError, match="gen-master-key"):
        load_settings()


@pytest.mark.parametrize("bad", ["short", "not base64 at all!!", "A" * 43])
def test_invalid_master_key_refused_without_echo(env: pytest.MonkeyPatch, bad: str) -> None:
    env.setenv("SCRIPTORIUM_MASTER_KEYS", f"{MASTER_KEY},{bad}")
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert "key #2" in str(info.value)
    assert bad not in str(info.value)
    assert MASTER_KEY not in str(info.value)


def test_several_master_keys(env: pytest.MonkeyPatch) -> None:
    second = Fernet.generate_key().decode()
    env.setenv("SCRIPTORIUM_MASTER_KEYS", f"{MASTER_KEY}, {second}")
    assert load_settings().master_key_list() == [MASTER_KEY, second]


@pytest.mark.parametrize("missing", ["SCRIPTORIUM_PUBLIC_URL", "SCRIPTORIUM_OIDC_ISSUER"])
def test_required_urls(env: pytest.MonkeyPatch, missing: str) -> None:
    env.delenv(missing)
    with pytest.raises(ConfigError, match=missing):
        load_settings()


# ---- *_FILE ---------------------------------------------------------------------------------


def test_file_variant(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "master_keys"
    secret.write_text(MASTER_KEY + "\n")
    env.delenv("SCRIPTORIUM_MASTER_KEYS")
    env.setenv("SCRIPTORIUM_MASTER_KEYS_FILE", str(secret))
    assert load_settings().master_key_list() == [MASTER_KEY]


def test_file_variant_for_any_field(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.write_text("sub-9,sub-10")
    env.delenv("SCRIPTORIUM_OIDC_ALLOWED_USERS")
    env.setenv("SCRIPTORIUM_OIDC_ALLOWED_USERS_FILE", str(allowed))
    assert load_settings().oidc_allowed_users == ("sub-9", "sub-10")


def test_both_forms_refused(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "master_keys"
    secret.write_text(MASTER_KEY)
    env.setenv("SCRIPTORIUM_MASTER_KEYS_FILE", str(secret))
    with pytest.raises(ConfigError, match="not both"):
        load_settings()


def test_missing_file_reported_without_path_contents(
    env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env.delenv("SCRIPTORIUM_MASTER_KEYS")
    env.setenv("SCRIPTORIUM_MASTER_KEYS_FILE", str(tmp_path / "absent"))
    with pytest.raises(ConfigError, match="cannot read"):
        load_settings()


def test_oversized_file_refused(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "huge"
    secret.write_bytes(b"x" * (64 * 1024 + 1))
    env.delenv("SCRIPTORIUM_MASTER_KEYS")
    env.setenv("SCRIPTORIUM_MASTER_KEYS_FILE", str(secret))
    with pytest.raises(ConfigError, match="larger"):
        load_settings()


# ---- validation -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",
        "https://mcp.example.com/mcp/",
        "https://mcp.example.com/",
        "https://mcp.example.com/other",
        "https://user:pw@mcp.example.com/mcp",
        "https://mcp.example.com/mcp?x=1",
        "https://mcp.example.com/mcp#frag",
        'https://mcp.example.com/mcp"',
        "https://mcp.example.com/m|cp",
        " https://mcp.example.com/mcp",
    ],
)
def test_public_url_rejected(env: pytest.MonkeyPatch, url: str) -> None:
    env.setenv("SCRIPTORIUM_PUBLIC_URL", url)
    with pytest.raises(ConfigError, match="SCRIPTORIUM_PUBLIC_URL"):
        load_settings()


def test_public_url_http_localhost_allowed(env: pytest.MonkeyPatch) -> None:
    env.setenv("SCRIPTORIUM_PUBLIC_URL", "http://localhost:8000/mcp")
    assert load_settings().public_origin == "http://localhost:8000"


def test_issuer_must_be_https(env: pytest.MonkeyPatch) -> None:
    env.setenv("SCRIPTORIUM_OIDC_ISSUER", "http://auth.example.com/realms/x")
    with pytest.raises(ConfigError, match="SCRIPTORIUM_OIDC_ISSUER"):
        load_settings()


def test_audience_override(env: pytest.MonkeyPatch) -> None:
    env.setenv("SCRIPTORIUM_OIDC_AUDIENCE", "scriptorium-api")
    assert load_settings().audience == "scriptorium-api"


def test_admin_claim_and_value_together(env: pytest.MonkeyPatch) -> None:
    env.setenv("SCRIPTORIUM_OIDC_ADMIN_CLAIM", "realm_access.roles")
    with pytest.raises(ConfigError, match="OIDC_ADMIN_VALUE"):
        load_settings()
    env.setenv("SCRIPTORIUM_OIDC_ADMIN_VALUE", "scriptorium-admin")
    assert load_settings().oidc_admin_value == "scriptorium-admin"


def test_required_scopes(env: pytest.MonkeyPatch) -> None:
    assert load_settings().oidc_required_scopes == ()
    env.setenv("SCRIPTORIUM_OIDC_REQUIRED_SCOPES", "scriptorium, offline_access")
    assert load_settings().oidc_required_scopes == ("scriptorium", "offline_access")
    env.setenv("SCRIPTORIUM_OIDC_REQUIRED_SCOPES", "two words")
    with pytest.raises(ConfigError, match="OIDC_REQUIRED_SCOPES"):
        load_settings()


def test_trusted_proxies_csv(env: pytest.MonkeyPatch) -> None:
    env.setenv("SCRIPTORIUM_TRUSTED_PROXIES", "10.0.0.0/8, fd00::/8")
    assert load_settings().trusted_proxies == (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("fd00::/8"),
    )


def test_trusted_proxies_invalid(env: pytest.MonkeyPatch) -> None:
    env.setenv("SCRIPTORIUM_TRUSTED_PROXIES", "10.0.0.1/8")  # host bits set
    with pytest.raises(ConfigError, match="TRUSTED_PROXIES"):
        load_settings()


@pytest.mark.parametrize(
    ("bind", "host", "port"), [("127.0.0.1:9000", "127.0.0.1", 9000), ("[::]:8000", "::", 8000)]
)
def test_bind(env: pytest.MonkeyPatch, bind: str, host: str, port: int) -> None:
    env.setenv("SCRIPTORIUM_BIND", bind)
    settings = load_settings()
    assert (settings.bind_host, settings.bind_port) == (host, port)


@pytest.mark.parametrize("bind", ["8000", ":8000", "0.0.0.0:http", "0.0.0.0:70000"])
def test_bind_invalid(env: pytest.MonkeyPatch, bind: str) -> None:
    env.setenv("SCRIPTORIUM_BIND", bind)
    with pytest.raises(ConfigError, match="SCRIPTORIUM_BIND"):
        load_settings()


def test_log_level_case_insensitive(env: pytest.MonkeyPatch) -> None:
    env.setenv("SCRIPTORIUM_LOG_LEVEL", "DEBUG")
    assert load_settings().log_level == "debug"


def test_settings_are_frozen(env: pytest.MonkeyPatch) -> None:
    settings = load_settings()
    with pytest.raises(ValueError, match="frozen"):
        settings.spike_tools = True


def test_settings_class_direct_errors_hide_input(env: pytest.MonkeyPatch) -> None:
    env.setenv("SCRIPTORIUM_MASTER_KEYS", "s3cret-not-a-key")
    with pytest.raises(ValueError) as info:
        Settings()
    assert "s3cret-not-a-key" not in str(info.value)
