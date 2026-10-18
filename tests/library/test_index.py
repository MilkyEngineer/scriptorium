from __future__ import annotations

import base64
import json
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scriptorium.db.connect import open_sqlite
from scriptorium.db.migrations import SchemaMismatch, migrate
from scriptorium.db.schema_account import ACCOUNT_STEPS
from scriptorium.errors import InvalidArgumentError, NotFoundError
from scriptorium.library.index import DocumentHit, DocumentRow, Index, PageRow, SearchHit
from tests.db.sqlite_support import requires_fts5

pytestmark = requires_fts5

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012x}"


def folder(n: int, name: str, parent: str | None = None, **kw: Any) -> DocumentRow:
    return DocumentRow(
        **{"last_modified": T0, **kw},
        uuid=uid(n),
        type="CollectionType",
        parent=parent,
        name=name,
    )


def doc(n: int, name: str, parent: str | None = None, **kw: Any) -> DocumentRow:
    return DocumentRow(
        **{"last_modified": T0, **kw},
        uuid=uid(n),
        type="DocumentType",
        parent=parent,
        name=name,
    )


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def index(tmp_path: Path, clock: Clock) -> Iterator[Index]:
    with Index.open(tmp_path / "index.sqlite", migrate=True, clock=clock) as ix:
        yield ix


def all_children(ix: Index, parent: str | None, limit: int, **kw: bool) -> list[str]:
    names: list[str] = []
    cursor = None
    while True:
        page = ix.children(
            parent,
            include_trash=kw.get("include_trash", False),
            include_deleted=kw.get("include_deleted", False),
            cursor=cursor,
            limit=limit,
        )
        names.extend(d.name for d in page.items)
        if page.next_cursor is None:
            return names
        cursor = page.next_cursor


# ---- open and versions --------------------------------------------------------------------


def test_open_migrates_and_reports_version(tmp_path: Path) -> None:
    with Index.open(tmp_path / "i.sqlite", migrate=True) as ix:
        assert ix.version() == len(ACCOUNT_STEPS)
    with Index.open(tmp_path / "i.sqlite", migrate=False) as ix:
        assert ix.version() == len(ACCOUNT_STEPS)


def test_worker_open_refuses_a_missing_file_without_creating_it(tmp_path: Path) -> None:
    path = tmp_path / "i.sqlite"
    with pytest.raises(SchemaMismatch) as caught:
        Index.open(path, migrate=False)
    assert (caught.value.found, caught.value.expected) == (0, len(ACCOUNT_STEPS))
    assert not path.exists()


def test_worker_open_refuses_an_older_schema(tmp_path: Path) -> None:
    path = tmp_path / "i.sqlite"
    open_sqlite(path).close()  # exists, user_version 0
    with pytest.raises(SchemaMismatch):
        Index.open(path, migrate=False)


def test_both_opens_refuse_a_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "i.sqlite"
    conn = open_sqlite(path)
    migrate(conn, ACCOUNT_STEPS)
    conn.execute(f"PRAGMA user_version={len(ACCOUNT_STEPS) + 1}")
    conn.close()
    for flag in (False, True):
        with pytest.raises(SchemaMismatch) as caught:
            Index.open(path, migrate=flag)
        assert caught.value.found == len(ACCOUNT_STEPS) + 1


# ---- documents ----------------------------------------------------------------------------


def test_upsert_and_read_back(index: Index) -> None:
    row = doc(
        1,
        "Notes",
        last_opened=T0 - timedelta(days=3),
        source_kind="notebook",
        page_count=4,
        hash_root="ab" * 32,
        generation=7,
    )
    index.upsert_document(row)
    assert index.document(uid(1)) == row
    assert index.document(uid(2)) is None


def test_root_spelled_as_empty_string_is_none(index: Index) -> None:
    index.upsert_document(doc(1, "A", parent=""))
    got = index.document(uid(1))
    assert got is not None and got.parent is None


def test_upsert_updates_but_leaves_tombstone_fields_alone(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    index.mark_deleted(uid(1), "unknown", T0)
    index.upsert_document(doc(1, "B", page_count=2))
    got = index.document(uid(1))
    assert got is not None
    assert (got.name, got.page_count, got.deleted_at, got.deletion_cause) == (
        "B",
        2,
        T0,
        "unknown",
    )


def test_upsert_validates(index: Index) -> None:
    with pytest.raises(InvalidArgumentError):
        index.upsert_document(
            DocumentRow(uuid=uid(1), type="Bogus", parent=None, name="x", last_modified=T0)
        )
    with pytest.raises(InvalidArgumentError):
        index.upsert_document(doc(1, "x", deleted_at=T0))
    with pytest.raises(InvalidArgumentError):
        index.upsert_document(doc(1, "x", deleted_at=T0, deletion_cause="whim"))
    with pytest.raises(InvalidArgumentError):
        index.upsert_document(doc(1, "x", last_modified=datetime(2026, 1, 1)))


def test_mark_and_unmark_deleted(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    index.mark_deleted(uid(1), "aged", T0)
    got = index.document(uid(1))
    assert got is not None and (got.deleted_at, got.deletion_cause) == (T0, "aged")
    index.unmark_deleted(uid(1))
    got = index.document(uid(1))
    assert got is not None and (got.deleted_at, got.deletion_cause) == (None, None)
    with pytest.raises(InvalidArgumentError):
        index.mark_deleted(uid(1), "bored", T0)
    with pytest.raises(NotFoundError):
        index.mark_deleted(uid(9), "unknown", T0)
    with pytest.raises(NotFoundError):
        index.unmark_deleted(uid(9))


# ---- children and cursors -------------------------------------------------------------------


def test_children_sort_folders_first_then_name_case_insensitively(index: Index) -> None:
    index.upsert_document(doc(1, "beta"))
    index.upsert_document(doc(2, "Alpha"))
    index.upsert_document(folder(3, "zeta"))
    index.upsert_document(folder(4, "Eta"))
    index.upsert_document(doc(5, "alpha"))  # same name ignoring case: uuid breaks the tie
    page = index.children(None, include_trash=False, include_deleted=False, cursor=None, limit=50)
    assert [d.name for d in page.items] == ["Eta", "zeta", "Alpha", "alpha", "beta"]
    assert page.next_cursor is None


def test_children_cursor_round_trip_at_every_limit(index: Index) -> None:
    for n in range(23):
        maker = folder if n % 4 == 0 else doc
        index.upsert_document(maker(n + 1, f"{'aAbB'[n % 4]}{n:02d}"))
    expected = all_children(index, None, 100)
    assert len(expected) == 23
    for limit in (1, 2, 3, 7, 22, 23, 24):
        assert all_children(index, None, limit) == expected


def test_cursor_is_opaque_sort_key_not_offset(index: Index) -> None:
    for n in range(3):
        index.upsert_document(doc(n + 1, f"d{n}"))
    page = index.children(None, include_trash=False, include_deleted=False, cursor=None, limit=1)
    assert page.next_cursor is not None
    raw = page.next_cursor + "=" * (-len(page.next_cursor) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(raw))
    assert decoded == ["children", "DocumentType", "d0", uid(1)]
    assert "=" not in page.next_cursor and "+" not in page.next_cursor


def test_cursor_is_stable_under_inserts(index: Index) -> None:
    for n in range(6):
        index.upsert_document(doc(n + 1, f"m{n}"))
    first = index.children(None, include_trash=False, include_deleted=False, cursor=None, limit=3)
    assert [d.name for d in first.items] == ["m0", "m1", "m2"]
    # Rows that sort before the boundary must not shift the next page; rows after it appear.
    index.upsert_document(doc(10, "a-before"))
    index.upsert_document(folder(11, "folder-before"))
    index.upsert_document(doc(12, "m25-after"))
    second = index.children(
        None, include_trash=False, include_deleted=False, cursor=first.next_cursor, limit=10
    )
    assert [d.name for d in second.items] == ["m25-after", "m3", "m4", "m5"]


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64 !",
        base64.urlsafe_b64encode(b"[1,2]").decode(),
        base64.urlsafe_b64encode(b'["recent","x","y"]').decode(),
        base64.urlsafe_b64encode(b'["children","a","b",3]').decode(),
        base64.urlsafe_b64encode(b'{"offset":10}').decode(),
    ],
)
def test_bad_cursors_are_invalid_argument(index: Index, cursor: str) -> None:
    with pytest.raises(InvalidArgumentError):
        index.children(None, include_trash=False, include_deleted=False, cursor=cursor, limit=5)


def test_limit_bounds(index: Index) -> None:
    for limit in (0, 1001):
        with pytest.raises(InvalidArgumentError):
            index.children(
                None, include_trash=False, include_deleted=False, cursor=None, limit=limit
            )


def test_children_trash_and_deleted_filters(index: Index) -> None:
    index.upsert_document(folder(1, "F"))
    index.upsert_document(doc(2, "in-folder", parent=uid(1)))
    index.upsert_document(doc(3, "at-root"))
    index.upsert_document(doc(4, "trashed", parent="trash"))
    index.upsert_document(doc(5, "gone"))
    index.mark_deleted(uid(5), "unknown", T0)
    assert all_children(index, None, 10) == ["F", "at-root"]
    assert all_children(index, None, 10, include_trash=True) == ["F", "at-root", "trashed"]
    assert all_children(index, None, 10, include_deleted=True) == ["F", "at-root", "gone"]
    assert all_children(index, "trash", 10) == ["trashed"]
    assert all_children(index, uid(1), 10) == ["in-folder"]
    trash = index.trash(None, 10)
    assert [d.name for d in trash.items] == ["gone", "trashed"]


def test_trash_paginates(index: Index) -> None:
    for n in range(5):
        index.upsert_document(doc(n + 1, f"t{n}", parent="trash"))
    first = index.trash(None, 2)
    assert first.next_cursor is not None
    rest = index.trash(first.next_cursor, 10)
    assert [d.name for d in first.items + rest.items] == ["t0", "t1", "t2", "t3", "t4"]
    with pytest.raises(InvalidArgumentError):
        index.children(
            None, include_trash=False, include_deleted=False, cursor=first.next_cursor, limit=2
        )


# ---- paths ----------------------------------------------------------------------------------


def test_resolve_path_walks_names_and_reports_duplicates(index: Index) -> None:
    index.upsert_document(folder(1, "Notes"))
    index.upsert_document(folder(2, "Projects", parent=uid(1)))
    index.upsert_document(folder(3, "Projects", parent=uid(1)))  # duplicate folder name
    index.upsert_document(doc(4, "2026-10", parent=uid(2)))
    index.upsert_document(doc(5, "2026-10", parent=uid(3)))
    index.upsert_document(doc(6, "Solo", parent=uid(2)))
    index.upsert_document(doc(7, "Notes"))  # a document with a folder's name at the root
    assert index.resolve_path(["Notes", "Projects", "2026-10"]) == [uid(4), uid(5)]
    assert index.resolve_path(["Notes", "Projects", "Solo"]) == [uid(6)]
    assert index.resolve_path(["Notes", "Projects"]) == [uid(2), uid(3)]
    assert index.resolve_path(["Notes"]) == [uid(1), uid(7)]
    assert index.resolve_path(["Notes", "Nope"]) == []
    assert index.resolve_path(["notes"]) == []  # exact names
    assert index.resolve_path([]) == []


def test_resolve_path_skips_trash_and_tombstones_and_documents_mid_path(index: Index) -> None:
    index.upsert_document(folder(1, "F"))
    index.upsert_document(doc(2, "x", parent=uid(1)))
    index.upsert_document(folder(3, "T", parent="trash"))
    index.upsert_document(doc(4, "x", parent=uid(3)))
    index.upsert_document(folder(5, "G"))
    index.mark_deleted(uid(5), "unknown", T0)
    index.upsert_document(doc(6, "D"))
    index.upsert_document(doc(7, "x", parent=uid(6)))  # a "child" of a document
    assert index.resolve_path(["F", "x"]) == [uid(2)]
    assert index.resolve_path(["T", "x"]) == []
    assert index.resolve_path(["G"]) == []
    assert index.resolve_path(["D", "x"]) == []


# ---- recent -------------------------------------------------------------------------------


def test_recent_orders_newest_first_and_paginates(index: Index) -> None:
    for n in range(7):
        index.upsert_document(doc(n + 1, f"r{n}", last_modified=T0 + timedelta(minutes=n % 4)))
    index.upsert_document(doc(20, "old", last_modified=T0 - timedelta(days=1)))
    since = T0
    collected: list[tuple[datetime, str]] = []
    cursor = None
    while True:
        page = index.recent(since, cursor, 2)
        collected.extend((c.last_modified, c.uuid) for c in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert len(collected) == 7
    assert collected == sorted(collected, key=lambda k: (-k[0].timestamp(), k[1]))
    assert uid(20) not in {u for _, u in collected}


def test_recent_cursor_stable_under_inserts(index: Index) -> None:
    for n in range(4):
        index.upsert_document(doc(n + 1, f"r{n}", last_modified=T0 + timedelta(minutes=n)))
    first = index.recent(T0, None, 2)
    assert [c.name for c in first.items] == ["r3", "r2"]
    index.upsert_document(doc(9, "newest", last_modified=T0 + timedelta(hours=1)))
    second = index.recent(T0, first.next_cursor, 10)
    assert [c.name for c in second.items] == ["r1", "r0"]


def test_recent_includes_tombstones(index: Index) -> None:
    index.upsert_document(doc(1, "gone"))
    index.mark_deleted(uid(1), "unknown", T0)
    (change,) = index.recent(T0, None, 5).items
    assert change.deleted_at == T0


# ---- history ------------------------------------------------------------------------------


def test_history_is_newest_generation_first_and_idempotent(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    for gen in (3, 1, 2):
        index.append_history(uid(1), gen, f"h{gen}", T0 + timedelta(minutes=gen))
    index.append_history(uid(1), 2, "other", T0)  # same generation again: first row kept
    index.append_history(uid(1), 4, "h4", T0, index_sha256="a" * 64)
    rows = index.history(uid(1))
    assert [(r.generation, r.hash_root) for r in rows] == [
        (4, "h4"),
        (3, "h3"),
        (2, "h2"),
        (1, "h1"),
    ]
    assert rows[1].seen_at == T0 + timedelta(minutes=3)
    assert [r.index_sha256 for r in rows] == ["a" * 64, None, None, None]
    assert index.history(uid(2)) == []


def test_history_without_document_fails_the_transaction(index: Index) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        index.append_history(uid(1), 1, "h", T0)
    assert index.history(uid(1)) == []


# ---- pages --------------------------------------------------------------------------------


def pr(n: int, idx: int, h: str, **kw: Any) -> PageRow:
    return PageRow(page_id=uid(100 + n), document=uid(1), idx=idx, content_hash=h, **kw)


def test_replace_pages_keeps_render_state_of_unchanged_pages(index: Index) -> None:
    index.upsert_document(doc(1, "A", page_count=3))
    index.replace_pages(uid(1), [pr(0, 0, "a"), pr(1, 1, "b"), pr(2, 2, "c")])
    index.set_rendered(uid(100), "pages/a.png", T0)
    index.set_rendered(uid(101), "pages/b.png", T0)
    later = T0 + timedelta(hours=1)
    # Page 0 moves to the end unchanged, page 1 changes, page 2 is removed, page 3 is new.
    index.replace_pages(
        uid(1),
        [pr(1, 0, "b2", stable_since=later), pr(3, 1, "d", stable_since=later), pr(0, 2, "a")],
    )
    pages = index.pages(uid(1))
    assert [(p.page_id, p.idx, p.content_hash) for p in pages] == [
        (uid(101), 0, "b2"),
        (uid(103), 1, "d"),
        (uid(100), 2, "a"),
    ]
    moved, changed, new = pages[2], pages[0], pages[1]
    assert (moved.rendered_at, moved.png_path) == (T0, "pages/a.png")
    assert (changed.rendered_at, changed.png_path, changed.stable_since) == (
        None,
        "pages/b.png",  # last good image kept until re-rendered
        later,
    )
    assert (new.rendered_at, new.png_path) == (None, None)
    assert index.page(uid(1), 1) == new
    assert index.page(uid(1), 5) is None
    with pytest.raises(NotFoundError):
        index.set_rendered(uid(102), "x.png", T0)  # dropped from the current view


def test_replace_pages_swaps_indices(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    index.replace_pages(uid(1), [pr(0, 0, "a"), pr(1, 1, "b")])
    index.replace_pages(uid(1), [pr(0, 1, "a"), pr(1, 0, "b")])
    assert [p.page_id for p in index.pages(uid(1))] == [uid(101), uid(100)]


def test_replace_pages_validates(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    with pytest.raises(InvalidArgumentError):
        index.replace_pages(uid(1), [pr(0, 0, "a"), pr(0, 1, "a")])
    with pytest.raises(InvalidArgumentError):
        index.replace_pages(uid(1), [pr(0, 0, "a"), pr(1, 0, "b")])
    with pytest.raises(InvalidArgumentError):
        index.replace_pages(uid(1), [pr(0, -1, "a")])
    with pytest.raises(InvalidArgumentError):
        index.replace_pages(uid(2), [pr(0, 0, "a")])
    index.replace_pages(uid(1), [])
    assert index.pages(uid(1)) == []


# ---- transcripts and search -----------------------------------------------------------------


def test_put_and_get_transcript(index: Index, clock: Clock) -> None:
    index.upsert_document(doc(1, "A"))
    index.replace_pages(uid(1), [pr(0, 0, "h0")])
    clock.now = T0 + timedelta(minutes=5)
    index.put_transcript(uid(100), "h0", "first text", "model-x", "lazy")
    got = index.transcript(uid(100), "h0")
    assert got is not None
    assert (got.text, got.model, got.mode, got.document, got.created_at) == (
        "first text",
        "model-x",
        "lazy",
        uid(1),
        clock.now,
    )
    index.put_transcript(uid(100), "h0", "second text", "model-y", "user")
    got = index.transcript(uid(100), "h0")
    assert got is not None and (got.text, got.mode) == ("second text", "user")
    assert index.transcript(uid(100), "other") is None
    with pytest.raises(NotFoundError):
        index.put_transcript(uid(999), "h", "t", "m", "lazy")
    with pytest.raises(InvalidArgumentError):
        index.put_transcript(uid(100), "h0", "t", "m", "psychic")


def test_search_returns_the_page_ref(index: Index) -> None:
    index.upsert_document(folder(1, "Meetings"))
    index.upsert_document(doc(2, "Kickoff", parent=uid(1)))
    pages = [
        PageRow(page_id=uid(200), document=uid(2), idx=0, content_hash="p0"),
        PageRow(page_id=uid(201), document=uid(2), idx=1, content_hash="p1"),
    ]
    index.replace_pages(uid(2), pages)
    index.put_transcript(uid(200), "p0", "agenda and budget", "m", "lazy")
    index.put_transcript(uid(201), "p1", "the quarterly budget review", "m", "lazy")
    hits = index.search("quarterly budget", cursor=None, limit=10).items
    assert len(hits) == 1
    hit = hits[0]
    assert (hit.page_id, hit.document, hit.idx, hit.content_hash, hit.current) == (
        uid(201),
        uid(2),
        1,
        "p1",
        True,
    )
    assert (hit.doc_name, hit.doc_path) == ("Kickoff", "Meetings")
    assert "quarterly" in hit.snippet
    # Names and folder paths are searchable too.
    assert {h.page_id for h in index.search("meetings", cursor=None, limit=10).items} == {
        uid(200),
        uid(201),
    }
    assert {h.page_id for h in index.search("kickoff", cursor=None, limit=10).items} == {
        uid(200),
        uid(201),
    }


def test_search_quotes_operators_and_rejects_empty(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    index.replace_pages(uid(1), [pr(0, 0, "h")])
    index.put_transcript(uid(100), "h", 'say "NEAR" OR not', "m", "lazy")
    assert len(index.search('"NEAR" OR (', cursor=None, limit=5).items) == 1
    assert index.search("NOT AND", cursor=None, limit=5).items == []
    with pytest.raises(InvalidArgumentError):
        index.search("   ", cursor=None, limit=5)


def test_search_marks_old_versions_and_follows_renames(index: Index) -> None:
    index.upsert_document(folder(1, "Old folder"))
    index.upsert_document(doc(2, "Doc", parent=uid(1)))
    index.replace_pages(uid(2), [PageRow(uid(300), uid(2), 0, "v1")])
    index.put_transcript(uid(300), "v1", "lighthouse", "m", "lazy")
    index.replace_pages(uid(2), [PageRow(uid(300), uid(2), 0, "v2")])
    (hit,) = index.search("lighthouse", cursor=None, limit=5).items
    assert (hit.current, hit.idx) == (False, 0)
    # Renaming the parent folder re-indexes the path of everything below it.
    index.upsert_document(folder(1, "New folder"))
    assert index.search("old", cursor=None, limit=5).items == []
    (hit,) = index.search("new", cursor=None, limit=5).items
    assert hit.doc_path == "New folder"
    # Moving the document to the root clears its path.
    index.upsert_document(doc(2, "Doc"))
    (hit,) = index.search("lighthouse", cursor=None, limit=5).items
    assert hit.doc_path == ""
    assert index.search("new", cursor=None, limit=5).items == []


def test_search_paginates(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    index.replace_pages(uid(1), [pr(n, n, f"h{n}") for n in range(5)])
    for n in range(5):
        index.put_transcript(uid(100 + n), f"h{n}", "common " + "word " * n, "m", "lazy")
    seen: list[str] = []
    cursor = None
    while True:
        page = index.search("common", cursor=cursor, limit=2)
        seen.extend(h.page_id for h in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert sorted(seen) == [uid(100 + n) for n in range(5)]


def test_path_walk_survives_cycles_in_cloud_metadata(index: Index) -> None:
    index.upsert_document(folder(1, "A", parent=uid(2)))
    index.upsert_document(folder(2, "B", parent=uid(1)))
    index.upsert_document(doc(3, "C", parent=uid(1)))
    index.replace_pages(uid(3), [PageRow(uid(400), uid(3), 0, "h")])
    index.put_transcript(uid(400), "h", "loop", "m", "lazy")
    (hit,) = index.search("loop", cursor=None, limit=5).items
    assert hit.doc_path == "B/A"
    index.upsert_document(folder(2, "B2", parent=uid(1)))  # refresh over a cycle terminates
    (hit,) = index.search("loop", cursor=None, limit=5).items
    assert hit.doc_path == "B2/A"


def test_fts_index_stays_consistent(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    index.replace_pages(uid(1), [pr(0, 0, "h")])
    for text in ("one", "two", "three"):
        index.put_transcript(uid(100), "h", text, "m", "lazy")
        index.upsert_document(doc(1, f"A-{text}"))
    conn = index._conn
    conn.execute("INSERT INTO transcripts_fts(transcripts_fts) VALUES ('integrity-check')")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# ---- sync state and transactions ------------------------------------------------------------


def test_state_set_get_delete(index: Index) -> None:
    assert index.get_state("last_generation") is None
    index.set_state("last_generation", "12")
    index.set_state("last_generation", "13")
    assert index.get_state("last_generation") == "13"
    index.set_state("last_generation", None)
    assert index.get_state("last_generation") is None
    index.set_state("absent", None)


def test_transaction_commits_all_writes_together(index: Index, tmp_path: Path) -> None:
    other = Index.open(tmp_path / "index.sqlite", migrate=False)
    try:
        with index.transaction():
            index.append_history(uid(1), 5, "h5", T0)  # before the document: FKs are deferred
            index.upsert_document(doc(1, "A", generation=5, hash_root="h5"))
            index.replace_pages(uid(1), [pr(0, 0, "a")])
            index.set_state("last_generation", "5")
            assert other.document(uid(1)) is None  # nothing visible before COMMIT
        assert other.document(uid(1)) is not None
        assert other.get_state("last_generation") == "5"
    finally:
        other.close()


def test_transaction_rolls_back_on_exception(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    with pytest.raises(RuntimeError), index.transaction():
        index.upsert_document(doc(1, "renamed"))
        index.append_history(uid(1), 1, "h", T0)
        with index.transaction():  # nested blocks join the outer transaction
            index.set_state("k", "v")
        raise RuntimeError
    got = index.document(uid(1))
    assert got is not None and got.name == "A"
    assert index.history(uid(1)) == []
    assert index.get_state("k") is None
    index.set_state("k", "after")  # the connection is usable again
    assert index.get_state("k") == "after"


def test_transaction_rolls_back_on_constraint_failure(index: Index) -> None:
    with pytest.raises(sqlite3.IntegrityError), index.transaction():
        index.set_state("k", "v")
        index.append_history(uid(42), 1, "h", T0)  # no such document: fails at COMMIT
    assert index.get_state("k") is None


def test_threads_share_one_index(index: Index) -> None:
    errors: list[BaseException] = []

    def work(base: int) -> None:
        try:
            for n in range(20):
                with index.transaction():
                    index.upsert_document(doc(base + n, f"t{base + n}"))
                    index.append_history(uid(base + n), 1, "h", T0)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(b,)) for b in (1000, 2000, 3000)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(all_children(index, None, 100)) == 60


# ---- document_uuids and the name fallback of search ----------------------------------------


def test_document_uuids_splits_on_tombstones(index: Index) -> None:
    assert index.document_uuids(deleted=False) == frozenset()
    index.upsert_document(folder(1, "F"))
    index.upsert_document(doc(2, "A", parent=uid(1)))
    index.upsert_document(doc(3, "Trashed", parent="trash"))
    index.upsert_document(doc(4, "Gone"))
    index.mark_deleted(uid(4), "unknown", T0)
    assert index.document_uuids(deleted=False) == {uid(1), uid(2), uid(3)}
    assert index.document_uuids(deleted=True) == {uid(4)}
    index.unmark_deleted(uid(4))
    assert index.document_uuids(deleted=True) == frozenset()


def test_search_falls_back_to_names_of_untranscribed_documents(index: Index) -> None:
    index.upsert_document(folder(1, "Budget folder"))
    index.upsert_document(doc(2, "Transcribed budget", parent=uid(1)))
    index.upsert_document(doc(3, "Budget 2026", parent=uid(1)))
    index.upsert_document(doc(4, "Über BUDGET plans"))
    index.upsert_document(doc(5, "Unrelated"))
    index.upsert_document(doc(6, "Old budget"))
    index.mark_deleted(uid(6), "unknown", T0)
    index.replace_pages(uid(2), [PageRow(uid(200), uid(2), 0, "h")])
    index.put_transcript(uid(200), "h", "the budget review", "m", "lazy")

    items = index.search("budget", cursor=None, limit=10).items
    # Page hits first, then name hits sorted by name; folders, transcribed and tombstoned
    # documents never come back as name hits.
    assert isinstance(items[0], SearchHit) and items[0].page_id == uid(200)
    names = [i for i in items[1:] if isinstance(i, DocumentHit)]
    assert len(names) == len(items) - 1
    assert [(h.document, h.doc_name, h.doc_path) for h in names] == [
        (uid(3), "Budget 2026", "Budget folder"),
        (uid(4), "Über BUDGET plans", ""),
    ]
    # Case-insensitive beyond ASCII, and every term must be in the name.
    (hit,) = index.search("über plans", cursor=None, limit=10).items
    assert isinstance(hit, DocumentHit) and hit.document == uid(4)
    assert index.search("budget unrelated", cursor=None, limit=10).items == []


def test_search_name_hits_paginate_after_page_hits(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    index.replace_pages(uid(1), [pr(n, n, f"h{n}") for n in range(3)])
    for n in range(3):
        index.put_transcript(uid(100 + n), f"h{n}", "common " + "word " * n, "m", "lazy")
    for n in range(10, 15):
        index.upsert_document(doc(n, f"common {n}"))
    for limit in (1, 2, 3, 4, 8):
        seen: list[str] = []
        cursor = None
        while True:
            page = index.search("common", cursor=cursor, limit=limit)
            assert len(page.items) <= limit
            for item in page.items:
                seen.append(item.page_id if isinstance(item, SearchHit) else item.document)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert set(seen[:3]) == {uid(100 + n) for n in range(3)}
        assert seen[3:] == [uid(n) for n in range(10, 15)], limit


def test_search_refuses_bad_cursors_and_too_many_terms(index: Index) -> None:
    with pytest.raises(InvalidArgumentError):
        index.search("x", cursor="garbage", limit=5)
    with pytest.raises(InvalidArgumentError):
        index.search(" ".join(["w"] * 65), cursor=None, limit=5)
