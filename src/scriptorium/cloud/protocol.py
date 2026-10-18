"""The sync 1.5 wire formats, parsed strictly and with bounds (Architecture §7, §14).

Pure functions: bytes in, typed values out. Everything here comes from the cloud and is
attacker-controlled (CLAUDE.md, "Untrusted input"), so parsing refuses anything it does
not fully understand instead of guessing:

* An index is ``<schema>\\n`` (``3`` or ``4``; schema 4 adds ``0:.:<count>:<size>``)
  followed by ``hash:type:id:subfiles:size`` lines. A malformed line is a protocol
  error, never a skipped line: a silently dropped root entry would read as a deletion.
* An index must hash to the name it was fetched under, by either content address the
  service uses: ``HashEntries`` (SHA-256 over the entries' raw hashes sorted by id,
  confirmed for document indexes by S1) or SHA-256 of the body (schema-4 roots).
* Root entry ids must be UUIDs and document file names must pass the §11.0 allowlist
  and belong to their document; anything else is counted and left out, so no
  cloud-supplied string ever reaches a path join unchecked. What a left-out name looked
  like is kept only as its shape (:func:`name_shape`), so a new file kind shows in the logs
  without a cloud-supplied string reaching them.
* Hashes are accepted in either case and returned as lowercase hex, the only spelling
  the archive and the index store (Architecture §6).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, NamedTuple

from scriptorium.cloud.errors import CloudProtocolError, ResponseTooLargeError
from scriptorium.cloud.ports import is_safe_name

MAX_INDEX_BYTES: Final = 8 * 1024 * 1024
"""Largest root or document index accepted (a 50 000-document root is about 6 MB)."""
MAX_ROOT_ENTRIES: Final = 50_000
MAX_DOCUMENT_FILES: Final = 20_000
MAX_BLOB_BYTES: Final = 1024 * 1024 * 1024
"""Hard cap on one blob, whatever its index declares."""
MAX_POINTER_BYTES: Final = 64 * 1024
"""The root pointer JSON and the user token are both tiny."""
MAX_GENERATION: Final = 2**63 - 1
MAX_SHAPE_CHARS: Final = 24
"""Longest :func:`name_shape` (a longer shape is cut and ends in ``+``)."""
MAX_SHAPES_PER_INDEX: Final = 4
"""Most distinct shapes a :class:`DocumentIndex` keeps of the names it left out."""

_UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
_HEX64_RE: Final = re.compile(r"[0-9A-Fa-f]{64}")
# [0-9] rather than \d: Python's \d also matches non-ASCII digits, which int() accepts.
_ENTRY_RE: Final = re.compile(
    r"(?P<hash>[0-9A-Fa-f]{64}):(?P<type>[0-9A-Fa-f]{1,8}):(?P<id>[^:\s]{1,256})"
    r":(?P<subfiles>[0-9]{1,9}):(?P<size>[0-9]{1,15})"
)
#: Schema 4 puts a header line after the version: ``0:<name>:<count>:<size>``, where ``name``
#: is ``.`` for the root and the document's own UUID for a document index.
_SCHEMA4_HEADER_RE: Final = re.compile(
    r"0:(?P<name>\.|[0-9a-f-]{36}):(?P<count>[0-9]{1,9}):(?P<size>[0-9]{1,15})"
)


def is_uuid(value: str) -> bool:
    return _UUID_RE.fullmatch(value) is not None


_SHAPE_UUID_RE: Final = re.compile(r"[0-9A-Fa-f-]{36}")
_SHAPE_KEPT: Final = frozenset("./-_")


def _shape_char(char: str) -> str:
    if char in _SHAPE_KEPT:
        return char
    if char.isascii() and char.isalpha():
        return "a"
    if char.isascii() and char.isdigit():
        return "9"
    return "?"


def name_shape(uuid: str, name: str) -> str:
    """What a document file name the allowlist left out looked like, safe to log.

    The part of ``name`` after the document ``uuid``, with each UUID-shaped run as ``U``,
    ASCII letters as ``a``, ASCII digits as ``9``, ``.``, ``/``, ``-`` and ``_`` as they are
    and anything else as ``?``, cut to :data:`MAX_SHAPE_CHARS`: ``<uuid>.newkind`` is
    ``.aaaaaaa`` and ``<uuid>/<page>.rmx`` is ``/U.aaa``. A name that does not start with
    the document's UUID is ``foreign``; the bare UUID is ``bare``. No letter or digit of
    the name survives, so no cloud-chosen text reaches a log.
    """
    if not name.startswith(uuid):
        return "foreign"
    # Enough of the rest for MAX_SHAPE_CHARS shape characters, even if each is a UUID.
    rest = name[len(uuid) : len(uuid) + 37 * MAX_SHAPE_CHARS]
    if not rest:
        return "bare"
    parts = _SHAPE_UUID_RE.split(rest)
    shape = "U".join("".join(_shape_char(c) for c in part) for part in parts)
    if len(shape) > MAX_SHAPE_CHARS:
        return shape[: MAX_SHAPE_CHARS - 1] + "+"
    return shape


def is_sha256(value: str) -> bool:
    """A lowercase hex SHA-256, the normalised spelling."""
    return _SHA256_RE.fullmatch(value) is not None


def normalise_sha256(value: object) -> str | None:
    """``value`` as lowercase hex if it is a SHA-256 in hex of either case, else None."""
    if isinstance(value, str) and _HEX64_RE.fullmatch(value) is not None:
        return value.lower()
    return None


@dataclass(frozen=True, slots=True)
class IndexEntry:
    hash: str
    type: str
    id: str
    subfiles: int
    size: int


@dataclass(frozen=True, slots=True)
class RootEntry:
    """One document in the root index. ``uuid`` passed the UUID allowlist."""

    uuid: str
    hash: str
    subfiles: int
    size: int


@dataclass(frozen=True, slots=True)
class RootInfo:
    """The current root: its hash, generation and documents.

    ``rejected`` counts root entries left out because their id is not a UUID.
    """

    hash: str
    generation: int
    entries: tuple[RootEntry, ...]
    rejected: int = 0


class BlobRef(NamedTuple):
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class DocumentIndex:
    """A document's files: xochitl-relative name -> (sha256, declared size).

    Every name passed ``is_safe_name`` and belongs to ``uuid``; ``skipped`` counts the
    names that did not and were left out, and ``skipped_shapes`` holds up to
    :data:`MAX_SHAPES_PER_INDEX` of their distinct :func:`name_shape` values, sorted.
    ``body`` is the index as fetched (it matched ``hash``), kept so sync can store it in
    ``blobs/`` beside the files it lists.
    """

    uuid: str
    hash: str
    files: Mapping[str, BlobRef]
    body: bytes = field(default=b"", repr=False)
    skipped: int = 0
    skipped_shapes: tuple[str, ...] = ()


def parse_index(
    body: bytes, *, max_entries: int, what: str, owner: str | None = None
) -> list[IndexEntry]:
    """Parse a schema 3 or 4 index; raise on anything malformed.

    ``owner`` is the document UUID a document index belongs to: its schema 4 header must name
    either that UUID or ``.``. The root passes nothing, so only ``.`` is accepted there.
    """
    if len(body) > MAX_INDEX_BYTES:
        raise ResponseTooLargeError(f"{what} is larger than {MAX_INDEX_BYTES} bytes")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise CloudProtocolError(f"{what} is not UTF-8") from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0] not in ("3", "4"):
        raise CloudProtocolError(f"{what} has no known schema line")
    rest = lines[1:]
    declared: int | None = None
    if lines[0] == "4":
        header = _SCHEMA4_HEADER_RE.fullmatch(rest[0]) if rest else None
        if header is None:
            raise CloudProtocolError(f"{what} has a malformed schema 4 header")
        if header["name"] not in (".", owner):
            raise CloudProtocolError(f"{what} has a schema 4 header for another document")
        declared = int(header["count"])
        rest = rest[1:]
    if len(rest) > max_entries:
        raise ResponseTooLargeError(f"{what} has more than {max_entries} entries")
    entries: list[IndexEntry] = []
    seen: set[str] = set()
    for line in rest:
        match = _ENTRY_RE.fullmatch(line)
        if match is None:
            raise CloudProtocolError(f"{what} has a malformed entry line")
        entry_id = match["id"]
        if entry_id in seen:
            raise CloudProtocolError(f"{what} lists one id twice")
        seen.add(entry_id)
        entries.append(
            IndexEntry(
                hash=match["hash"].lower(),
                type=match["type"],
                id=entry_id,
                subfiles=int(match["subfiles"]),
                size=int(match["size"]),
            )
        )
    if declared is not None and declared != len(entries):
        raise CloudProtocolError(f"{what} declares {declared} entries but has {len(entries)}")
    return entries


def hash_entries(entries: Iterable[IndexEntry]) -> str:
    """``HashEntries``: SHA-256 over each entry's raw hash, entries sorted by id."""
    hasher = hashlib.sha256()
    for entry in sorted(entries, key=lambda e: e.id):
        hasher.update(bytes.fromhex(entry.hash))
    return hasher.hexdigest()


def matches_address(body: bytes, entries: list[IndexEntry], expected: str) -> bool:
    """True if the index is stored under either content address the service uses."""
    expected = expected.lower()
    return hash_entries(entries) == expected or hashlib.sha256(body).hexdigest() == expected


def parse_root_pointer(body: bytes) -> tuple[str, int]:
    """``GET /sync/v4/root`` → ``(root hash, generation)``."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise CloudProtocolError("root pointer is not JSON") from None
    if not isinstance(data, dict):
        raise CloudProtocolError("root pointer is not an object")
    root_hash = normalise_sha256(data.get("hash"))
    generation = data.get("generation")
    if root_hash is None:
        raise CloudProtocolError("root pointer has no valid hash")
    # bool is an int subclass; a JSON true is not a generation.
    if type(generation) is not int or not 0 <= generation <= MAX_GENERATION:
        raise CloudProtocolError("root pointer has no valid generation")
    return root_hash, generation


def parse_root(body: bytes, *, root_hash: str, generation: int, max_entries: int) -> RootInfo:
    root_hash = root_hash.lower()
    entries = parse_index(body, max_entries=max_entries, what="root index")
    if not matches_address(body, entries, root_hash):
        raise CloudProtocolError("root index does not match its hash")
    kept = tuple(
        RootEntry(uuid=e.id, hash=e.hash, subfiles=e.subfiles, size=e.size)
        for e in entries
        if is_uuid(e.id)
    )
    return RootInfo(
        hash=root_hash, generation=generation, entries=kept, rejected=len(entries) - len(kept)
    )


def is_document_file(uuid: str, name: str) -> bool:
    """``name`` is an allowlisted file of document ``uuid`` (not the bare directory)."""
    return is_safe_name(name) and (name.startswith(f"{uuid}.") or name.startswith(f"{uuid}/"))


def parse_document_index(body: bytes, *, uuid: str, hash: str, max_files: int) -> DocumentIndex:
    hash = hash.lower()
    entries = parse_index(body, max_entries=max_files, what="document index", owner=uuid)
    if not matches_address(body, entries, hash):
        raise CloudProtocolError("document index does not match its hash")
    files: dict[str, BlobRef] = {}
    shapes: set[str] = set()
    for e in entries:
        if is_document_file(uuid, e.id):
            files[e.id] = BlobRef(e.hash, e.size)
        elif len(shapes) < MAX_SHAPES_PER_INDEX:
            shapes.add(name_shape(uuid, e.id))
    return DocumentIndex(
        uuid=uuid,
        hash=hash,
        files=MappingProxyType(files),
        body=body,
        skipped=len(entries) - len(files),
        skipped_shapes=tuple(sorted(shapes)),
    )
