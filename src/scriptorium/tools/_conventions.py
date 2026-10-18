"""Conventions binding for every tool (Architecture §11.0).

Identifiers, 1-based pages, pagination, the text cap, the error model, timestamps,
annotations and the confirmation-token interface live here so every tool module
shares one implementation of each rule.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from scriptorium.auth.oidc import User

# ---- errors ---------------------------------------------------------------------------


class ErrorCode(StrEnum):
    """Tool error codes (§11.0). Clients branch on these; never rename one."""

    NOT_LINKED = "not_linked"
    NEEDS_RELINK = "needs_relink"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    STALE_HASH = "stale_hash"
    QUOTA_EXCEEDED = "quota_exceeded"
    OCR_CAP_REACHED = "ocr_cap_reached"
    PENDING = "pending"
    REFUSED = "refused"
    CONFIRM_REQUIRED = "confirm_required"
    SYNC_ANOMALY = "sync_anomaly"
    INVALID_ARGUMENT = "invalid_argument"


class ToolError(Exception):
    """Raised inside a tool to return ``isError: true`` with ``{code, message, hint}``.

    ``message`` and ``hint`` go to the client, never to logs. ``extra`` adds identifiers the
    client can act on (``job`` for ``pending``, ``candidates`` for ``ambiguous``); it never
    carries content.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        hint: str | None = None,
        *,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = dict(extra or {})


_RESERVED_ERROR_KEYS: Final = frozenset({"code", "message", "hint"})


def error_payload(
    code: ErrorCode,
    message: str,
    hint: str | None = None,
    *,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"code": code.value, "message": message, "hint": hint}
    for key, value in (extra or {}).items():
        if key not in _RESERVED_ERROR_KEYS:
            payload[key] = value
    return payload


def error_result(
    code: ErrorCode,
    message: str,
    hint: str | None = None,
    *,
    extra: Mapping[str, object] | None = None,
) -> CallToolResult:
    """A tool error result: JSON text for the model plus the same object as structured content."""
    payload = error_payload(code, message, hint, extra=extra)
    return CallToolResult(
        content=[TextContent(text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
        is_error=True,
    )


def refused(message: str, hint: str | None = None) -> ToolError:
    return ToolError(ErrorCode.REFUSED, message, hint)


def invalid_argument(message: str, hint: str | None = None) -> ToolError:
    return ToolError(ErrorCode.INVALID_ARGUMENT, message, hint)


# ---- arguments --------------------------------------------------------------------------


class ToolArgs(BaseModel):
    """Base for every tool's arguments: unknown keys are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class NoArgs(ToolArgs):
    """Arguments of a tool that takes none."""


# ---- results --------------------------------------------------------------------------

TEXT_CAP_BYTES: Final = 64 * 1024
"""The spike tools' text cap (64 KB, UTF-8 bytes); S6 measured what clients do above it."""
RESULT_CAP_BYTES: Final = 32 * 1024
"""Text results of the library tools are capped at 32 KB (§11.0): Claude Code persists results
of about 64 KB to a file and shows the model a 2 KB preview (S6)."""


def cap_text(text: str, cap: int = TEXT_CAP_BYTES) -> tuple[str, bool]:
    """Truncate ``text`` to at most ``cap`` UTF-8 bytes on a character boundary."""
    data = text.encode("utf-8")
    if len(data) <= cap:
        return text, False
    return data[:cap].decode("utf-8", errors="ignore"), True


def text_result(text: str, *, cap: int = TEXT_CAP_BYTES) -> CallToolResult:
    """A plain text result; refuses (rather than silently truncates) over the cap."""
    if len(text.encode("utf-8")) > cap:
        raise invalid_argument(
            f"The result exceeds the {cap // 1024} KB text limit.",
            "Request a smaller range or page.",
        )
    return CallToolResult(content=[TextContent(text=text)])


def json_text(payload: Mapping[str, object]) -> str:
    """The compact JSON rendering every JSON result uses."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def json_result(payload: Mapping[str, object], *, cap: int = TEXT_CAP_BYTES) -> CallToolResult:
    """A JSON object result: structured content plus its text rendering."""
    text = json_text(payload)
    if len(text.encode("utf-8")) > cap:
        raise invalid_argument(
            f"The result exceeds the {cap // 1024} KB text limit.",
            "Use a smaller limit or a cursor.",
        )
    return CallToolResult(content=[TextContent(text=text)], structured_content=dict(payload))


# ---- identifiers ------------------------------------------------------------------------

UUID_RE: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
"""A document/page/folder UUID as the cloud writes them (lower-case)."""


def is_uuid(value: str) -> bool:
    return UUID_RE.fullmatch(value) is not None


# ---- pages (1-based in every tool) ------------------------------------------------------


def page_to_index(page: int, page_count: int | None = None) -> int:
    """Convert a 1-based page number from a tool argument to a 0-based index."""
    if page < 1:
        raise invalid_argument("Pages are numbered from 1.")
    if page_count is not None and page > page_count:
        raise invalid_argument(f"The document has {page_count} pages.")
    return page - 1


def index_to_page(index: int) -> int:
    """Convert a 0-based internal index to the 1-based page number tools expose."""
    return index + 1


# ---- pagination ---------------------------------------------------------------------------

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 100
CURSOR_MAX: Final = 20_000
"""Longest cursor accepted: an index cursor carries a name of up to 1024 characters, JSON
escaped (up to 12 characters each) and base64 encoded."""


class PageArgs(ToolArgs):
    """Mixin for list-like tools: ``limit`` (≤ 100, default 50) and an opaque ``cursor``."""

    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum items.")
    cursor: str | None = Field(
        default=None,
        max_length=CURSOR_MAX,
        description="next_cursor from the previous call.",
    )


def encode_cursor(state: Mapping[str, str | int]) -> str:
    """An opaque cursor for ``next_cursor``; the client must pass it back unchanged."""
    raw = json.dumps(dict(state), separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> dict[str, str | int]:
    """Decode a cursor from :func:`encode_cursor`; anything else is ``invalid_argument``."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        state = json.loads(raw)
    except (binascii.Error, ValueError, RecursionError):  # deep nesting: RecursionError
        raise invalid_argument(
            "The cursor is not valid.", "Start again without a cursor."
        ) from None
    if not isinstance(state, dict) or not all(
        isinstance(k, str) and isinstance(v, str | int) for k, v in state.items()
    ):
        raise invalid_argument("The cursor is not valid.", "Start again without a cursor.")
    return state


# ---- timestamps -------------------------------------------------------------------------


def iso_utc(moment: datetime) -> str:
    """ISO 8601 UTC with a ``Z`` suffix."""
    if moment.tzinfo is None:
        raise ValueError("naive datetimes are ambiguous")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso_utc(value: str) -> datetime:
    """Parse a client-supplied ISO 8601 timestamp; naive values are taken as UTC."""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    except (ValueError, OverflowError):  # OverflowError: year 1 or 9999 with an offset
        raise invalid_argument("Timestamps are ISO 8601, e.g. 2026-10-17T09:00:00Z.") from None


# ---- annotations ------------------------------------------------------------------------

READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def tool_annotations(
    *,
    read_only: bool,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )


# ---- confirmation tokens (§11.0 destructive actions) --------------------------------------

CONFIRMATION_TTL_SECONDS: Final = 600


def issue_confirmation_token(
    user: User, tool: str, arguments: Mapping[str, object], summary: Mapping[str, object]
) -> str:
    """Mint a token bound to the user and the exact arguments of a dry run (valid 10 min)."""
    raise NotImplementedError("confirmation tokens arrive with the first destructive tool")


def verify_confirmation_token(
    user: User, tool: str, arguments: Mapping[str, object], token: str
) -> None:
    """Raise ``ToolError(confirm_required)`` unless ``token`` matches this user and arguments."""
    raise NotImplementedError("confirmation tokens arrive with the first destructive tool")
