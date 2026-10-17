"""Structure of docker/compose.example.yml and the env examples (Architecture §12, §13).

The compose checks need PyYAML, which is not a project dependency; they skip
cleanly without it. Run them with:

    uv run --with pyyaml pytest -q tests/docker
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

DOCKER_DIR = Path(__file__).resolve().parents[2] / "docker"
COMPOSE_PATH = DOCKER_DIR / "compose.example.yml"
SCRIPTORIUM_ENV = DOCKER_DIR / "scriptorium.env.example"
DOTENV = DOCKER_DIR / ".env.example"
CREATE_ADMIN = DOCKER_DIR / "keycloak" / "create-admin.sh"

CORE = ("scriptorium", "scriptorium-worker")
KEYCLOAK = ("keycloak", "keycloak-db")
# Third-party images: exact x.y.z (or PostgreSQL's x.y) plus an optional variant.
EXACT_THIRD_PARTY = re.compile(
    r"^(quay\.io/keycloak/keycloak:\d+\.\d+\.\d+|postgres:\d+\.\d+(-[a-z0-9.]+)?)$"
)
# Our own images track a major version (§13: "`1` tracks the latest 1.x").
OWN_IMAGE = re.compile(r"^ghcr\.io/milkyengineer/scriptorium(-ink)?:\d+(\.\d+){0,2}$")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    data: dict[str, Any] = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return data


def _env_keys(path: Path, *, include_commented: bool) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if include_commented and line.startswith("#"):
            line = line.lstrip("#").strip()
        elif line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def test_core_services_exist_and_are_hardened(compose: dict[str, Any]) -> None:
    services = compose["services"]
    for name in CORE:
        svc = services[name]
        assert svc["read_only"] is True, name
        assert "/tmp" in svc["tmpfs"], name
        assert svc["env_file"] == "scriptorium.env", name
        assert "scriptorium_master_keys" in svc["secrets"], name
        assert svc["image"] == "ghcr.io/milkyengineer/scriptorium:1", name
        assert "profiles" not in svc, name
        assert any(str(v).endswith(":/data") for v in svc["volumes"]), name
        assert "healthcheck" in svc, name
    assert services["scriptorium"]["command"] == "serve"
    assert services["scriptorium-worker"]["command"] == "worker"


def test_worker_is_not_on_the_proxy_network(compose: dict[str, Any]) -> None:
    assert "proxy" not in compose["services"]["scriptorium-worker"]["networks"]
    assert "proxy" in compose["services"]["scriptorium"]["networks"]


def test_optional_services_carry_profiles(compose: dict[str, Any]) -> None:
    services = compose["services"]
    for name in KEYCLOAK:
        assert services[name]["profiles"] == ["keycloak"], name
        assert "healthcheck" in services[name], name
    assert services["scriptorium-ink"]["profiles"] == ["ink"]


def test_keycloak_imports_the_realm(compose: dict[str, Any]) -> None:
    kc = compose["services"]["keycloak"]
    assert kc["command"][0] == "start"
    assert "--import-realm" in kc["command"]
    assert any(
        v.startswith("./keycloak/realm-scriptorium.json:/opt/keycloak/data/import/")
        for v in kc["volumes"]
    )
    env = kc["environment"]
    assert env["KC_PROXY_HEADERS"] == "xforwarded"
    assert env["KC_HTTP_ENABLED"] == "true"
    for key in ("KC_HOSTNAME", "KC_HOSTNAME_ADMIN", "SCRIPTORIUM_AUDIENCE", "KC_DB_PASSWORD"):
        assert key in env, key


def test_images_are_pinned(compose: dict[str, Any]) -> None:
    for name, svc in compose["services"].items():
        image = svc["image"]
        assert "latest" not in image, name
        assert EXACT_THIRD_PARTY.match(image) or OWN_IMAGE.match(image), (name, image)


def test_keycloak_is_26_4(compose: dict[str, Any]) -> None:
    assert re.fullmatch(
        r"quay\.io/keycloak/keycloak:26\.4\.\d+", compose["services"]["keycloak"]["image"]
    )
    assert re.fullmatch(
        r"postgres:16\.\d+(-[a-z0-9.]+)?", compose["services"]["keycloak-db"]["image"]
    )


def test_published_ports(compose: dict[str, Any]) -> None:
    services = compose["services"]
    for name, svc in services.items():
        for port in svc.get("ports", []):
            text = str(port)
            assert "9100" not in text, name
            assert "9000" not in text, name
            # Publish address is configurable and defaults to loopback.
            assert text.startswith("${SCRIPTORIUM_PUBLISH_ADDR:-127.0.0.1}:"), (name, text)
    assert services["scriptorium"]["ports"] == ["${SCRIPTORIUM_PUBLISH_ADDR:-127.0.0.1}:8480:8000"]
    assert services["keycloak"]["ports"] == ["${SCRIPTORIUM_PUBLISH_ADDR:-127.0.0.1}:8481:8080"]
    for name in ("scriptorium-worker", "keycloak-db", "scriptorium-ink"):
        assert "ports" not in services[name], name


def test_networks_and_secret(compose: dict[str, Any]) -> None:
    assert {"proxy", "internal"} <= set(compose["networks"])
    assert compose["networks"]["keycloak-db"]["internal"] is True
    assert compose["services"]["keycloak-db"]["networks"] == ["keycloak-db"]
    assert compose["secrets"]["scriptorium_master_keys"]["file"] == "./secrets/master_keys"


def test_scriptorium_env_example_required_values() -> None:
    env = _env_keys(SCRIPTORIUM_ENV, include_commented=False)
    assert env["SCRIPTORIUM_PUBLIC_URL"] == "https://mcp.example.com/mcp"
    assert env["SCRIPTORIUM_OIDC_ISSUER"] == "https://auth.example.com/realms/scriptorium"
    assert env["SCRIPTORIUM_OIDC_AUDIENCE"] == "https://mcp.example.com/mcp"
    assert env["SCRIPTORIUM_OIDC_ADMIN_CLAIM"] == "realm_access.roles"
    assert env["SCRIPTORIUM_OIDC_ADMIN_VALUE"] == "scriptorium-admin"
    assert env["SCRIPTORIUM_OIDC_ALLOWED_USERS"] == ""
    assert env["SCRIPTORIUM_MASTER_KEYS_FILE"] == "/run/secrets/scriptorium_master_keys"
    assert "SCRIPTORIUM_MASTER_KEYS" not in env
    assert "SCRIPTORIUM_ANTHROPIC_API_KEY" not in env


def test_scriptorium_env_example_lists_section_12() -> None:
    documented = _env_keys(SCRIPTORIUM_ENV, include_commented=True)
    for suffix in (
        "PUBLIC_URL", "BIND", "TRUSTED_PROXIES", "METRICS_BIND", "DATA_DIR",
        "OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_USER_CLAIM", "OIDC_NAME_CLAIM",
        "OIDC_ADMIN_CLAIM", "OIDC_ADMIN_VALUE", "OIDC_ALLOWED_USERS",
        "ALLOW_ALL_ISSUER_USERS", "POLL_SECONDS", "SYNC_CONCURRENCY", "SYNC_WORKERS",
        "RENDER_WORKERS", "TOMBSTONE_MAX_PER_POLL", "BLOB_RETENTION_DAYS",
        "SERVER_KEY_USERS", "OCR_MODEL", "OCR_MONTHLY_CAP_USD", "OCR_USER_MONTHLY_CAP_USD",
        "OCR_PRICE_IN_PER_MTOK", "OCR_PRICE_OUT_PER_MTOK", "MAX_INLINE_KB", "MAX_FETCH_MB",
        "PUSH_URL_ALLOWLIST", "INK_SIDECAR_URL", "DEFAULT_QUOTA_GB", "SNAPSHOT_STRATEGY",
        "RESTIC_REPO", "RESTIC_PASSWORD_FILE", "AUDIT_RETENTION_DAYS", "LOG_LEVEL",
    ):  # fmt: skip
        assert f"SCRIPTORIUM_{suffix}" in documented, suffix
    assert any(k.startswith("SCRIPTORIUM_ANTHROPIC_API_KEY") for k in documented)
    assert any(k.startswith("SCRIPTORIUM_MASTER_KEYS") for k in documented)


def test_dotenv_example_has_no_secrets() -> None:
    env = _env_keys(DOTENV, include_commented=False)
    assert env["KEYCLOAK_DB_PASSWORD"] == ""
    assert env["KC_BOOTSTRAP_ADMIN_PASSWORD"] == ""
    assert env["SCRIPTORIUM_PUBLISH_ADDR"] == "127.0.0.1"
    assert env["SCRIPTORIUM_AUDIENCE"] == "https://mcp.example.com/mcp"
    assert env["KC_HOSTNAME"] == "https://auth.example.com"


def test_examples_use_documentation_names_and_addresses() -> None:
    ipv4 = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
    allowed_prefixes = (
        "127.", "0.0.0.0", "172.16.", "192.0.2.", "198.51.100.", "203.0.113.",  # noqa: S104
        "160.79.104.",  # Anthropic egress, from Architecture §5.1
    )  # fmt: skip
    cloudflare = re.compile(r"^(173\.245\.48\.|103\.21\.244\.|103\.22\.200\.)")
    files = [
        COMPOSE_PATH, SCRIPTORIUM_ENV, DOTENV,
        DOCKER_DIR / "proxy" / "nginx.conf.example",
        DOCKER_DIR / "proxy" / "Caddyfile.example",
        DOCKER_DIR / "proxy" / "README.md",
        DOCKER_DIR / "keycloak" / "README.md",
    ]  # fmt: skip
    for path in files:
        text = path.read_text(encoding="utf-8")
        for match in ipv4.finditer(text):
            addr = match.group(0)
            assert addr.startswith(allowed_prefixes) or cloudflare.match(addr), (path.name, addr)
        for host in re.findall(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io)\b", text):
            assert host.endswith(
                ("example.com", "claude.ai", "quay.io", "ghcr.io", "keycloak.org",
                 "cloudflare.com", "microsoftonline.com", "github.com")
            ), (path.name, host)  # fmt: skip


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_create_admin_script_parses() -> None:
    result = subprocess.run(  # noqa: S603
        ["bash", "-n", str(CREATE_ADMIN)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    text = CREATE_ADMIN.read_text(encoding="utf-8")
    assert "set +x" in text
    assert "--reset-password" in text
