"""Typed settings from the environment (``SCRIPTORIUM_*``), Architecture §12.

Every variable may instead be given as ``SCRIPTORIUM_<NAME>_FILE``: the path of a
file (typically a Docker secret) whose contents are the value. Setting both forms
of the same variable is an error, so a stale secret can never silently win.

The settings object is the single place where default-deny is enforced: it
refuses to build without an allowlist (or the explicit opt-out) and without at
least one valid master key. Validation errors never echo input values, because
some inputs are secrets.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

ENV_PREFIX = "SCRIPTORIUM_"
FILE_SUFFIX = "_FILE"
# Docker's default bridge networks are carved from 172.16.0.0/12.
DEFAULT_TRUSTED_PROXIES = ("172.16.0.0/12",)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# A secret file larger than this is almost certainly the wrong path.
_MAX_SECRET_FILE_BYTES = 64 * 1024

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
LogLevel = Literal["debug", "info", "warning", "error"]


class ConfigError(Exception):
    """Settings are missing or invalid; the message is safe to print (no values)."""


def env_name(field_name: str) -> str:
    """The environment variable that sets ``field_name``."""
    return f"{ENV_PREFIX}{field_name.upper()}"


class _FileEnvSource(PydanticBaseSettingsSource):
    """Reads ``SCRIPTORIUM_<NAME>_FILE`` for every field (Docker secrets)."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        file_var = env_name(field_name) + FILE_SUFFIX
        path = os.environ.get(file_var)
        if not path:
            return None, field_name, False
        if os.environ.get(env_name(field_name)):
            raise ConfigError(f"set either {env_name(field_name)} or {file_var}, not both")
        try:
            with open(path, "rb") as fh:
                raw = fh.read(_MAX_SECRET_FILE_BYTES + 1)
        except OSError as exc:
            raise ConfigError(f"{file_var}: cannot read the file ({type(exc).__name__})") from None
        if len(raw) > _MAX_SECRET_FILE_BYTES:
            raise ConfigError(f"{file_var}: the file is larger than {_MAX_SECRET_FILE_BYTES} bytes")
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise ConfigError(f"{file_var}: the file is not UTF-8 text") from None
        return value, field_name, False

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field_name, field in self.settings_cls.model_fields.items():
            value, key, _ = self.get_field_value(field, field_name)
            if value is not None:
                values[key] = value
        return values


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def _validate_url(value: str, *, require_path: str | None) -> str:
    # No whitespace: a newline separates (iss, sub) in the user hash (Architecture §6).
    if value != value.strip() or any(c in value for c in "\"'<>|` \t\r\n\\"):
        raise ValueError("must be a plain URL without quotes, '|' or whitespace")
    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.scheme == "http" and host in _LOOPBACK_HOSTS:
        pass  # local development only
    elif parts.scheme != "https":
        raise ValueError("must be an https:// URL (http:// only for localhost)")
    if not host or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("must have a host and no credentials, query or fragment")
    if require_path is not None and parts.path != require_path:
        raise ValueError(f"must end in {require_path} with no trailing slash")
    return value


class Settings(BaseSettings):
    """Server settings; see Architecture §12 for each variable."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )

    public_url: str = Field(description="https://mcp.example.com/mcp, byte-exact")
    bind: str = "0.0.0.0:8000"  # the container listens on all interfaces
    trusted_proxies: Annotated[tuple[IPNetwork, ...], NoDecode] = tuple(
        ipaddress.ip_network(n) for n in DEFAULT_TRUSTED_PROXIES
    )
    data_dir: Path = Path("/data")
    master_keys: SecretStr | None = None
    oidc_issuer: str
    oidc_audience: str | None = None
    oidc_user_claim: str = Field(
        default="sub",
        description=(
            "Claim that, with the issuer, identifies a user and names their storage. It must "
            "be immutable at the issuer: a claim users can change lets them take over another "
            "user's library. Anything but `sub` needs OIDC_UNSAFE_USER_CLAIM=true."
        ),
    )
    oidc_unsafe_user_claim: bool = Field(
        default=False,
        description=(
            "Explicit acknowledgement that OIDC_USER_CLAIM is not `sub`; the operator vouches "
            "that the issuer never lets that claim change or be reassigned."
        ),
    )
    oidc_name_claim: str = "preferred_username"
    oidc_admin_claim: str | None = None
    oidc_admin_value: str | None = None
    # repr=False: the list may hold email addresses, which must never reach a log.
    oidc_allowed_users: Annotated[tuple[str, ...], NoDecode] = Field(default=(), repr=False)
    allow_all_issuer_users: bool = False
    # Optional: scopes every token must carry (from `scope` or `scp`). Empty = aud only.
    oidc_required_scopes: Annotated[tuple[str, ...], NoDecode] = ()
    oidc_jwks_max_age_seconds: int = Field(
        default=3600,
        ge=60,
        le=24 * 3600,
        description=(
            "Longest a fetched JWKS is trusted before it is refetched, so a key the issuer "
            "withdraws stops being accepted within this time. A shorter Cache-Control max-age "
            "on the JWKS response wins (never below 60 s)."
        ),
    )
    log_level: LogLevel = "info"
    max_inline_kb: int = Field(default=256, ge=1, le=16 * 1024)
    spike_tools: bool = False

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # No dotenv: compose's env_file injects the environment instead.
        return (init_settings, env_settings, _FileEnvSource(settings_cls))

    @field_validator("trusted_proxies", "oidc_allowed_users", "oidc_required_scopes", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("log_level", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @field_validator("public_url")
    @classmethod
    def _public_url(cls, value: str) -> str:
        return _validate_url(value, require_path="/mcp")

    @field_validator("oidc_issuer")
    @classmethod
    def _issuer(cls, value: str) -> str:
        return _validate_url(value, require_path=None)

    @field_validator("oidc_user_claim", "oidc_name_claim")
    @classmethod
    def _claim_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("oidc_required_scopes")
    @classmethod
    def _scope_tokens(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not s.isprintable() or " " in s or '"' in s or "\\" in s for s in value):
            raise ValueError("each scope must be a single printable token without quotes")
        return value

    @field_validator("bind")
    @classmethod
    def _bind(cls, value: str) -> str:
        _parse_bind(value)
        return value

    @model_validator(mode="after")
    def _identity_claim(self) -> Settings:
        if self.oidc_user_claim != "sub" and not self.oidc_unsafe_user_claim:
            raise ValueError(
                f"{env_name('oidc_user_claim')} is not `sub`. The identity claim must be "
                "immutable at the issuer (for example Entra ID's `oid`), or users could take "
                f"over each other's storage; set {env_name('oidc_unsafe_user_claim')}=true to "
                "confirm that it is."
            )
        return self

    @model_validator(mode="after")
    def _default_deny(self) -> Settings:
        if not self.oidc_allowed_users and not self.allow_all_issuer_users:
            raise ValueError(
                f"{env_name('oidc_allowed_users')} is empty, so nobody could log in. List the "
                f"allowed `sub` values or verified emails, or set "
                f"{env_name('allow_all_issuer_users')}=true to admit every user of the issuer."
            )
        if self.master_keys is None or not self.master_keys.get_secret_value().strip():
            raise ValueError(
                f"{env_name('master_keys')} (or {env_name('master_keys')}{FILE_SUFFIX}) is "
                "required; generate a key with `scriptorium gen-master-key`."
            )
        keys = _split_csv(self.master_keys.get_secret_value())
        assert isinstance(keys, list)
        for position, key in enumerate(keys, start=1):
            try:
                Fernet(key)
            except (ValueError, TypeError):
                raise ValueError(
                    f"{env_name('master_keys')}: key #{position} is not a urlsafe-base64 "
                    "encoded 32-byte key"
                ) from None
        if (self.oidc_admin_claim is None) != (self.oidc_admin_value is None):
            raise ValueError(
                f"set both {env_name('oidc_admin_claim')} and {env_name('oidc_admin_value')}, "
                "or neither"
            )
        return self

    # ---- derived values -------------------------------------------------------------

    @property
    def audience(self) -> str:
        """The value that must appear in a token's ``aud`` (§5.1)."""
        return self.oidc_audience or self.public_url

    @property
    def public_origin(self) -> str:
        """``scheme://host[:port]`` of ``PUBLIC_URL``."""
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def bind_host(self) -> str:
        return _parse_bind(self.bind)[0]

    @property
    def bind_port(self) -> int:
        return _parse_bind(self.bind)[1]

    def master_key_list(self) -> list[str]:
        """The master keys, newest first. Callers must not log them."""
        assert self.master_keys is not None
        keys = _split_csv(self.master_keys.get_secret_value())
        assert isinstance(keys, list)
        return [str(k) for k in keys]


def _parse_bind(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("must be host:port, e.g. 0.0.0.0:8000 or [::]:8000")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host, int(port)


def load_settings(**overrides: Any) -> Settings:
    """Build settings from the environment, turning validation errors into ConfigError.

    The message lists each offending variable and why, never its value.
    """
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        lines = []
        for err in exc.errors(include_input=False, include_url=False):
            loc = ".".join(str(p) for p in err["loc"])
            where = env_name(loc) if loc else "settings"
            message = str(err["msg"]).removeprefix("Value error, ")
            lines.append(f"{where}: {message}" if loc else message)
        raise ConfigError("invalid configuration:\n  " + "\n  ".join(lines)) from None
