"""Error model shared by every module (Architecture §11.0).

Tools turn a :class:`ScriptoriumError` into an MCP tool error ``{code, message, hint}``.
``code`` is one of :data:`ERROR_CODES`; ``message`` and ``hint`` must never contain note
content, secrets or raw tool arguments.
"""

from __future__ import annotations

from typing import Final

ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "not_linked",
        "needs_relink",
        "not_found",
        "ambiguous",
        "stale_hash",
        "quota_exceeded",
        "ocr_cap_reached",
        "pending",
        "refused",
        "confirm_required",
        "sync_anomaly",
        "invalid_argument",
        "internal",
    }
)


class ScriptoriumError(Exception):
    """Base class; subclasses fix ``code``."""

    code: str = "internal"

    def __init__(self, message: str, *, hint: str | None = None, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown error code {self.code!r}")
        super().__init__(message)
        self.message = message
        self.hint = hint

    def as_dict(self) -> dict[str, str]:
        out = {"code": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


class NotLinkedError(ScriptoriumError):
    code = "not_linked"


class NeedsRelinkError(ScriptoriumError):
    code = "needs_relink"


class NotFoundError(ScriptoriumError):
    code = "not_found"


class AmbiguousError(ScriptoriumError):
    code = "ambiguous"

    def __init__(self, message: str, candidates: list[str], *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.candidates = candidates


class StaleHashError(ScriptoriumError):
    code = "stale_hash"


class QuotaExceededError(ScriptoriumError):
    code = "quota_exceeded"


class PendingError(ScriptoriumError):
    code = "pending"


class RefusedError(ScriptoriumError):
    code = "refused"


class ConfirmRequiredError(ScriptoriumError):
    code = "confirm_required"


class SyncAnomalyError(ScriptoriumError):
    code = "sync_anomaly"


class InvalidArgumentError(ScriptoriumError):
    code = "invalid_argument"


class PathEscapeError(RefusedError):
    """A resolved path left the account root. Always a bug or an attack; never retried."""
