"""Names the server and the worker share about one linked account (§7, §11.1).

The device token lives in the user's ``secrets.enc`` under :func:`device_token_name`. The
worker records its view of the account in the account index's ``sync_state`` under the
``STATE_*`` keys below (beside the sync's own keys in :mod:`scriptorium.cloud.sync`);
``account_status`` reads them and ``connect_remarkable`` clears the relink state.
"""

from __future__ import annotations

from typing import Final

from scriptorium.users.store import ACCT_RE

__all__ = [
    "DEVICE_DESCRIPTION",
    "RELINK_AFTER",
    "STATE_LAST_POLL_AT",
    "STATE_LAST_POLL_OUTCOME",
    "STATE_LAST_SYNC_AT",
    "STATE_NEEDS_RELINK",
    "STATE_TOKEN_REJECTIONS",
    "device_token_name",
]

DEVICE_DESCRIPTION: Final = "desktop-linux"
"""What ``connect_remarkable`` registers the device as (the fork's default)."""

RELINK_AFTER: Final = 3
"""Consecutive polls whose device token is rejected before the account needs relinking."""

STATE_NEEDS_RELINK: Final = "needs_relink"
"""``"true"`` once the token was rejected :data:`RELINK_AFTER` times in a row: polling stops."""
STATE_TOKEN_REJECTIONS: Final = "token_rejections"  # noqa: S105 - a key name, not a secret
"""Consecutive polls that ended ``token_rejected`` (decimal)."""
STATE_LAST_SYNC_AT: Final = "last_sync_at"
"""ISO 8601 time of the last poll that reached the cloud and ended ``ok`` or ``partial``."""
STATE_LAST_POLL_AT: Final = "last_poll_at"
STATE_LAST_POLL_OUTCOME: Final = "last_poll_outcome"
"""The last poll's ``SyncReport.outcome``, or ``watchdog``/``error`` when it did not end."""


def device_token_name(acct: str) -> str:
    """The ``SecretBox`` name of an account's device token."""
    if not ACCT_RE.fullmatch(acct):
        raise ValueError("not an account id")
    return f"device_token:{acct}"
