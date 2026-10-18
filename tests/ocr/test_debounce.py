from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scriptorium.library.index import PageRow
from scriptorium.ocr.debounce import STABLE_AFTER, is_stable

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
DOC = "00000000-0000-4000-8000-000000000001"
PAGE = "00000000-0000-4000-8000-000000000002"


def row(since: datetime | None) -> PageRow:
    return PageRow(PAGE, DOC, 0, "a" * 64, stable_since=since)


def test_unchanged_across_two_complete_polls_is_stable() -> None:
    # Changed in the previous poll (stable_since just before it was recorded), unchanged now.
    page = row(T0)
    assert is_stable(page, now=T0 + timedelta(minutes=5), previous_poll_at=T0)
    assert is_stable(page, now=T0 + timedelta(minutes=5), previous_poll_at=T0 + timedelta(1))


def test_changed_since_the_previous_poll_is_not_stable() -> None:
    page = row(T0 + timedelta(minutes=5))  # this poll saw a new hash
    assert not is_stable(page, now=T0 + timedelta(minutes=5), previous_poll_at=T0)
    assert not is_stable(page, now=T0 + timedelta(minutes=5), previous_poll_at=None)


def test_thirty_minutes_unchanged_is_stable_without_a_previous_poll() -> None:
    page = row(T0)
    assert not is_stable(page, now=T0 + STABLE_AFTER - timedelta(seconds=1), previous_poll_at=None)
    assert is_stable(page, now=T0 + STABLE_AFTER, previous_poll_at=None)
    # A previous poll that predates the change does not make it stable sooner.
    early = T0 - timedelta(minutes=1)
    assert not is_stable(page, now=T0 + timedelta(minutes=10), previous_poll_at=early)
    assert is_stable(page, now=T0 + STABLE_AFTER, previous_poll_at=early)


def test_a_page_never_refreshed_is_not_stable() -> None:
    assert not is_stable(row(None), now=T0 + timedelta(days=1), previous_poll_at=T0)
