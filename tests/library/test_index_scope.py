"""``documents.excluded_at`` through the index (M2 sync scope, §6.1)."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from scriptorium.library.index import Index
from tests.library.test_index import T0, Clock, doc, folder, uid


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    with Index.open(tmp_path / "index.sqlite", migrate=True, clock=Clock()) as ix:
        yield ix


def test_excluded_at_round_trips_and_every_upsert_sets_it(index: Index) -> None:
    out = T0 + timedelta(hours=1)
    index.upsert_document(doc(1, "Kept", hash_root="a" * 64, generation=3))
    index.upsert_document(doc(2, "Out", excluded_at=out))
    one, two = index.document(uid(1)), index.document(uid(2))
    assert one is not None and one.excluded_at is None
    assert two is not None and two.excluded_at == out and two.hash_root is None
    # Leaving the scope keeps the archived version; coming back clears the mark.
    index.upsert_document(dataclasses.replace(one, excluded_at=out))
    left = index.document(uid(1))
    assert left is not None and left.excluded_at == out
    assert (left.hash_root, left.generation) == ("a" * 64, 3)
    index.upsert_document(dataclasses.replace(left, excluded_at=None))
    back = index.document(uid(1))
    assert back is not None and back.excluded_at is None


def test_document_uuids_can_split_on_the_scope(index: Index) -> None:
    out = T0 + timedelta(hours=1)
    index.upsert_document(folder(1, "Folder"))
    index.upsert_document(doc(2, "In", parent=uid(1)))
    index.upsert_document(doc(3, "Out", excluded_at=out))
    index.upsert_document(doc(4, "Gone", excluded_at=out))
    index.mark_deleted(uid(4), "unknown", out)
    assert index.document_uuids(deleted=False) == {uid(1), uid(2), uid(3)}
    assert index.document_uuids(deleted=False, excluded=True) == {uid(3)}
    assert index.document_uuids(deleted=False, excluded=False) == {uid(1), uid(2)}
    assert index.document_uuids(deleted=True, excluded=True) == {uid(4)}
    assert index.document_uuids(deleted=True, excluded=False) == frozenset()
