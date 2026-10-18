"""Fake of ``library/tombstones.py:plan`` (plan §3.3): the rules, written from the plan.

Pure, like the real planner. Sync tests inject it (``SyncOnce(..., planner=plan)``) while
A's module is unmerged; after the merge they can run against both.
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
) -> TombstonePlan:
    if previous is not None and current.generation < previous.generation:
        return TombstonePlan(_NONE, _NONE, "generation_regressed")
    if not current.uuids and ((previous is not None and previous.uuids) or library):
        return TombstonePlan(_NONE, _NONE, "empty_root")
    to_untombstone = tombstoned & current.uuids
    if previous is None or not (previous.complete and current.complete):
        return TombstonePlan(_NONE, to_untombstone, None)
    candidates = library - current.uuids - previous.uuids
    limit = max(5, len(library) / 10)
    if len(candidates) > limit or len(candidates) > max_per_poll:
        return TombstonePlan(_NONE, to_untombstone, "mass_delete")
    return TombstonePlan(frozenset(candidates), to_untombstone, None)
