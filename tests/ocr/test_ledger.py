"""Spend accounting: cost per call, UTC calendar months, the three caps (§8.1)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from scriptorium.auth.oidc import User, uid_hash_for
from scriptorium.db.migrations import SchemaMismatch
from scriptorium.errors import InvalidArgumentError
from scriptorium.ocr.ledger import Allowance, Spend, SpendLedger, cost_usd, month_bounds
from scriptorium.users.store import GlobalStore, global_db_path
from tests.conftest import ISSUER, make_settings

OCT = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def make_user(sub: str) -> User:
    return User(
        iss=ISSUER,
        sub=sub,
        uid_hash=uid_hash_for(ISSUER, sub),
        display_name=sub,
        email=None,
        email_verified=False,
        is_admin=False,
        scopes=frozenset(),
    )


@pytest.fixture
def store(tmp_path: Path) -> GlobalStore:
    return GlobalStore.open(global_db_path(tmp_path), migrate=True)


@pytest.fixture
def ledger(tmp_path: Path, store: GlobalStore) -> SpendLedger:
    return SpendLedger.open(global_db_path(tmp_path), migrate=False)


@pytest.fixture
def alice(store: GlobalStore) -> str:
    return store.ensure_user(make_user("alice")).uid


@pytest.fixture
def bob(store: GlobalStore) -> str:
    return store.ensure_user(make_user("bob")).uid


def spend(ledger: SpendLedger, uid: str, usd: float, *, mode: str = "server", at: datetime = OCT):
    ledger.record(
        uid=uid,
        mode=mode,  # type: ignore[arg-type]
        model="claude-sonnet-5-5",
        input_tokens=2300,
        output_tokens=150,
        cost_usd=usd,
        at=at,
    )


# ---- cost -----------------------------------------------------------------------------------


def test_cost_at_the_default_list_price() -> None:
    settings = make_settings()
    assert cost_usd(settings, 1_000_000, 0) == pytest.approx(2.0)
    assert cost_usd(settings, 0, 1_000_000) == pytest.approx(10.0)
    # One typical page: 2300 input tokens, 150 output tokens (thinking included).
    assert cost_usd(settings, 2300, 150) == pytest.approx(0.0046 + 0.0015)
    assert cost_usd(settings, 2300, 150, batch=True) == pytest.approx((0.0046 + 0.0015) / 2)


def test_cost_follows_overridden_prices() -> None:
    settings = make_settings(ocr_price_in_per_mtok=3, ocr_price_out_per_mtok=15)
    assert cost_usd(settings, 1_000_000, 1_000_000) == pytest.approx(18.0)


@pytest.mark.parametrize(("i", "o"), [(-1, 0), (0, -1), (True, 0), (1.5, 0)])
def test_cost_refuses_odd_token_counts(i: object, o: object) -> None:
    with pytest.raises(InvalidArgumentError):
        cost_usd(make_settings(), i, o)  # type: ignore[arg-type]


# ---- months ---------------------------------------------------------------------------------


def test_month_bounds() -> None:
    assert month_bounds(OCT) == (
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC),
    )
    dec = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    assert month_bounds(dec)[1] == datetime(2027, 1, 1, tzinfo=UTC)
    # 1 November 08:00 in Sydney is still 31 October in UTC.
    sydney = datetime(2026, 11, 1, 8, 0, tzinfo=timezone(timedelta(hours=11)))
    assert month_bounds(sydney)[0] == datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(InvalidArgumentError):
        month_bounds(datetime(2026, 10, 1))


def test_month_sums_one_utc_calendar_month(ledger: SpendLedger, alice: str, bob: str) -> None:
    last_oct = datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=UTC)
    first_nov = datetime(2026, 11, 1, tzinfo=UTC)
    spend(ledger, alice, 1.0, at=datetime(2026, 10, 1, tzinfo=UTC))
    spend(ledger, alice, 2.0, at=last_oct)
    spend(ledger, alice, 4.0, at=first_nov)
    spend(ledger, alice, 0.5, mode="user")
    spend(ledger, bob, 8.0)
    spend(ledger, alice, 16.0, at=datetime(2026, 9, 30, 23, 59, tzinfo=UTC))
    assert ledger.month(uid=alice, mode="server", at=OCT) == Spend(3.0, 2, 4600, 300)
    assert ledger.month(uid=alice, mode=None, at=OCT).cost_usd == pytest.approx(3.5)
    assert ledger.month(uid=None, mode="server", at=OCT).cost_usd == pytest.approx(11.0)
    assert ledger.month(uid=None, mode=None, at=first_nov) == Spend(4.0, 1, 2300, 150)
    assert ledger.month(uid=bob, mode="user", at=OCT) == Spend(0.0, 0, 0, 0)


@pytest.mark.parametrize(
    "change",
    [
        {"uid": "reader@example.com"},
        {"uid": "F" * 16},
        {"mode": "lazy"},
        {"model": "claude sonnet"},
        {"input_tokens": -1},
        {"output_tokens": 1.5},
        {"cost_usd": -0.01},
        {"cost_usd": math.nan},
        {"cost_usd": math.inf},
        {"at": datetime(2026, 10, 1)},
    ],
)
def test_record_validates(ledger: SpendLedger, alice: str, change: dict[str, object]) -> None:
    row: dict[str, object] = {
        "uid": alice,
        "mode": "server",
        "model": "claude-sonnet-5-5",
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_usd": 0.01,
        "at": OCT,
    }
    row.update(change)
    with pytest.raises(InvalidArgumentError):
        ledger.record(**row)  # type: ignore[arg-type]
    assert ledger.month(uid=None, mode=None, at=OCT).pages == 0


def test_open_without_migrate_needs_the_current_schema(tmp_path: Path) -> None:
    with pytest.raises(SchemaMismatch):
        SpendLedger.open(tmp_path / "absent.sqlite", migrate=False)


# ---- caps -----------------------------------------------------------------------------------


def test_allowance_with_room(ledger: SpendLedger, store: GlobalStore, alice: str) -> None:
    settings = make_settings()  # caps: 20 global, 10 per user
    spend(ledger, alice, 4.0)
    assert ledger.allowance(settings, store, alice, OCT) == Allowance(16.0, 6.0, None, False)


def test_global_cap_counts_every_user(
    ledger: SpendLedger, store: GlobalStore, alice: str, bob: str
) -> None:
    settings = make_settings()
    spend(ledger, bob, 20.0)
    allowance = ledger.allowance(settings, store, alice, OCT, mode="server")
    assert (allowance.global_remaining_usd, allowance.user_remaining_usd) == (0.0, 10.0)
    assert allowance.degraded == "global"
    # A user on their own key is not stopped by the server's caps.
    assert ledger.allowance(settings, store, alice, OCT, mode="user").degraded is False


def test_user_cap(ledger: SpendLedger, store: GlobalStore, alice: str, bob: str) -> None:
    settings = make_settings()
    spend(ledger, alice, 10.0)
    assert ledger.allowance(settings, store, alice, OCT, mode="server").degraded == "user"
    assert ledger.allowance(settings, store, bob, OCT, mode="server").degraded is False
    # Own-key spend does not count against the server caps.
    spend(ledger, bob, 50.0, mode="user")
    assert ledger.allowance(settings, store, bob, OCT, mode="server").degraded is False


def test_caps_reset_with_the_month(ledger: SpendLedger, store: GlobalStore, alice: str) -> None:
    settings = make_settings()
    spend(ledger, alice, 10.0, at=datetime(2026, 9, 30, 23, 0, tzinfo=UTC))
    assert ledger.allowance(settings, store, alice, OCT, mode="server").degraded is False


def test_zero_cap_means_no_spend(ledger: SpendLedger, store: GlobalStore, alice: str) -> None:
    settings = make_settings(ocr_monthly_cap_usd=0)
    assert ledger.allowance(settings, store, alice, OCT, mode="server").degraded == "global"


def test_own_cap(ledger: SpendLedger, store: GlobalStore, alice: str) -> None:
    settings = make_settings()
    store.set_user_ocr_cap(alice, 5.0)
    spend(ledger, alice, 3.0, mode="user")
    allowance = ledger.allowance(settings, store, alice, OCT, mode="user")
    assert (allowance.own_remaining_usd, allowance.degraded) == (2.0, False)
    spend(ledger, alice, 2.5, mode="user")
    allowance = ledger.allowance(settings, store, alice, OCT, mode="user")
    assert (allowance.own_remaining_usd, allowance.degraded) == (0.0, "own")
    # The conservative default applies every cap; server ones first.
    assert ledger.allowance(settings, store, alice, OCT).degraded == "own"
    spend(ledger, alice, 10.0)
    assert ledger.allowance(settings, store, alice, OCT).degraded == "user"
    store.set_user_ocr_cap(alice, None)
    assert ledger.allowance(settings, store, alice, OCT, mode="user").own_remaining_usd is None
