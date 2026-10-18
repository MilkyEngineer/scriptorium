"""Failure classes of the cloud client (Architecture §7 step 7).

Each class maps to one way the sync loop reacts: back off, count a token rejection, raise
an anomaly, or fail one document and carry on. ``reason`` is the short machine-readable
cause that goes into the sync report and logs. Messages carry status codes, sizes and
fixed text only: never a token, a response body or a cloud-supplied name.
"""

from __future__ import annotations

from scriptorium.errors import ScriptoriumError


class CloudError(ScriptoriumError):
    """Base class for everything the cloud client raises."""

    code = "internal"
    reason = "cloud_error"

    def __init__(self, message: str, *, status: int | None = None, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.status = status


class TransientCloudError(CloudError):
    """HTTP 5xx, a network failure or a timeout: back off and poll again."""

    reason = "transient"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message, status=status, hint="The reMarkable cloud is unavailable.")
        self.retry_after_s = retry_after_s


class RateLimitedError(TransientCloudError):
    """HTTP 429: stop issuing requests for this poll."""

    reason = "rate_limited"


class TokenRejectedError(CloudError):
    """The token endpoint answered 401/403 for the device token.

    One rejection is not yet ``needs_relink``; the worker decides that after three
    consecutive polls (§7 step 7).
    """

    code = "needs_relink"
    reason = "token_rejected"


class CloudProtocolError(CloudError):
    """A response that does not parse, or contradicts its own content address."""

    code = "sync_anomaly"
    reason = "protocol"


class ResponseTooLargeError(CloudProtocolError):
    """A response, index or blob larger than its cap; refused without reading the rest."""

    code = "refused"
    reason = "too_large"


class CloudNotFoundError(CloudError):
    """HTTP 404/410 for a document index or blob."""

    code = "not_found"
    reason = "not_found"


class CloudRequestError(CloudError):
    """Any other non-retryable HTTP status."""

    code = "refused"
    reason = "http_error"


class BlobVerificationError(CloudError):
    """A blob whose SHA-256 is not the hash it was requested under."""

    code = "refused"
    reason = "hash_mismatch"
