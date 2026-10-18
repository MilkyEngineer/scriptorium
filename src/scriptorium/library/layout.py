"""Filesystem names for archived documents: the allowlist and the xochitl layout (§6, §11.0).

Every file name that comes from the cloud is attacker-controlled. Nothing derived from one is
joined onto a path until it has passed :func:`is_safe_name`, and every joined path is then
resolved (following symlinks) and asserted to stay inside its root (:func:`resolve_inside`).
Filesystem names are always UUIDs; cloud display names never reach the filesystem.

The allowlist accepts exactly these names, where ``<uuid>`` is ``[0-9a-f-]{36}`` (§11.0):

- ``<uuid>``: a document's page directory, and the name of its tombstone directory;
- ``<uuid>.<ext>`` for ``ext`` in metadata, content, pagedata, pdf, epub;
- ``<uuid>/<page-uuid>.rm``: a page, the only two-level name.

Matching is always against the whole string (``re.fullmatch``): a ``$`` anchor would let a
trailing newline through.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

from scriptorium.errors import PathEscapeError, RefusedError

__all__ = [
    "DOC_EXTENSIONS",
    "UUID_PATTERN",
    "belongs_to",
    "doc_entries",
    "doc_file_name",
    "doc_uuid",
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

_UUID: Final = re.compile(UUID_PATTERN)
_SHA256: Final = re.compile("[0-9a-f]{64}")
_SAFE_NAME: Final = re.compile(
    rf"(?P<doc>{UUID_PATTERN})"
    rf"(?:\.(?:{'|'.join(DOC_EXTENSIONS)})|/(?P<page>{UUID_PATTERN})\.rm)?"
)


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


def page_name(uuid: str, page_uuid: str) -> str:
    """``<uuid>/<page-uuid>.rm``, validated."""
    name = f"{uuid}/{page_uuid}.rm"
    if not is_safe_name(name):
        raise RefusedError("not a page file name")
    return name


def doc_entries(uuid: str) -> tuple[str, ...]:
    """Every top-level directory entry document ``uuid`` may own in ``library/``.

    The page directory comes first, then the files in :data:`DOC_EXTENSIONS` order. These are
    the only entries the archive ever moves for a document; anything else in the directory
    (which Scriptorium never writes) is left where it is.
    """
    if not is_uuid(uuid):
        raise RefusedError("document identifier is not on the allowlist")
    return (uuid, *(f"{uuid}.{ext}" for ext in DOC_EXTENSIONS))


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

    Each part must pass :func:`is_safe_name` on its own (a page is passed as the single part
    ``"<uuid>/<page-uuid>.rm"``). The result follows symlinks, so use it to read; code that
    moves a directory entry must not follow the entry itself.
    """
    if not parts:
        raise RefusedError("nothing to join")
    for part in parts:
        if not is_safe_name(part):
            raise RefusedError("file name is not on the allowlist")
    return resolve_inside(root, Path(root).joinpath(*parts))
