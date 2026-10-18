"""A document's pages: the page list, each page's content hash, and its PNG (§6, §6.1, §8.3).

The worker refreshes the index's ``pages`` from here after every commit, and the tools use the
same functions to find a page's inputs in any recorded version (``read_page(version=…)``).

**Inputs come from ``blobs/``**, through the version's document index
(``history.index_sha256``, §6), never from ``library/``. Blobs are content-addressed and never
move, so a reader cannot catch a document halfway through a swap, and an old version reads
exactly like the current one. A version whose index blob was not recorded has no page list
here (:class:`~scriptorium.errors.NotFoundError`).

**Page order** is the ``.content`` file's: ``cPages.pages`` (format 2) sorted by the
``idx.value`` fractional-index string, ties broken by position, pages marked ``deleted``
skipped; or the format 1 ``pages`` list with ``redirectionPageMap``. Page ids pass the
filesystem UUID allowlist or are skipped; a repeated id keeps its first position.

**Content hash** (the OCR cache key, §8.2): for a notebook page with ink it is the ``.rm``
blob's SHA-256, which a person can check against the cloud. Every other page (a PDF page,
annotated or not, or a page with no ink) hashes what it is drawn from: SHA-256 over
``scriptorium-page-v1``, the ``.rm`` hash, the PDF hash and the PDF page number. Equal hash,
equal image; so a duplicated document shares its pages' renders and transcripts.

**PNG layout**: ``pages/<page-uuid>/<content-hash>.png`` at the archive scale, and
``pages/<page-uuid>/<content-hash>.<N>px.png`` for a ``page_image`` rendered larger than that,
whose longest side is ``N``. The path names the content, so a file is written once and
never changes; a page edited on the tablet gets a new file and the old one stays for
``read_page(version=…)``. (Architecture §6 sketches ``pages/<uuid>/<page-uuid>.png``; that
name cannot hold two versions of a page.) PNGs are a cache: they are written with a
temporary file and a rename, and this module never deletes one.

Everything read here is untrusted cloud data: JSON is size-capped, page ids and hashes pass
their allowlists before they reach a path, and every path is resolved inside its root.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from PIL import Image

from scriptorium.cloud.protocol import MAX_DOCUMENT_FILES, MAX_INDEX_BYTES, parse_document_index
from scriptorium.errors import InvalidArgumentError, NotFoundError, ScriptoriumError
from scriptorium.library.index import HistoryRow, Index
from scriptorium.library.layout import (
    doc_file_name,
    is_sha256,
    is_uuid,
    page_name,
    resolve_inside,
)
from scriptorium.library.render import PX_PER_UNIT
from scriptorium.library.tags import TagSet, parse_tags
from scriptorium.users.store import AccountPaths

__all__ = [
    "BLANK_PAGE_UNITS",
    "MAX_CONTENT_BYTES",
    "PageEntry",
    "PageSource",
    "Version",
    "blank_page_png",
    "blob_path",
    "content_hash",
    "content_tags",
    "current_version",
    "load_version",
    "page_list",
    "page_sources",
    "png_relpath",
    "png_size",
    "read_blob",
    "resolve_png",
    "version_at",
    "write_png",
]

MAX_CONTENT_BYTES: Final = 8 * 1024 * 1024
"""A ``.content`` file larger than this is not parsed (the sync's own JSON cap)."""
MAX_PAGES: Final = 100_000
BLANK_PAGE_UNITS: Final = (820, 1458)
"""The Paper Pro Move's paper in `.rm` units (§8.3): a page with no ink renders this size."""
_HASH_DOMAIN: Final = b"scriptorium-page-v1\n"


# ---- versions --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Version:
    """One recorded version of a document: its generation and its files (name → blob hash)."""

    document: str
    generation: int
    hash_root: str
    files: Mapping[str, str]


def blob_path(paths: AccountPaths, sha256: str) -> Path:
    """``blobs/<sha256>`` resolved inside ``blobs/`` (not checked to exist)."""
    if not is_sha256(sha256):
        raise NotFoundError("blob is not in the archive")
    return resolve_inside(paths.blobs, paths.blobs / sha256)


def read_blob(paths: AccountPaths, sha256: str, *, limit: int) -> bytes:
    """``blobs/<sha256>``, at most ``limit`` bytes; ``invalid_argument`` when larger."""
    path = blob_path(paths, sha256)
    try:
        with path.open("rb") as fh:
            data = fh.read(limit + 1)
    except FileNotFoundError:
        raise NotFoundError("blob is not in the archive") from None
    if len(data) > limit:
        raise InvalidArgumentError("a stored file is larger than this reader accepts")
    return data


def load_version(paths: AccountPaths, row: HistoryRow) -> Version:
    """The file list of the version ``row`` records, read from its index blob."""
    if row.index_sha256 is None:
        raise NotFoundError("this version's file list was not recorded")
    body = read_blob(paths, row.index_sha256, limit=MAX_INDEX_BYTES)
    index = parse_document_index(
        body, uuid=row.uuid, hash=row.hash_root, max_files=MAX_DOCUMENT_FILES
    )
    return Version(
        document=row.uuid,
        generation=row.generation,
        hash_root=row.hash_root,
        files={name: ref.sha256 for name, ref in index.files.items()},
    )


def current_version(paths: AccountPaths, index: Index, uuid: str) -> Version | None:
    """The version ``documents`` points at (its ``hash_root``), or None for no document.

    Prefers the history row of the document's own generation, then the newest row with the
    same ``hash_root``.
    """
    doc = index.document(uuid)
    if doc is None or doc.hash_root is None:
        return None
    rows = [h for h in index.history(uuid) if h.hash_root == doc.hash_root]
    if not rows:
        return None
    row = next((h for h in rows if h.generation == doc.generation), rows[0])
    return load_version(paths, row)


def version_at(paths: AccountPaths, index: Index, uuid: str, generation: int) -> Version:
    """The version recorded at ``generation``; ``not_found`` if there is none."""
    for row in index.history(uuid):
        if row.generation == generation:
            return load_version(paths, row)
    raise NotFoundError("no version of this document has that generation")


# ---- the page list ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageEntry:
    """A page as ``.content`` lists it: its id and, for a PDF page, the PDF page (0-based)."""

    page_id: str
    pdf_page: int | None


def _int_or_none(value: object) -> int | None:
    return value if type(value) is int and 0 <= value < MAX_PAGES else None


def _redir(value: object) -> int | None:
    if isinstance(value, dict):
        value = value.get("value")
    return _int_or_none(value)


def page_list(content: Mapping[str, Any], *, is_pdf: bool) -> list[PageEntry]:
    """The document's pages in order (see the module docstring)."""
    entries: list[PageEntry] = []
    c_pages = content.get("cPages")
    pages = c_pages.get("pages") if isinstance(c_pages, dict) else None
    if isinstance(pages, list):
        keyed: list[tuple[str, int, PageEntry]] = []
        for position, page in enumerate(pages[:MAX_PAGES]):
            if not isinstance(page, dict) or not is_uuid(page.get("id")):
                continue
            deleted = page.get("deleted")
            if isinstance(deleted, dict) and deleted.get("value") not in (None, 0, False):
                continue
            idx = page.get("idx")
            key = idx.get("value") if isinstance(idx, dict) else None
            entry = PageEntry(page["id"], _redir(page.get("redir")) if is_pdf else None)
            keyed.append((key if isinstance(key, str) else "", position, entry))
        keyed.sort(key=lambda item: (item[0], item[1]))
        entries = [entry for _, _, entry in keyed]
    else:
        old = content.get("pages")
        if isinstance(old, list):
            redirections = content.get("redirectionPageMap")
            mapping = redirections if isinstance(redirections, list) else None
            for position, page_id in enumerate(old[:MAX_PAGES]):
                if not is_uuid(page_id):
                    continue
                target: int | None = None
                if is_pdf:
                    if mapping is None:
                        target = position
                    elif position < len(mapping):
                        target = _int_or_none(mapping[position])
                entries.append(PageEntry(page_id, target))
    seen: set[str] = set()
    unique: list[PageEntry] = []
    for listed in entries:
        if listed.page_id not in seen:
            seen.add(listed.page_id)
            unique.append(listed)
    return unique


# ---- page sources and hashes -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageSource:
    """Everything that decides how one page of one version looks."""

    document: str
    page_id: str
    idx: int  # 0-based
    rm_sha: str | None
    pdf_sha: str | None
    pdf_page: int | None  # 0-based page of the PDF, when pdf_sha is set
    content_hash: str


def content_hash(rm_sha: str | None, pdf_sha: str | None, pdf_page: int | None) -> str:
    """The page's content hash (see the module docstring)."""
    if rm_sha is not None and pdf_sha is None:
        return rm_sha
    canon = (
        f"rm={rm_sha or '-'}\npdf={pdf_sha or '-'}\n"
        f"page={pdf_page if pdf_sha is not None and pdf_page is not None else '-'}\n"
    )
    return hashlib.sha256(_HASH_DOMAIN + canon.encode("ascii")).hexdigest()


def _content_json(paths: AccountPaths, version: Version) -> dict[str, Any]:
    sha = version.files.get(doc_file_name(version.document, "content"))
    if sha is None:
        return {}
    try:
        raw = read_blob(paths, sha, limit=MAX_CONTENT_BYTES)
        value = json.loads(raw)
    except (ScriptoriumError, ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def page_sources(paths: AccountPaths, version: Version) -> list[PageSource]:
    """Every page of ``version`` in order, with its inputs and content hash."""
    uuid = version.document
    content = _content_json(paths, version)
    pdf_sha = version.files.get(doc_file_name(uuid, "pdf"))
    file_type = content.get("fileType")
    is_pdf = pdf_sha is not None and file_type != "notebook"
    out: list[PageSource] = []
    for idx, entry in enumerate(page_list(content, is_pdf=is_pdf)):
        try:
            rm_sha = version.files.get(page_name(uuid, entry.page_id))
        except ScriptoriumError:
            continue
        page_pdf = pdf_sha if is_pdf and entry.pdf_page is not None else None
        pdf_page = entry.pdf_page if page_pdf is not None else None
        out.append(
            PageSource(
                document=uuid,
                page_id=entry.page_id,
                idx=idx,
                rm_sha=rm_sha,
                pdf_sha=page_pdf,
                pdf_page=pdf_page,
                content_hash=content_hash(rm_sha, page_pdf, pdf_page),
            )
        )
    return out


def content_tags(paths: AccountPaths, version: Version) -> TagSet:
    """The tags in ``version``'s ``.content`` (``library/tags.py``), read with the same size
    cap as :func:`page_sources`; no ``.content``, or one that does not parse, has none."""
    return parse_tags(_content_json(paths, version))


# ---- PNG files -------------------------------------------------------------------------------


def png_relpath(page_id: str, content_hash: str, *, max_side: int | None = None) -> str:
    """``pages/<page-uuid>/<content-hash>[.<N>px].png``, relative to the account root."""
    if not is_uuid(page_id) or not is_sha256(content_hash):
        raise InvalidArgumentError("not a page reference")
    if max_side is not None and not (type(max_side) is int and 1 <= max_side <= 32_000):
        raise InvalidArgumentError("not an image size")
    suffix = "" if max_side is None else f".{max_side}px"
    return f"pages/{page_id}/{content_hash}{suffix}.png"


def resolve_png(paths: AccountPaths, relpath: str) -> Path:
    """The absolute path of a :func:`png_relpath`, asserted inside ``pages/``."""
    parts = relpath.split("/")
    if len(parts) != 3 or parts[0] != "pages":
        raise InvalidArgumentError("not a page image path")
    page_id, name = parts[1], parts[2]
    stem = name.removesuffix(".png")
    content, _, size = stem.partition(".")
    expected = png_relpath(page_id, content, max_side=_px(size) if size else None)
    if expected != relpath:
        raise InvalidArgumentError("not a page image path")
    return resolve_inside(paths.pages, paths.pages / page_id / name)


def _px(text: str) -> int:
    if not text.endswith("px") or not text[:-2].isdigit() or len(text) > 7:
        raise InvalidArgumentError("not a page image path")
    return int(text[:-2])


def write_png(paths: AccountPaths, relpath: str, data: bytes) -> Path:
    """Write ``data`` to ``relpath`` atomically (temporary file, fsync, rename)."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise InvalidArgumentError("not a PNG")
    target = resolve_png(paths, relpath)
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    resolve_inside(paths.pages, directory)
    tmp = directory / f".{target.name}.{secrets.token_hex(6)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()  # our own temporary file under pages/, never archive data
        raise
    return target


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height from a PNG's header, without decoding it."""
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        raise InvalidArgumentError("not a PNG")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def blank_page_png(scale: float = 1.0) -> bytes:
    """A white page of the Move's paper size at ``PX_PER_UNIT x scale``: a page with no ink."""
    width = max(1, round(BLANK_PAGE_UNITS[0] * PX_PER_UNIT * scale))
    height = max(1, round(BLANK_PAGE_UNITS[1] * PX_PER_UNIT * scale))
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()
