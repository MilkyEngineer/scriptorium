"""When a page is still being written (Architecture §7 step 6).

A page that changes on every poll is someone writing on it: transcribing each version would
pay for pages nobody reads. A page is *stable* once it has gone unchanged through a complete
poll after the one that saw it change, or once it has not changed for
:data:`STABLE_AFTER`, whichever comes first. ``pages.stable_since`` is the time the worker
recorded the page's current content (it moves on every change); the worker records the time
of each complete poll under :data:`STATE_OCR_POLL_AT` after refreshing pages, so the next
poll's scan reads it as ``previous_poll_at``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from scriptorium.library.index import PageRow

__all__ = ["STABLE_AFTER", "STATE_OCR_POLL_AT", "is_stable"]

STABLE_AFTER: Final = timedelta(minutes=30)
STATE_OCR_POLL_AT: Final = "ocr_poll_at"
"""``sync_state`` key: when the last complete (``ok``) poll had refreshed its pages."""


def is_stable(page: PageRow, *, now: datetime, previous_poll_at: datetime | None) -> bool:
    """True when ``page`` is unchanged across two complete polls or for 30 minutes.

    ``stable_since <= previous_poll_at``: the content was already there when the previous
    complete poll finished, and this poll found it unchanged. ``now - stable_since >= 30
    min``: covers an account whose polls are rare or keep stopping early. A page with no
    ``stable_since`` (never refreshed) is not stable.
    """
    since = page.stable_since
    if since is None:
        return False
    if previous_poll_at is not None and since <= previous_poll_at:
        return True
    return now - since >= STABLE_AFTER
