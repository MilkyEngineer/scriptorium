"""Which documents to tombstone or bring back after a poll: a pure planner (§7 steps 1 and 4).

No I/O. The sync loop gathers the facts, this module decides, and the archive moves files.
The rules, in the order they are applied:

1. **Generation regressed**: the current root generation is lower than the previous
   listing's. The listing cannot be trusted: ``anomaly="generation_regressed"``, empty plan.
2. **Empty root**: the current listing has no entries although the previous one had some (or,
   with no previous listing, the library is not empty). ``anomaly="empty_root"``, empty plan.
   An empty *previous* listing is not evidence of entries: after an administrator clears the
   anomaly, a root that stays empty is believed and goes on to rules 4 and 5 (so a small
   library is tombstoned, which only moves files, and a larger one trips the breaker).
3. **Un-tombstone**: a tombstoned UUID present in the current listing comes back. Presence is
   positive evidence, so this holds for a partial listing too.
4. **Candidates**: only when the previous and the current listing are both complete, a
   library UUID absent from both is a deletion candidate. The caller must pass the listing of
   the immediately preceding poll as ``previous`` (complete or not), so "two consecutive
   complete listings" means exactly that.
5. **Circuit breaker**: more candidates than ``max(5, 10 % of the library)``, or more than
   ``max_per_poll``: ``anomaly="mass_delete"`` and nothing is tombstoned (un-tombstones from
   rule 3 are still reported; bringing a document back deletes nothing). The comparison is
   exact (no rounding): with 55 documents, 5 candidates pass and 6 trip it.

An anomaly means the account goes read-only until an administrator clears it (§7 step 7);
the plan returned with one never tombstones anything, and the caller applies no plan while
the account is read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from scriptorium.errors import InvalidArgumentError

__all__ = [
    "ANOMALY_EMPTY_ROOT",
    "ANOMALY_GENERATION_REGRESSED",
    "ANOMALY_MASS_DELETE",
    "BREAKER_FLOOR",
    "Listing",
    "TombstonePlan",
    "plan",
]

ANOMALY_MASS_DELETE: Final = "mass_delete"
ANOMALY_GENERATION_REGRESSED: Final = "generation_regressed"
ANOMALY_EMPTY_ROOT: Final = "empty_root"
#: Candidates up to this many never trip the breaker on the percentage rule alone.
BREAKER_FLOOR: Final = 5
#: The percentage rule: candidates may not exceed this fraction (numerator/denominator).
_BREAKER_FRACTION: Final = (1, 10)


@dataclass(frozen=True)
class Listing:
    """What one poll saw: the UUIDs in the root, whether every index fetch succeeded, and the
    root generation."""

    uuids: frozenset[str]
    complete: bool
    generation: int


@dataclass(frozen=True)
class TombstonePlan:
    to_tombstone: frozenset[str]
    to_untombstone: frozenset[str]
    anomaly: str | None


_EMPTY: Final = frozenset[str]()


def plan(
    previous: Listing | None,
    current: Listing,
    library: frozenset[str],
    tombstoned: frozenset[str],
    max_per_poll: int,
) -> TombstonePlan:
    """Decide tombstones for this poll (rules in the module docstring).

    ``library`` is the set of documents currently in the library (not tombstoned);
    ``tombstoned`` the set currently in ``tombstones/``; ``max_per_poll`` is
    ``TOMBSTONE_MAX_PER_POLL``.
    """
    if isinstance(max_per_poll, bool) or not isinstance(max_per_poll, int) or max_per_poll < 0:
        raise InvalidArgumentError("max_per_poll must be a non-negative integer")

    if previous is not None and current.generation < previous.generation:
        return TombstonePlan(_EMPTY, _EMPTY, ANOMALY_GENERATION_REGRESSED)
    had_entries = bool(previous.uuids) if previous is not None else bool(library)
    if not current.uuids and had_entries:
        return TombstonePlan(_EMPTY, _EMPTY, ANOMALY_EMPTY_ROOT)

    to_untombstone = frozenset(tombstoned & current.uuids)

    if previous is None or not (previous.complete and current.complete):
        return TombstonePlan(_EMPTY, to_untombstone, None)

    candidates = frozenset(library - tombstoned - previous.uuids - current.uuids)
    if _breaker_trips(len(candidates), len(library), max_per_poll):
        return TombstonePlan(_EMPTY, to_untombstone, ANOMALY_MASS_DELETE)
    return TombstonePlan(candidates, to_untombstone, None)


def _breaker_trips(candidates: int, library_size: int, max_per_poll: int) -> bool:
    """``candidates > max(5, 10 % of library)`` or ``candidates > max_per_poll``, exactly."""
    num, den = _BREAKER_FRACTION
    over_share = candidates > BREAKER_FLOOR and candidates * den > library_size * num
    return over_share or candidates > max_per_poll
