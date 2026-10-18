"""Filesystem names for archived documents: the allowlist and the xochitl layout (§6, §11.0).

Every file name that comes from the cloud is attacker-controlled. Nothing derived from one is
joined onto a path until it has passed :func:`is_safe_name`, and every joined path is then
resolved (following symlinks) and asserted to stay inside its root (:func:`resolve_inside`).
Filesystem names are always UUIDs; cloud display names never reach the filesystem.

The allowlist (:data:`SAFE_NAME_PATTERN`) accepts exactly these names, where ``<uuid>`` and
``<page>`` are ``[0-9a-f-]{36}`` (§11.0):

- ``<uuid>``: a document's page directory, and the name of its tombstone directory;
- ``<uuid>.<ext>`` for ``ext`` in metadata, content, pagedata, pdf, epub;
- ``<uuid>/<page>.rm``: a page's ink;
- ``<uuid>/<page>-metadata.json``: a page's layer names;
- ``<uuid>.highlights/<page>.json``: text selected with the highlighter on a PDF or EPUB page;
- ``<uuid>.thumbnails/<page>.png`` and ``<uuid>.thumbnails/<page>.jpg``: page thumbnails;
- ``<uuid>.textconversion/<page>.json``: the tablet's own handwriting conversion.

Nothing else: every two-level name is one of these directories, a page UUID and one fixed
suffix. Matching is always against the whole string (``re.fullmatch``): a ``$`` anchor would
let a trailing newline through.

A document owns the top-level entries :func:`doc_entries` lists: its files and the four
directories ``<uuid>``, ``<uuid>.highlights``, ``<uuid>.thumbnails`` and
``<uuid>.textconversion``. The archive moves exactly those, each as a whole, and nothing else.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

from scriptorium.errors import PathEscapeError, RefusedError

__all__ = [
    "DOC_EXTENSIONS",
    "DOC_SUBDIRS",
    "PAGE_FILES",
    "SAFE_NAME_PATTERN",
    "UUID_PATTERN",
    "PageFile",
    "belongs_to",
    "doc_dirs",
    "doc_entries",
    "doc_file_name",
    "doc_uuid",
    "is_doc_entry",
    "is_safe_name",
    "is_sha256",
    "is_uuid",
    "page_name",
    "resolve_inside",
    "safe_join",
]

#: A document or page identifier as it may appear on disk (§11.0).
UUID_PATTERN: Final = "[0-9a-f-]{36}"
#: The per-document files of the xochitl layout, besides the page directory.
DOC_EXTENSIONS: Final = ("metadata", "content", "pagedata", "pdf", "epub")
#: Per-page directories besides ``<uuid>/``, as ``<uuid>.<subdir>/``.
DOC_SUBDIRS: Final = ("highlights", "thumbnails", "textconversion")

PageFile = Literal[
    "rm", "page_metadata", "highlights", "thumbnail_png", "thumbnail_jpg", "textconversion"
]
#: Every per-page file: kind -> (the directory suffix after ``<uuid>``, the suffix after
#: ``<page>``), so the name is ``<uuid><dir>/<page><suffix>``.
PAGE_FILES: Final[Mapping[PageFile, tuple[str, str]]] = MappingProxyType(
    {
        "rm": ("", ".rm"),
        "page_metadata": ("", "-metadata.json"),
        "highlights": (".highlights", ".json"),
        "thumbnail_png": (".thumbnails", ".png"),
        "thumbnail_jpg": (".thumbnails", ".jpg"),
        "textconversion": (".textconversion", ".json"),
    }
)

#: The §11.0 allowlist, matched with ``re.fullmatch`` only. It is written out rather than
#: generated so the copy in Architecture §11.0 can be compared character for character; a
#: test checks it against :data:`DOC_EXTENSIONS` and :data:`PAGE_FILES`.
SAFE_NAME_PATTERN: Final = (
    r"(?P<doc>[0-9a-f-]{36})"
    r"(?:\.(?:metadata|content|pagedata|pdf|epub)"
    r"|/[0-9a-f-]{36}(?:\.rm|-metadata\.json)"
    r"|\.highlights/[0-9a-f-]{36}\.json"
    r"|\.thumbnails/[0-9a-f-]{36}\.(?:png|jpg)"
    r"|\.textconversion/[0-9a-f-]{36}\.json"
    r")?"
)

_UUID: Final = re.compile(UUID_PATTERN)
_SHA256: Final = re.compile("[0-9a-f]{64}")
_SAFE_NAME: Final = re.compile(SAFE_NAME_PATTERN)


def is_uuid(value: object) -> bool:
    """True for a string that is a document or page identifier as stored on disk."""
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def is_sha256(value: object) -> bool:
    """True for a lowercase hex SHA-256 digest, the name of a blob."""
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def is_safe_name(name: object) -> bool:
    """True when ``name`` is one of the xochitl-relative names the archive will store."""
    return isinstance(name, str) and _SAFE_NAME.fullmatch(name) is not None


def doc_uuid(name: str) -> str:
    """The document a safe name belongs to; raises :class:`RefusedError` for any other name."""
    match = _SAFE_NAME.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        raise RefusedError("file name is not on the allowlist")
    return match.group("doc")


def belongs_to(name: str, uuid: str) -> bool:
    """True when ``name`` is a safe name of document ``uuid``."""
    return is_uuid(uuid) and is_safe_name(name) and doc_uuid(name) == uuid


def doc_file_name(uuid: str, ext: str) -> str:
    """``<uuid>.<ext>``, validated."""
    name = f"{uuid}.{ext}"
    if ext not in DOC_EXTENSIONS or not is_safe_name(name):
        raise RefusedError("not a document file name")
    return name


def page_name(uuid: str, page_uuid: str, kind: PageFile = "rm") -> str:
    """The name of one per-page file (:data:`PAGE_FILES`), validated.

    The default is the page's ink, ``<uuid>/<page-uuid>.rm``.
    """
    shape = PAGE_FILES.get(kind) if isinstance(kind, str) else None
    if shape is None:
        raise RefusedError("not a page file kind")
    directory, suffix = shape
    name = f"{uuid}{directory}/{page_uuid}{suffix}"
    if not is_safe_name(name):
        raise RefusedError("not a page file name")
    return name


def doc_dirs(uuid: str) -> tuple[str, ...]:
    """The directories document ``uuid`` may own: ``<uuid>``, then ``<uuid>.<subdir>``."""
    if not is_uuid(uuid):
        raise RefusedError("document identifier is not on the allowlist")
    return (uuid, *(f"{uuid}.{sub}" for sub in DOC_SUBDIRS))


def doc_entries(uuid: str) -> tuple[str, ...]:
    """Every top-level directory entry document ``uuid`` may own in ``library/``.

    The page directory comes first, then the files in :data:`DOC_EXTENSIONS` order, then the
    other directories in :data:`DOC_SUBDIRS` order. These are the only entries the archive
    ever moves for a document, each as a whole; anything else in the directory (which
    Scriptorium never writes) is left where it is.
    """
    page_dir, *subdirs = doc_dirs(uuid)
    return (page_dir, *(f"{uuid}.{ext}" for ext in DOC_EXTENSIONS), *subdirs)


def is_doc_entry(name: object) -> bool:
    """True for a top-level entry of some document: a name :func:`doc_entries` can return."""
    if not isinstance(name, str):
        return False
    uuid, dot, rest = name.partition(".")
    return is_uuid(uuid) and (not dot or rest in DOC_EXTENSIONS or rest in DOC_SUBDIRS)


def resolve_inside(root: Path | str, path: Path | str) -> Path:
    """``path`` resolved like :func:`os.path.realpath`, asserted to be ``root`` or below it.

    Symlinks are followed in both, so a link inside the root that points outside it is an
    escape. Relative paths, NUL bytes and unresolvable paths are refused. Raises
    :class:`PathEscapeError`; the message never contains the path (it may be attacker-chosen).
    """
    text, root_text = os.fspath(path), os.fspath(root)
    if "\x00" in text or "\x00" in root_text:
        raise PathEscapeError("path contains a NUL byte")
    if not os.path.isabs(text) or not os.path.isabs(root_text):
        raise PathEscapeError("path is not absolute")
    try:
        real_root = os.path.realpath(root_text)
        real = os.path.realpath(text)
    except (OSError, ValueError):
        raise PathEscapeError("path cannot be resolved") from None
    if real != real_root and not real.startswith(real_root.rstrip(os.sep) + os.sep):
        raise PathEscapeError("path resolves outside its root")
    return Path(real)


def safe_join(root: Path, *parts: str) -> Path:
    """``root`` joined with allowlisted ``parts``, resolved and asserted inside ``root``.

    Each part must pass :func:`is_safe_name` or :func:`is_doc_entry` on its own. A per-page
    file is passed as one part, ``"<uuid>.thumbnails/<page>.png"``, never as a directory and a
    file name separately; a document directory such as ``"<uuid>.highlights"`` may be joined
    to list it. The result follows symlinks, so use it to read; code that moves a directory
    entry must not follow the entry itself.
    """
    if not parts:
        raise RefusedError("nothing to join")
    for part in parts:
        if not (is_safe_name(part) or is_doc_entry(part)):
            raise RefusedError("file name is not on the allowlist")
    return resolve_inside(root, Path(root).joinpath(*parts))
