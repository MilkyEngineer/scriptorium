"""Strict, bounded parsing of the sync 1.5 wire formats."""

from __future__ import annotations

import hashlib
import json

import pytest

from scriptorium.cloud.errors import CloudProtocolError, ResponseTooLargeError
from scriptorium.cloud.protocol import (
    MAX_INDEX_BYTES,
    hash_entries,
    is_document_file,
    parse_document_index,
    parse_index,
    parse_root,
    parse_root_pointer,
)

DOC = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
PAGE = "00000000-0000-4000-8000-000000000101"
H1 = "a" * 64
H2 = "b" * 64


def body(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def test_schema_3_and_4_parse() -> None:
    three = parse_index(body("3", f"{H1}:0:{DOC}.metadata:0:10"), max_entries=10, what="x")
    four = parse_index(body("4", "0:.:1:10", f"{H1}:0:{DOC}:3:10"), max_entries=10, what="x")
    assert [(e.id, e.size) for e in three] == [(f"{DOC}.metadata", 10)]
    assert [(e.id, e.subfiles) for e in four] == [(DOC, 3)]


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"5\n",
        body("4", f"{H1}:0:{DOC}:3:10"),  # no schema 4 header
        body("4", "0:.:2:10", f"{H1}:0:{DOC}:3:10"),  # header count is wrong
        body("3", f"{H1}:0:{DOC}:3"),  # a field missing
        body("3", f"{H1[:-1]}:0:{DOC}:0:1"),  # short hash
        body("3", f"{H1[:-1]}g:0:{DOC}:0:1"),  # not hex
        body("3", f"{H1}:0:{DOC}:0:-1"),
        body("3", f"{H1}:0:{DOC}:0:" + chr(0x661)),  # a non-ASCII digit int() accepts
        body("3", f"{H1}:0:{DOC}:0:1", f"{H2}:0:{DOC}:0:1"),  # one id twice
        body("3", f"{H1}:0:a b:0:1"),
        body("3", ""),  # an empty line inside
        b"3\n\xff\n",
    ],
)
def test_malformed_indexes_are_refused(raw: bytes) -> None:
    with pytest.raises(CloudProtocolError):
        parse_index(raw, max_entries=10, what="x")


def test_index_bounds() -> None:
    with pytest.raises(ResponseTooLargeError):
        parse_index(b"3\n" + b"x" * MAX_INDEX_BYTES, max_entries=10, what="x")
    lines = [f"{H1}:0:{DOC}.{n}:0:1" for n in range(3)]
    with pytest.raises(ResponseTooLargeError):
        parse_index(body("3", *lines), max_entries=2, what="x")


@pytest.mark.parametrize(
    "pointer",
    [
        b"not json",
        b"[]",
        json.dumps({"hash": H1}).encode(),
        json.dumps({"hash": H1, "generation": True}).encode(),
        json.dumps({"hash": H1, "generation": -1}).encode(),
        json.dumps({"hash": H1, "generation": 2**63}).encode(),
        json.dumps({"hash": H1, "generation": 1.5}).encode(),
        json.dumps({"hash": "../" + H1[3:], "generation": 1}).encode(),
        json.dumps({"hash": H1[:-1] + "G", "generation": 1}).encode(),
    ],
)
def test_bad_root_pointers_are_refused(pointer: bytes) -> None:
    with pytest.raises(CloudProtocolError):
        parse_root_pointer(pointer)


def test_root_pointer() -> None:
    raw = json.dumps({"hash": H1, "generation": 7, "schemaVersion": 4}).encode()
    assert parse_root_pointer(raw) == (H1, 7)


def test_hashes_are_normalised_to_lowercase() -> None:
    upper = "AbCdEf" + "0" * 58
    pointer = json.dumps({"hash": upper, "generation": 1}).encode()
    assert parse_root_pointer(pointer) == (upper.lower(), 1)

    [entry] = parse_index(body("3", f"{upper}:0:{DOC}.pdf:0:1"), max_entries=1, what="x")
    assert entry.hash == upper.lower()

    raw = body("3", f"{upper}:0:{DOC}.pdf:0:1")
    by_entries = hash_entries(parse_index(raw, max_entries=1, what="x"))
    index = parse_document_index(raw, uuid=DOC, hash=by_entries.upper(), max_files=1)
    assert index.hash == by_entries
    assert index.files[f"{DOC}.pdf"].sha256 == upper.lower()
    assert index.body == raw

    root_raw = body("4", "0:.:1:1", f"{upper}:0:{DOC}:1:1")
    by_body = hashlib.sha256(root_raw).hexdigest()
    root = parse_root(root_raw, root_hash=by_body.upper(), generation=1, max_entries=1)
    assert root.hash == by_body
    assert root.entries[0].hash == upper.lower()


def test_root_must_match_its_hash_by_either_address() -> None:
    raw = body("4", "0:.:2:20", f"{H1}:0:{DOC}:3:10", f"{H2}:0:../../etc:1:10")
    by_body = hashlib.sha256(raw).hexdigest()
    root = parse_root(raw, root_hash=by_body, generation=3, max_entries=10)
    assert [e.uuid for e in root.entries] == [DOC]
    assert root.rejected == 1

    entries = parse_index(raw, max_entries=10, what="x")
    by_entries = hash_entries(entries)
    assert parse_root(raw, root_hash=by_entries, generation=3, max_entries=10).entries

    with pytest.raises(CloudProtocolError):
        parse_root(raw, root_hash=H1, generation=3, max_entries=10)


def test_document_index_keeps_only_allowlisted_names_of_its_document() -> None:
    names = [
        f"{DOC}.metadata",
        f"{DOC}.content",
        f"{DOC}/{PAGE}.rm",
        f"{OTHER}.metadata",
        f"{DOC}/{PAGE}-metadata.json",  # not on A's allowlist
        f"{DOC}/../{OTHER}.metadata",
        f"{DOC}.metadata/../../x",
        f"/{DOC}.pdf",
        f"{DOC}.thumbnails/{PAGE}.png",
        DOC,
    ]
    lines = [f"{hashlib.sha256(n.encode()).hexdigest()}:0:{n}:0:1" for n in names]
    raw = body("3", *lines)
    address = hash_entries(parse_index(raw, max_entries=20, what="x"))

    index = parse_document_index(raw, uuid=DOC, hash=address, max_files=20)

    assert sorted(index.files) == sorted(names[:3])
    assert index.skipped == 7
    with pytest.raises(TypeError):
        index.files["x"] = index.files[names[0]]  # type: ignore[index]


def test_is_document_file() -> None:
    assert is_document_file(DOC, f"{DOC}.pdf")
    assert is_document_file(DOC, f"{DOC}/{PAGE}.rm")
    assert not is_document_file(DOC, DOC)
    assert not is_document_file(DOC, f"{OTHER}.pdf")
    assert not is_document_file(DOC, f"{DOC}.PDF")
