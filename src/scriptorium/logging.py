"""Structured JSON logging (Architecture §15) with a closed set of fields.

Scriptorium code logs through :func:`log_event`, which accepts only the field
names in :data:`ALLOWED_FIELDS`. There is deliberately no free-text field: note
content, transcripts, tokens, emails and raw tool arguments have nowhere to go.
Exceptions are reduced to their class and the file:line that raised them;
messages and tracebacks are dropped because they can carry user data.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from types import TracebackType
from typing import Final

ALLOWED_FIELDS: Final = frozenset(
    {
        "uid",  # hashed user id prefix, never the raw sub or email
        "tool",
        "duration_ms",
        "outcome",
        "reason",  # short machine-readable cause, e.g. "bad_audience"
        "acct",
        "job",
        "status",
        "method",
        "path",  # already redacted by the caller (no capability tokens)
        "bytes",
        "host",  # URL host only
        "version",
        "count",
        "tombstoned",  # documents moved to tombstones/ in this poll (a count, never UUIDs)
        "untombstoned",
    }
)
UID_PREFIX_LEN: Final = 12
FieldValue = str | int | float | bool | None

_FIELDS_ATTR = "scriptorium_fields"
_THIRD_PARTY_MAX_CHARS = 300


def uid_prefix(uid_hash: str) -> str:
    """The part of a user's hash that goes into logs."""
    return uid_hash[:UID_PREFIX_LEN]


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
    **fields: FieldValue,
) -> None:
    """Log ``event`` with structured, allowlisted fields.

    ``exc_info=True`` records the current exception's class and location only.
    """
    unknown = fields.keys() - ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"log fields not allowed: {sorted(unknown)}")
    if not logger.isEnabledFor(level):
        return
    clean = {k: v for k, v in fields.items() if v is not None}
    logger.log(level, event, exc_info=exc_info, extra={_FIELDS_ATTR: clean})


def _exception_fields(
    exc_info: tuple[type[BaseException], BaseException, TracebackType | None]
    | tuple[None, None, None],
) -> dict[str, str]:
    exc_type, _exc, tb = exc_info
    if exc_type is None:
        return {}
    out = {"exc_type": exc_type.__qualname__}
    frames = traceback.extract_tb(tb) if tb is not None else []
    if frames:
        last = frames[-1]
        out["exc_where"] = f"{last.filename.rsplit('/', 1)[-1]}:{last.lineno}"
    return out


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, event, then the allowlisted fields."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, FieldValue] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
        }
        fields = getattr(record, _FIELDS_ATTR, None)
        if isinstance(fields, dict):
            out["event"] = str(record.msg)
            out.update(fields)
        else:
            # A library's own log line: keep the logger name and a bounded message.
            out["event"] = "library"
            out["logger"] = record.name
            out["message"] = record.getMessage()[:_THIRD_PARTY_MAX_CHARS]
        if record.exc_info:
            out.update(_exception_fields(record.exc_info))
        return json.dumps(out, separators=(",", ":"), default=str)


_LEVELS: Final = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def configure_logging(level: str = "info") -> None:
    """Route every logger to stderr as JSON and quiet chatty libraries."""
    numeric = _LEVELS.get(level.lower(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(numeric)
    # Libraries that log request details (URLs, session ids, headers) at INFO; the anthropic
    # SDK logs whole request bodies (page images, the prompt) at DEBUG; websockets logs every
    # frame it sends or receives (notification payloads carry account ids) at DEBUG.
    for name in (
        "mcp",
        "httpx",
        "httpx2",
        "httpcore",
        "httpcore2",
        "uvicorn.error",
        "anthropic",
        "websockets",
    ):
        logging.getLogger(name).setLevel(max(numeric, logging.WARNING))
    # rmscene warns "Some data has not been read" once per page written by newer firmware:
    # content-free, but one line per rendered page.
    logging.getLogger("rmscene").setLevel(max(numeric, logging.ERROR))
    # uvicorn's access log prints full paths, which include /dl/<token> capabilities;
    # the server logs requests itself with the token redacted.
    access = logging.getLogger("uvicorn.access")
    access.handlers[:] = []
    access.propagate = False
    access.disabled = True
