"""The account's sync scope (§11.1 ``set_sync_scope``, M2): which documents the worker mirrors.

``all`` mirrors everything (the default). ``allow`` mirrors only documents that carry one of
the scope's tags, directly or through a folder above them; ``deny`` mirrors everything except
those. A document outside the scope keeps a ``documents`` row (``excluded_at`` set) built from
its ``.metadata``; nothing else of it is downloaded. Folders are always mirrored (they are
metadata only and hold the tree together); their tags apply to everything below them.

Tag names come from the cloud (a document's ``.content``) and from the user (the tool's
arguments). Both pass :func:`normalise_tag` (NFC, stripped, control characters refused,
truncated to :data:`MAX_TAG_LEN`) and are then compared exactly. They are display strings:
they are stored in the account index and returned by tools, and never logged or joined into
a path.

The scope is stored as JSON in the account's ``settings`` table under ``sync_scope``:
``{"mode": "allow", "tags": ["a", "b"]}`` (tags sorted). Only ``set_sync_scope`` writes it.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, cast, get_args

SETTING_SYNC_SCOPE: Final = "sync_scope"
MAX_TAG_LEN: Final = 64
MAX_TAGS_PER_DOCUMENT: Final = 100
MAX_SCOPE_TAGS: Final = 100
MAX_ANCESTOR_DEPTH: Final = 128
"""Folders walked above a document; deeper (or a cycle in the cloud's parents) stops there."""
MAX_SETTING_CHARS: Final = 64 * 1024
TRASH: Final = "trash"

ScopeMode = Literal["all", "allow", "deny"]
MODES: Final[tuple[str, ...]] = get_args(ScopeMode)

_BIDI_CONTROLS: Final = frozenset("‎‏‪‫‬‭‮⁦⁧⁨⁩")


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


def normalise_tag(raw: object) -> str | None:
    """A tag name as it is stored and compared, or None if it is not one.

    NFC, then stripped; a name with a control character (C0/C1, a lone surrogate, a
    bidirectional override or isolate) or nothing left is refused; a longer one is cut to
    :data:`MAX_TAG_LEN` characters (and stripped again).
    """
    if not isinstance(raw, str):
        return None
    name = unicodedata.normalize("NFC", raw).strip()
    if not name or any(
        unicodedata.category(ch) in ("Cc", "Cs") or ch in _BIDI_CONTROLS for ch in name
    ):
        return None
    name = name[:MAX_TAG_LEN].rstrip()
    return name or None


def document_tags(content: Mapping[str, Any]) -> frozenset[str]:
    """The document-level tag names in a parsed ``.content`` (``"tags": [{"name": …}]``).

    Page tags (``pageTags``) do not decide the scope. Unusable entries are skipped; beyond
    :data:`MAX_TAGS_PER_DOCUMENT` names, the first ones in sort order are kept.
    """
    raw = content.get("tags")
    if not isinstance(raw, list):
        return frozenset()
    names: set[str] = set()
    for item in raw[: MAX_TAGS_PER_DOCUMENT * 10]:
        name = normalise_tag(item.get("name")) if isinstance(item, dict) else None
        if name is not None:
            names.add(name)
    return frozenset(sorted(names)[:MAX_TAGS_PER_DOCUMENT])


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


def in_scope(scope: SyncScope, doc_tags: frozenset[str], ancestor_tags: frozenset[str]) -> bool:
    """Whether a document with these tags, below folders with ``ancestor_tags``, is mirrored."""
    if scope.mode == "all":
        return True
    hit = not scope.tags.isdisjoint(doc_tags) or not scope.tags.isdisjoint(ancestor_tags)
    return hit if scope.mode == "allow" else not hit


def ancestor_tags(
    parent: str | None,
    parent_of: Callable[[str], str | None],
    tags_of: Callable[[str], frozenset[str]],
) -> frozenset[str]:
    """The union of the tags of ``parent`` and every folder above it.

    Stops at the root (None), the trash, a folder already visited (the cloud's parents are
    untrusted and may form a cycle) or after :data:`MAX_ANCESTOR_DEPTH` folders.
    """
    found: set[str] = set()
    visited: set[str] = set()
    current = parent
    while current is not None and current != TRASH and current not in visited:
        if len(visited) >= MAX_ANCESTOR_DEPTH:
            break
        visited.add(current)
        found |= tags_of(current)
        current = parent_of(current)
    return frozenset(found)


__all__ = [
    "ALL",
    "MAX_SCOPE_TAGS",
    "MAX_TAGS_PER_DOCUMENT",
    "MAX_TAG_LEN",
    "MODES",
    "SETTING_SYNC_SCOPE",
    "ScopeError",
    "ScopeMode",
    "SettingsReader",
    "SyncScope",
    "ancestor_tags",
    "document_tags",
    "dump_scope",
    "in_scope",
    "load_scope",
    "make_scope",
    "normalise_tag",
    "scope_fingerprint",
]
