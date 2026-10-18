"""The account's sync scope (§11.1 ``set_sync_scope``, M2): which documents the worker mirrors.

``all`` mirrors everything (the default). ``allow`` mirrors only documents that carry one of
the scope's tags, directly or through a folder above them; ``deny`` mirrors everything except
those. A document outside the scope keeps a ``documents`` row (``excluded_at`` set) built from
its ``.metadata``; nothing else of it is downloaded. Folders are always mirrored (they are
metadata only and hold the tree together); their tags apply to everything below them.

Tag names come from the cloud (a document's ``.content``) and from the user (the tool's
arguments). Both pass :func:`scriptorium.library.tags.normalise_tag`, the normalisation the
tag index uses (NFC, stripped, control characters refused, truncated to :data:`MAX_TAG_LEN`),
and are then compared exactly. They are display strings: they are stored in the account index
and returned by tools, and never logged or joined into a path.

The decision fails closed: a document is never found inside a scope on missing information.
:func:`scope_tags` reads every entry of a document's tag list (no cut at
:data:`MAX_TAGS_PER_DOCUMENT`, which the index applies for display) and answers None when the
list cannot be read whole; :func:`ancestor_tags` reports whether every folder above was known;
:func:`decide` answers None when the known tags do not settle the question. The caller then
leaves the document as it was for this poll.

The scope is stored as JSON in the account's ``settings`` table under ``sync_scope``:
``{"mode": "allow", "tags": ["a", "b"]}`` (tags sorted). Only ``set_sync_scope`` writes it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, cast, get_args

from scriptorium.library.tags import (
    MAX_ANCESTOR_DEPTH,
    MAX_TAG_LEN,
    MAX_TAGS_PER_DOCUMENT,
    normalise_tag,
)

SETTING_SYNC_SCOPE: Final = "sync_scope"
MAX_SCOPE_TAGS: Final = 100
MAX_TAG_SCAN: Final = 100_000
"""Entries of a ``tags`` list read for the scope; a longer list makes the tags unknown."""
MAX_SETTING_CHARS: Final = 64 * 1024
TRASH: Final = "trash"

ScopeMode = Literal["all", "allow", "deny"]
MODES: Final[tuple[str, ...]] = get_args(ScopeMode)


class ScopeError(ValueError):
    """A scope that cannot be used: malformed setting, unknown mode, bad tags."""


class SettingsReader(Protocol):
    """``Index.get_setting``: the JSON text stored under a key, or None."""

    def get_setting(self, key: str) -> str | None: ...


@dataclass(frozen=True)
class SyncScope:
    """One account's scope. ``tags`` are normalised names; empty exactly when ``mode`` is all."""

    mode: ScopeMode
    tags: frozenset[str]

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ScopeError("unknown scope mode")
        if self.mode == "all":
            if self.tags:
                raise ScopeError("scope 'all' takes no tags")
            return
        if not 1 <= len(self.tags) <= MAX_SCOPE_TAGS:
            raise ScopeError(f"scope '{self.mode}' needs 1 to {MAX_SCOPE_TAGS} tags")
        if any(normalise_tag(tag) != tag for tag in self.tags):
            raise ScopeError("scope tags must be normalised")


ALL: Final = SyncScope("all", frozenset())


def scope_tags(content: Mapping[str, Any] | None) -> frozenset[str] | None:
    """Every document-level tag name in a parsed ``.content``, or None if they are unknown.

    ``content`` is None when the file was listed but could not be read (fetch failed, too
    large, not a JSON object): unknown. A ``.content`` without a ``tags`` key has no tags.
    ``tags`` that is not a list, or longer than :data:`MAX_TAG_SCAN`, or with an entry that
    is not an object with a string ``name``, is unknown. A string name that normalises to
    nothing is ignored: no scope tag can equal it. Page tags (``pageTags``) do not decide
    the scope.
    """
    if content is None:
        return None
    raw = content.get("tags", [])
    if not isinstance(raw, list) or len(raw) > MAX_TAG_SCAN:
        return None
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return None
        name = normalise_tag(item["name"])
        if name is not None:
            names.add(name)
    return frozenset(names)


def make_scope(mode: str, tags: Iterable[object]) -> SyncScope:
    """A scope from loose input (the tool's arguments, the stored setting).

    Raises :class:`ScopeError` for an unknown mode, a tag that normalises to nothing, or the
    wrong number of tags for the mode. Duplicates after normalisation count once.
    """
    if mode not in MODES:
        raise ScopeError("unknown scope mode")
    names: set[str] = set()
    for tag in tags:
        name = normalise_tag(tag)
        if name is None:
            raise ScopeError("a tag is empty or has control characters")
        names.add(name)
    return SyncScope(cast(ScopeMode, mode), frozenset(names))


def load_scope(index: SettingsReader) -> SyncScope:
    """The account's scope (settings key ``sync_scope``); :data:`ALL` when none is set.

    Raises :class:`ScopeError` when the stored value is malformed: the caller must not
    guess a scope (falling back to ``all`` would download what the user excluded).
    """
    raw = index.get_setting(SETTING_SYNC_SCOPE)
    if raw is None:
        return ALL
    if len(raw) > MAX_SETTING_CHARS:
        raise ScopeError("stored scope is too large")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise ScopeError("stored scope is not JSON") from None
    if not isinstance(data, dict) or set(data) != {"mode", "tags"}:
        raise ScopeError("stored scope has the wrong shape")
    mode, tags = data["mode"], data["tags"]
    if not isinstance(mode, str) or not isinstance(tags, list):
        raise ScopeError("stored scope has the wrong shape")
    return make_scope(mode, tags)


def dump_scope(scope: SyncScope) -> str:
    """The canonical stored form of ``scope`` (tags sorted)."""
    return json.dumps(
        {"mode": scope.mode, "tags": sorted(scope.tags)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def scope_fingerprint(scope: SyncScope) -> str:
    """SHA-256 of the canonical form: what ``sync_state.scope_applied`` records."""
    return hashlib.sha256(dump_scope(scope).encode()).hexdigest()


@dataclass(frozen=True)
class Known:
    """What one poll knows of a folder above a document: its tags (None if unknown) and its
    parent (``parent_known`` False if its ``.metadata`` could not be read)."""

    tags: frozenset[str] | None
    parent: str | None
    parent_known: bool = True


NOT_A_FOLDER: Final = Known(frozenset(), None)
"""A parent the cloud does not list: no tags, and nothing above it."""


def ancestor_tags(
    parent: str | None, lookup: Callable[[str], Known | None]
) -> tuple[frozenset[str], bool]:
    """``(tags, complete)``: the union of the known tags of ``parent`` and every folder above
    it, and whether every one of them was known (``lookup`` answers None for a folder whose
    current version this poll has not read).

    Stops at the root (None), the trash, a folder already visited (the cloud's parents are
    untrusted and may form a cycle) or a folder whose parent is unknown. A chain longer than
    :data:`MAX_ANCESTOR_DEPTH` is incomplete.
    """
    found: set[str] = set()
    visited: set[str] = set()
    complete = True
    current = parent
    while current is not None and current != TRASH and current not in visited:
        if len(visited) >= MAX_ANCESTOR_DEPTH:
            return frozenset(found), False
        visited.add(current)
        node = lookup(current)
        if node is None:
            return frozenset(found), False
        if node.tags is None:
            complete = False
        else:
            found |= node.tags
        if not node.parent_known:
            return frozenset(found), False
        current = node.parent
    return frozenset(found), complete


def decide(
    scope: SyncScope,
    own: frozenset[str] | None,
    ancestors: frozenset[str],
    *,
    complete: bool = True,
) -> bool | None:
    """Whether a document is mirrored: True inside, False outside, None when the known tags
    do not settle it (its own tags unknown, or a folder above it unknown, and no known tag
    matched). A known match settles it either way: ``allow`` lets it in, ``deny`` keeps it
    out."""
    if scope.mode == "all":
        return True
    hit = not scope.tags.isdisjoint(ancestors) or (
        own is not None and not scope.tags.isdisjoint(own)
    )
    if hit:
        return scope.mode == "allow"
    if own is None or not complete:
        return None
    return scope.mode == "deny"


__all__ = [
    "ALL",
    "MAX_ANCESTOR_DEPTH",
    "MAX_SCOPE_TAGS",
    "MAX_TAGS_PER_DOCUMENT",
    "MAX_TAG_LEN",
    "MAX_TAG_SCAN",
    "MODES",
    "NOT_A_FOLDER",
    "SETTING_SYNC_SCOPE",
    "Known",
    "ScopeError",
    "ScopeMode",
    "SettingsReader",
    "SyncScope",
    "ancestor_tags",
    "decide",
    "dump_scope",
    "load_scope",
    "make_scope",
    "normalise_tag",
    "scope_fingerprint",
    "scope_tags",
]
