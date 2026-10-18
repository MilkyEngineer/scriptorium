from __future__ import annotations

import pytest

from scriptorium.errors import InvalidArgumentError
from scriptorium.library.tombstones import (
    ANOMALY_EMPTY_ROOT,
    ANOMALY_GENERATION_REGRESSED,
    ANOMALY_MASS_DELETE,
    Listing,
    TombstonePlan,
    plan,
)

NONE: frozenset[str] = frozenset()


def ids(n: int, start: int = 0) -> frozenset[str]:
    return frozenset(f"00000000-0000-4000-8000-{i:012x}" for i in range(start, start + n))


def listing(uuids: frozenset[str], *, complete: bool = True, generation: int = 10) -> Listing:
    return Listing(uuids=uuids, complete=complete, generation=generation)


def run(
    library: frozenset[str],
    previous: frozenset[str] | None,
    current: frozenset[str],
    *,
    tombstoned: frozenset[str] = NONE,
    prev_complete: bool = True,
    cur_complete: bool = True,
    prev_gen: int = 10,
    cur_gen: int = 11,
    max_per_poll: int = 50,
) -> TombstonePlan:
    prev = (
        None if previous is None else listing(previous, complete=prev_complete, generation=prev_gen)
    )
    return plan(
        prev,
        listing(current, complete=cur_complete, generation=cur_gen),
        library,
        tombstoned,
        max_per_poll,
    )


def test_absent_from_two_complete_listings_is_tombstoned() -> None:
    lib = ids(100)
    gone = ids(3)
    result = run(lib, lib - gone, lib - gone)
    assert result == TombstonePlan(gone, NONE, None)


def test_absent_from_only_one_listing_is_not_a_candidate() -> None:
    lib = ids(100)
    gone_now = ids(3)
    assert run(lib, lib, lib - gone_now).to_tombstone == NONE
    # ...and a document that only vanished from the previous listing came back.
    assert run(lib, lib - gone_now, lib).to_tombstone == NONE


@pytest.mark.parametrize(
    ("prev_complete", "cur_complete"), [(False, True), (True, False), (False, False)]
)
def test_partial_listing_never_tombstones(prev_complete: bool, cur_complete: bool) -> None:
    lib = ids(100)
    gone = ids(3)
    result = run(
        lib, lib - gone, lib - gone, prev_complete=prev_complete, cur_complete=cur_complete
    )
    assert result == TombstonePlan(NONE, NONE, None)


def test_m1_acceptance_5_partial_listing_gives_zero_tombstones() -> None:
    lib = ids(200)
    # One index fetch failed: the listing is short by one and marked incomplete.
    short = lib - ids(1)
    assert run(lib, lib, short, cur_complete=False).to_tombstone == NONE
    assert run(lib, short, short, prev_complete=False).to_tombstone == NONE


def test_no_previous_listing_never_tombstones() -> None:
    lib = ids(100)
    assert run(lib, None, lib - ids(3)) == TombstonePlan(NONE, NONE, None)


# ---- circuit breaker --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("library_size", "candidates", "trips"),
    [
        (10, 5, False),  # floor of 5: 10 % would be 1
        (10, 6, True),
        (6, 5, False),  # small library: still the floor
        (6, 6, True),
        (55, 5, False),  # 10 % = 5.5, exact comparison, no rounding
        (55, 6, True),
        (100, 10, False),
        (100, 11, True),
        (200, 20, False),
        (200, 21, True),
        (49, 5, False),
        (49, 6, True),
    ],
)
def test_breaker_percentage_boundaries(library_size: int, candidates: int, trips: bool) -> None:
    lib = ids(library_size)
    gone = ids(candidates)
    result = run(lib, lib - gone, lib - gone, max_per_poll=10_000)
    if trips:
        assert result == TombstonePlan(NONE, NONE, ANOMALY_MASS_DELETE)
    else:
        assert result == TombstonePlan(gone, NONE, None)


@pytest.mark.parametrize(
    ("max_per_poll", "candidates", "trips"),
    [(50, 50, False), (50, 51, True), (3, 3, False), (3, 4, True), (0, 0, False), (0, 1, True)],
)
def test_breaker_max_per_poll_boundaries(max_per_poll: int, candidates: int, trips: bool) -> None:
    lib = ids(10_000)  # 10 % = 1000, so only max_per_poll can trip
    gone = ids(candidates)
    result = run(lib, lib - gone, lib - gone, max_per_poll=max_per_poll)
    assert (result.anomaly == ANOMALY_MASS_DELETE) is trips
    assert result.to_tombstone == (NONE if trips else gone)


def test_m1_acceptance_6_sixty_percent_missing_trips_breaker() -> None:
    lib = ids(100)
    gone = ids(60)
    result = run(lib, lib - gone, lib - gone)
    assert result.anomaly == ANOMALY_MASS_DELETE
    assert result.to_tombstone == NONE


def test_breaker_still_reports_untombstones() -> None:
    lib = ids(100)
    back = ids(2, start=500)
    gone = ids(60)
    result = run(lib, lib - gone, (lib - gone) | back, tombstoned=back)
    assert result == TombstonePlan(NONE, back, ANOMALY_MASS_DELETE)


# ---- anomalies --------------------------------------------------------------------------------


def test_generation_regression_is_an_anomaly_with_an_empty_plan() -> None:
    lib = ids(100)
    back = ids(1, start=500)
    result = run(lib, lib - ids(3), (lib - ids(3)) | back, tombstoned=back, prev_gen=10, cur_gen=9)
    assert result == TombstonePlan(NONE, NONE, ANOMALY_GENERATION_REGRESSED)


def test_same_generation_is_not_a_regression() -> None:
    lib = ids(100)
    assert run(lib, lib, lib, prev_gen=7, cur_gen=7).anomaly is None


def test_generation_regression_counts_even_on_partial_listings() -> None:
    lib = ids(10)
    result = run(lib, lib, lib, prev_complete=False, cur_complete=False, prev_gen=5, cur_gen=4)
    assert result.anomaly == ANOMALY_GENERATION_REGRESSED


def test_empty_root_after_non_empty_is_an_anomaly() -> None:
    lib = ids(3)  # below the breaker floor: only the empty-root rule can stop this
    assert run(lib, lib, NONE) == TombstonePlan(NONE, NONE, ANOMALY_EMPTY_ROOT)


def test_empty_root_without_previous_listing_uses_the_library() -> None:
    assert run(ids(3), None, NONE).anomaly == ANOMALY_EMPTY_ROOT
    assert run(NONE, None, NONE) == TombstonePlan(NONE, NONE, None)


def test_empty_root_on_an_empty_account_is_fine() -> None:
    assert run(NONE, NONE, NONE) == TombstonePlan(NONE, NONE, None)


def test_regression_takes_precedence_over_empty_root() -> None:
    lib = ids(3)
    assert run(lib, lib, NONE, prev_gen=5, cur_gen=1).anomaly == ANOMALY_GENERATION_REGRESSED


# ---- un-tombstone -----------------------------------------------------------------------------


def test_reappearing_tombstone_is_untombstoned() -> None:
    lib = ids(10)
    back = ids(2, start=100)
    still_gone = ids(1, start=200)
    result = run(lib, lib, lib | back, tombstoned=back | still_gone)
    assert result == TombstonePlan(NONE, back, None)


def test_untombstone_on_a_partial_listing() -> None:
    lib = ids(10)
    back = ids(1, start=100)
    result = run(lib, None, lib | back, tombstoned=back, cur_complete=False)
    assert result == TombstonePlan(NONE, back, None)


def test_tombstoned_documents_are_never_candidates() -> None:
    lib = ids(10)
    tomb = ids(2, start=100)
    # Inconsistent input (a document both in the library and tombstoned) is not tombstoned again.
    result = run(lib | tomb, lib, lib, tombstoned=tomb)
    assert result.to_tombstone == NONE


@pytest.mark.parametrize("bad", [-1, True, 1.5, "50"])
def test_max_per_poll_must_be_a_non_negative_int(bad: object) -> None:
    lib = ids(1)
    with pytest.raises(InvalidArgumentError):
        plan(listing(lib), listing(lib), lib, NONE, bad)  # type: ignore[arg-type]
