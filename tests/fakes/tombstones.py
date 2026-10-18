"""Fake of ``library/tombstones.py:plan`` (plan §3.3), following A's rules as merged.

Pure, like the real planner. The cloud tests now use the real one (``SyncOnce``'s
default); this stays importable for other workstreams. The rules, in
A's order: generation regressed; empty root (``had_entries`` from the previous listing,
or from the library when there is none); un-tombstone what is present; candidates only
between two complete listings, excluding what is already tombstoned; the breaker at
``max(5, 10 %)`` or ``max_per_poll``, skipped once by ``allow_mass_delete``.
"""

from __future__ import annotations

from scriptorium.cloud.ports import Listing, TombstonePlan

_NONE: frozenset[str] = frozenset()


def plan(
    previous: Listing | None,
    current: Listing,
    library: frozenset[str],
    tombstoned: frozenset[str],
    max_per_poll: int,
    *,
    allow_mass_delete: bool = False,
) -> TombstonePlan:
    if previous is not None and current.generation < previous.generation:
        return TombstonePlan(_NONE, _NONE, "generation_regressed")
    had_entries = bool(previous.uuids) if previous is not None else bool(library)
    if not current.uuids and had_entries:
        return TombstonePlan(_NONE, _NONE, "empty_root")
    to_untombstone = frozenset(tombstoned & current.uuids)
    if previous is None or not (previous.complete and current.complete):
        return TombstonePlan(_NONE, to_untombstone, None)
    candidates = frozenset(library - tombstoned - previous.uuids - current.uuids)
    over_share = len(candidates) > 5 and len(candidates) * 10 > len(library)
    if not allow_mass_delete and (over_share or len(candidates) > max_per_poll):
        return TombstonePlan(_NONE, to_untombstone, "mass_delete")
    return TombstonePlan(candidates, to_untombstone, None)
