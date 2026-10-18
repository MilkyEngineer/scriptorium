"""Tags from a document's ``.content`` (Architecture §6.1 ``tags``, §7 step 3, §11.2).

The tablet stores a document's tags in its ``.content`` file: ``"tags": [{"name": …}]`` for
the document (or folder) itself and ``"pageTags": [{"name": …, "pageId": …}]`` for single
pages. Both are cloud-supplied display strings, so they are bounded and normalised here
before anything else sees them:

- a name must be a string; it is NFC-normalised and stripped of surrounding whitespace;
- a name holding a control character (Unicode ``Cc``), a surrogate, a line or paragraph
  separator, or a bidirectional embedding, override or isolate is rejected, as is a name
  that is empty after stripping;
- a name longer than :data:`MAX_TAG_LEN` characters is truncated to it (and stripped again);
- a page tag whose ``pageId`` is not a page of the document (a UUID listed in ``cPages`` and
  not deleted, or in the format 1 ``pages`` list) is dropped;
- at most :data:`MAX_TAGS_PER_DOCUMENT` tags are kept per document, counting document and page
  tags together: document tags first, sorted by name, then page tags sorted by page id and
  name.

Everything rejected, dropped or cut by the bound is counted in :attr:`TagSet.skipped` (the
worker logs that count, never a name). Tag names are display data only: they are returned to
their owner by the tools and stored in the index, never joined into a path or logged.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from scriptorium.library.layout import is_uuid

__all__ = [
    "MAX_TAGS_PER_DOCUMENT",
    "MAX_TAG_LEN",
    "TagSet",
    "normalise_tag",
    "parse_tags",
]

MAX_TAGS_PER_DOCUMENT: Final = 100
MAX_TAG_LEN: Final = 64
_MAX_SCAN: Final = 100_000
"""Entries of ``tags``/``pageTags``/``cPages`` looked at; the JSON itself is size-capped."""
_REJECTED_CATEGORIES: Final = frozenset({"Cc", "Cs", "Zl", "Zp"})
_BIDI_CONTROLS: Final = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


@dataclass(frozen=True)
class TagSet:
    """A document's tags: its own (``document``) and its pages' (page UUID → names).

    ``skipped`` counts entries :func:`parse_tags` rejected, dropped or cut by the bound; it is
    not part of the value (two sets with the same tags are equal).
    """

    document: frozenset[str] = frozenset()
    pages: Mapping[str, frozenset[str]] = field(default_factory=lambda: MappingProxyType({}))
    skipped: int = field(default=0, compare=False)

    def names(self) -> frozenset[str]:
        """Every name, on the document or on any page."""
        out = set(self.document)
        for names in self.pages.values():
            out |= names
        return frozenset(out)

    def for_page(self, page_id: str) -> frozenset[str]:
        """The tags that apply to one page: its own and the document's."""
        return self.document | self.pages.get(page_id, frozenset())

    def __len__(self) -> int:
        return len(self.document) + sum(len(names) for names in self.pages.values())


def normalise_tag(value: object) -> str | None:
    """``value`` as a stored tag name (see the module docstring), or ``None`` if rejected."""
    if not isinstance(value, str):
        return None
    name = unicodedata.normalize("NFC", value).strip()
    if any(unicodedata.category(ch) in _REJECTED_CATEGORIES or ch in _BIDI_CONTROLS for ch in name):
        return None
    if len(name) > MAX_TAG_LEN:
        name = name[:MAX_TAG_LEN].rstrip()
    return name or None


def _entries(value: object) -> Iterable[Mapping[str, Any]]:
    if not isinstance(value, list):
        return
    for entry in value[:_MAX_SCAN]:
        if isinstance(entry, dict):
            yield entry


def _page_ids(content: Mapping[str, Any]) -> frozenset[str]:
    """The document's page UUIDs, as ``library.pages.page_list`` reads them (unordered)."""
    c_pages = content.get("cPages")
    pages = c_pages.get("pages") if isinstance(c_pages, dict) else None
    if isinstance(pages, list):
        ids: set[str] = set()
        for page in _entries(pages):
            deleted = page.get("deleted")
            if isinstance(deleted, dict) and deleted.get("value") not in (None, 0, False):
                continue
            page_id = page.get("id")
            if isinstance(page_id, str) and is_uuid(page_id):
                ids.add(page_id)
        return frozenset(ids)
    old = content.get("pages")
    if isinstance(old, list):
        return frozenset(p for p in old[:_MAX_SCAN] if is_uuid(p))
    return frozenset()


def parse_tags(content: Mapping[str, Any]) -> TagSet:
    """The bounded, normalised tags of a parsed ``.content`` (see the module docstring)."""
    skipped = 0
    document: set[str] = set()
    raw_tags = content.get("tags")
    for entry in raw_tags[:_MAX_SCAN] if isinstance(raw_tags, list) else ():
        name = normalise_tag(entry.get("name")) if isinstance(entry, dict) else None
        if name is None:
            skipped += 1
        else:
            document.add(name)
    page_ids = _page_ids(content)
    page_tags: set[tuple[str, str]] = set()
    raw_page_tags = content.get("pageTags")
    for entry in raw_page_tags[:_MAX_SCAN] if isinstance(raw_page_tags, list) else ():
        if not isinstance(entry, dict):
            skipped += 1
            continue
        name = normalise_tag(entry.get("name"))
        page_id = entry.get("pageId")
        if name is None or not isinstance(page_id, str) or page_id not in page_ids:
            skipped += 1
            continue
        page_tags.add((page_id, name))
    for listed in (raw_tags, raw_page_tags):
        if isinstance(listed, list) and len(listed) > _MAX_SCAN:
            skipped += len(listed) - _MAX_SCAN
    kept_doc = sorted(document)[:MAX_TAGS_PER_DOCUMENT]
    room = MAX_TAGS_PER_DOCUMENT - len(kept_doc)
    kept_pages = sorted(page_tags)[:room]
    skipped += len(document) - len(kept_doc) + len(page_tags) - len(kept_pages)
    pages: dict[str, set[str]] = {}
    for page_id, name in kept_pages:
        pages.setdefault(page_id, set()).add(name)
    return TagSet(
        document=frozenset(kept_doc),
        pages=MappingProxyType({p: frozenset(n) for p, n in pages.items()}),
        skipped=skipped,
    )
