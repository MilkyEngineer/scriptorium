"""Tags in the index (account step V4): replace_tags, tags_of, documents_tagged,
children(tags=), and search with folder and tag filters (Architecture §6.1, §11.2).

Every name here is synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from scriptorium.db.connect import open_sqlite
from scriptorium.db.migrations import migrate
from scriptorium.db.schema_account import ACCOUNT_STEPS
from scriptorium.errors import InvalidArgumentError
from scriptorium.library.index import DocumentHit, DocumentRow, Index, PageRow, SearchHit
from scriptorium.library.tags import TagSet
from tests.db.sqlite_support import requires_fts5
from tests.fakes.index import FakeIndex

pytestmark = requires_fts5

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012x}"


def folder(n: int, name: str, parent: str | None = None) -> DocumentRow:
    return DocumentRow(uid(n), "CollectionType", parent, name, T0)


def doc(n: int, name: str, parent: str | None = None, **kw: Any) -> DocumentRow:
    return DocumentRow(uid(n), "DocumentType", parent, name, T0, **kw)


def tagset(document: set[str] | None = None, pages: Mapping[str, set[str]] | None = None) -> TagSet:
    return TagSet(
        frozenset(document or set()),
        MappingProxyType({p: frozenset(n) for p, n in (pages or {}).items()}),
    )


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    with Index.open(tmp_path / "index.sqlite", migrate=True) as ix:
        yield ix


def pages_of(ix: Index, n: int, count: int) -> None:
    ix.replace_pages(
        uid(n), [PageRow(uid(n * 100 + i), uid(n), i, f"h{n}-{i}") for i in range(count)]
    )


def listed(
    ix: Index, parent: str | None, tags: list[str], limit: int = 50, **kw: bool
) -> list[str]:
    names: list[str] = []
    cursor = None
    while True:
        page = ix.children(
            parent,
            include_trash=kw.get("include_trash", False),
            include_deleted=kw.get("include_deleted", False),
            tags=tags,
            cursor=cursor,
            limit=limit,
        )
        names.extend(d.name for d in page.items)
        if page.next_cursor is None:
            return names
        cursor = page.next_cursor


def fts_ok(ix: Index) -> None:
    conn = ix._conn
    conn.execute("INSERT INTO transcripts_fts(transcripts_fts) VALUES ('integrity-check')")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# ---- the table ------------------------------------------------------------------------------


def test_replace_tags_round_trips_and_replaces_whole(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    pages_of(index, 1, 2)
    first = tagset({"alpha", "beta"}, {uid(100): {"gamma"}})
    index.replace_tags(uid(1), first)
    assert index.tags_of(uid(1)) == first
    second = tagset({"beta"}, {uid(101): {"delta"}})
    index.replace_tags(uid(1), second)
    assert index.tags_of(uid(1)) == second  # alpha and gamma are gone
    index.replace_tags(uid(1), TagSet())
    assert index.tags_of(uid(1)) == TagSet()
    assert index.tags_of(uid(99)) == TagSet()


def test_replace_tags_refuses_what_parse_tags_cannot_produce(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    for bad in (
        tagset({f"t{n}" for n in range(101)}),
        tagset({"a\x00b"}),
        tagset({" padded "}),
        tagset({"x" * 65}),
        tagset(set(), {"../../etc": {"x"}}),
    ):
        with pytest.raises(InvalidArgumentError):
            index.replace_tags(uid(1), bad)
    assert index.tags_of(uid(1)) == TagSet()


def test_replace_tags_joins_the_callers_transaction(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    index.replace_tags(uid(1), tagset({"before"}))
    with pytest.raises(RuntimeError), index.transaction():
        pages_of(index, 1, 1)
        index.replace_tags(uid(1), tagset({"after"}))
        raise RuntimeError
    assert index.tags_of(uid(1)) == tagset({"before"})
    assert index.pages(uid(1)) == []


def test_document_rows_carry_their_own_tags_sorted(index: Index) -> None:
    index.upsert_document(doc(1, "A"))
    pages_of(index, 1, 1)
    index.replace_tags(uid(1), tagset({"zeta", "Alpha", "beta"}, {uid(100): {"page-only"}}))
    row = index.document(uid(1))
    assert row is not None and row.tags == ("Alpha", "beta", "zeta")
    # upsert_document neither reads nor clears tags.
    index.upsert_document(doc(1, "A renamed"))
    row = index.document(uid(1))
    assert row is not None and row.tags == ("Alpha", "beta", "zeta")


def test_documents_tagged_matches_own_tags_exactly(index: Index) -> None:
    index.upsert_document(folder(1, "F"))
    index.upsert_document(doc(2, "A", parent=uid(1)))
    index.upsert_document(doc(3, "B"))
    pages_of(index, 3, 1)
    index.replace_tags(uid(1), tagset({"keep"}))
    index.replace_tags(uid(2), tagset({"other", "keep"}))
    index.replace_tags(uid(3), tagset(set(), {uid(300): {"keep"}}))  # a page tag only
    assert index.documents_tagged(["keep"]) == {uid(1), uid(2)}
    assert index.documents_tagged([" keep "]) == {uid(1), uid(2)}  # normalised like storage
    assert index.documents_tagged(["Keep"]) == frozenset()
    assert index.documents_tagged(["other", "nothing"]) == {uid(2)}
    assert index.documents_tagged(["\x00", ""]) == frozenset()
    assert index.documents_tagged([]) == frozenset()
    many = [f"t{n}" for n in range(1200)] + ["other"]
    assert index.documents_tagged(many) == {uid(2)}


# ---- list(tags=) ----------------------------------------------------------------------------


@pytest.fixture
def tree(index: Index) -> Index:
    """Root: F (folder, tagged x) > G (folder) > deep (x); top (x, y); plain; trashed (x);
    gone (x, tombstoned); F > mid (y)."""
    index.upsert_document(folder(1, "F"))
    index.upsert_document(folder(2, "G", parent=uid(1)))
    index.upsert_document(doc(3, "deep", parent=uid(2)))
    index.upsert_document(doc(4, "top"))
    index.upsert_document(doc(5, "plain"))
    index.upsert_document(doc(6, "trashed", parent="trash"))
    index.upsert_document(doc(7, "gone"))
    index.upsert_document(doc(8, "mid", parent=uid(1)))
    index.mark_deleted(uid(7), "unknown", T0)
    for n, names in {1: {"x"}, 3: {"x"}, 4: {"x", "y"}, 6: {"x"}, 7: {"x"}, 8: {"y"}}.items():
        index.replace_tags(uid(n), tagset(names))
    return index


def test_children_with_tags_is_recursive_any_of(tree: Index) -> None:
    assert listed(tree, None, ["x"]) == ["F", "deep", "top"]
    assert listed(tree, None, ["y"]) == ["mid", "top"]
    assert listed(tree, None, ["x", "y"]) == ["F", "deep", "mid", "top"]
    assert listed(tree, uid(1), ["x"]) == ["deep"]  # below F, not F itself
    assert listed(tree, uid(2), ["y"]) == []
    assert listed(tree, None, ["nothing"]) == []


def test_children_with_tags_respects_trash_and_tombstones(tree: Index) -> None:
    assert listed(tree, None, ["x"], include_trash=True) == ["F", "deep", "top", "trashed"]
    assert listed(tree, None, ["x"], include_deleted=True) == ["F", "deep", "gone", "top"]
    assert listed(tree, "trash", ["x"]) == ["trashed"]


def test_children_with_tags_paginates_without_repeats(tree: Index) -> None:
    for n in range(20, 45):
        tree.upsert_document(doc(n, f"extra {n}", parent=uid(2)))
        tree.replace_tags(uid(n), tagset({"x"}))
    everything = listed(tree, None, ["x"], limit=100)
    assert len(everything) == 28
    for limit in (1, 3, 7):
        assert listed(tree, None, ["x"], limit=limit) == everything


def test_children_without_tags_is_unchanged_and_carries_tags(tree: Index) -> None:
    page = tree.children(None, include_trash=False, include_deleted=False, cursor=None, limit=50)
    assert [(d.name, d.tags) for d in page.items] == [
        ("F", ("x",)),
        ("plain", ()),
        ("top", ("x", "y")),
    ]
    empty = tree.children(
        None, include_trash=False, include_deleted=False, tags=[], cursor=None, limit=50
    )
    assert empty.items == page.items


def test_children_rejects_bad_filters(tree: Index) -> None:
    for bad in (["a\x07"], [""], [f"t{n}" for n in range(101)], "x"):
        with pytest.raises(InvalidArgumentError):
            tree.children(
                None,
                include_trash=False,
                include_deleted=False,
                tags=bad,  # type: ignore[arg-type]  # a bare string is refused too
                cursor=None,
                limit=5,
            )


def test_children_with_tags_survives_a_parent_cycle(index: Index) -> None:
    index.upsert_document(folder(1, "A", parent=uid(2)))
    index.upsert_document(folder(2, "B", parent=uid(1)))
    index.upsert_document(doc(3, "C", parent=uid(1)))
    index.replace_tags(uid(3), tagset({"x"}))
    assert listed(index, uid(1), ["x"]) == ["C"]


# ---- search ---------------------------------------------------------------------------------


@pytest.fixture
def library(index: Index) -> Index:
    """Projects (folder) > Kickoff (2 pages, transcribed); Elsewhere (1 page, transcribed);
    Sketches (untranscribed) in Projects; Loose (untranscribed) at the root."""
    index.upsert_document(folder(1, "Projects"))
    index.upsert_document(doc(2, "Kickoff", parent=uid(1)))
    index.upsert_document(doc(3, "Elsewhere"))
    index.upsert_document(doc(4, "Sketches", parent=uid(1)))
    index.upsert_document(doc(5, "Loose"))
    pages_of(index, 2, 2)
    pages_of(index, 3, 1)
    pages_of(index, 4, 1)
    pages_of(index, 5, 1)
    index.put_transcript(uid(2), uid(200), "h2-0", "agenda for the meeting", "m", "lazy")
    index.put_transcript(uid(2), uid(201), "h2-1", "meeting notes and actions", "m", "lazy")
    index.put_transcript(uid(3), uid(300), "h3-0", "unrelated meeting", "m", "lazy")
    return index


def ids(hits: list[SearchHit | DocumentHit]) -> list[str]:
    return [h.page_id if isinstance(h, SearchHit) else h.document for h in hits]


def test_tag_names_are_searchable_as_text(library: Index) -> None:
    assert library.search("lighthouse", cursor=None, limit=10).items == []
    library.replace_tags(uid(2), tagset({"lighthouse"}, {uid(201): {"harbour"}}))
    found = library.search("lighthouse", cursor=None, limit=10).items
    assert sorted(ids(found)) == [uid(200), uid(201)]
    assert all(isinstance(h, SearchHit) for h in found)
    (hit,) = library.search("harbour", cursor=None, limit=10).items
    assert isinstance(hit, SearchHit) and hit.page_id == uid(201)
    assert hit.tags == ("harbour", "lighthouse")
    # A transcript stored after the tags is indexed with them too.
    library.replace_pages(uid(2), [PageRow(uid(201), uid(2), 0, "h2-1b")])
    library.put_transcript(uid(2), uid(201), "h2-1b", "fresh text", "m", "lazy")
    assert uid(201) in ids(library.search("harbour fresh", cursor=None, limit=10).items)
    # Removing a tag on the tablet removes it from the index.
    library.replace_tags(uid(2), TagSet())
    assert library.search("harbour", cursor=None, limit=10).items == []
    assert library.search("lighthouse", cursor=None, limit=10).items == []
    fts_ok(library)


def test_tag_names_with_fts_syntax_stay_literal(library: Index) -> None:
    library.replace_tags(uid(3), tagset({'NEAR OR "x"', "a*"}))
    assert ids(library.search('"x" OR NEAR', cursor=None, limit=5).items) == [uid(300)]
    assert ids(library.search("a*", cursor=None, limit=5).items) == [uid(300)]
    assert library.search("NOT", cursor=None, limit=5).items == []
    assert ids(library.search("meeting", tags=['NEAR OR "x"'], cursor=None, limit=5).items) == [
        uid(300)
    ]


def test_tag_filter_page_hits_use_page_and_document_tags(library: Index) -> None:
    library.replace_tags(uid(2), tagset(set(), {uid(201): {"retro"}}))
    library.replace_tags(uid(3), tagset({"retro"}))
    hits = library.search("meeting", tags=["retro"], cursor=None, limit=10).items
    assert sorted(ids(hits)) == [uid(201), uid(300)]  # not page 200: untagged page
    for hit in hits:
        assert isinstance(hit, SearchHit) and "retro" in hit.tags
    assert library.search("meeting", tags=["other"], cursor=None, limit=10).items == []
    either = library.search("meeting", tags=["other", "retro"], cursor=None, limit=10).items
    assert sorted(ids(either)) == [uid(201), uid(300)]


def test_tag_filter_document_hits_use_every_tag_of_the_document(library: Index) -> None:
    library.replace_tags(uid(4), tagset({"draft"}, {uid(400): {"page-tag"}}))
    library.replace_tags(uid(5), tagset(set(), {uid(500): {"draft"}}))
    (hit,) = library.search("sketches", tags=["draft"], cursor=None, limit=10).items
    assert isinstance(hit, DocumentHit) and hit.document == uid(4)
    assert hit.tags == ("draft",)  # a document hit carries the document's own tags
    # An untranscribed page has no other way to be found: its page tags count too.
    (loose,) = library.search("loose", tags=["draft"], cursor=None, limit=10).items
    assert isinstance(loose, DocumentHit) and loose.document == uid(5)
    found = library.search("sketches", tags=["page-tag"], cursor=None, limit=10).items
    assert ids(found) == [uid(4)]
    # Untranscribed documents are found by their tag names, case-insensitively.
    by_tag = library.search("DRAFT", cursor=None, limit=10).items
    assert ids(by_tag) == [uid(5), uid(4)]  # Loose, Sketches: by name
    assert all(isinstance(h, DocumentHit) for h in by_tag)
    assert ids(library.search("page-tag", cursor=None, limit=10).items) == [uid(4)]


def test_a_page_tag_on_an_untranscribed_notebook_is_findable(index: Index) -> None:
    """The notebook's only "harbour" tag is on a page with no transcript yet."""
    index.upsert_document(doc(1, "Sketchbook"))
    index.upsert_document(doc(2, "Other"))
    pages_of(index, 1, 2)
    pages_of(index, 2, 1)
    index.replace_tags(uid(1), tagset(set(), {uid(100): {"harbour"}}))

    assert ids(index.search("harbour", cursor=None, limit=5).items) == [uid(1)]
    found = index.search("sketchbook", tags=["harbour"], cursor=None, limit=5).items
    assert ids(found) == [uid(1)]
    assert ids(index.search("other", tags=["harbour"], cursor=None, limit=5).items) == []
    assert listed(index, None, ["harbour"]) == ["Sketchbook"]
    (row,) = index.children(
        None, include_trash=False, include_deleted=False, tags=["harbour"], cursor=None, limit=5
    ).items
    assert row.tags == ()  # list shows the document's own tags


def test_page_tag_matches_keep_the_cursors_stable(index: Index) -> None:
    """Several page tags of one document match once: the predicate is per document."""
    for n in range(1, 8):
        index.upsert_document(doc(n, f"Book {n % 3}"))
        pages_of(index, n, 3)
        own = {"harbour"} if n % 2 else set()
        index.replace_tags(uid(n), tagset(own, {uid(n * 100 + i): {"harbour"} for i in range(3)}))
    full = ids(index.search("harbour", cursor=None, limit=100).items)
    assert sorted(full) == [uid(n) for n in range(1, 8)]
    for limit in (1, 2, 3):
        walked: list[str] = []
        cursor = None
        while True:
            page = index.search("harbour", tags=["harbour"], cursor=cursor, limit=limit)
            walked.extend(ids(page.items))
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert walked == full
    everything = listed(index, None, ["harbour"], limit=100)
    assert len(everything) == 7
    for limit in (1, 2, 3):
        assert listed(index, None, ["harbour"], limit=limit) == everything


def test_folder_filter_is_the_subtree(library: Index) -> None:
    library.upsert_document(folder(6, "Inner", parent=uid(1)))
    library.upsert_document(doc(7, "Nested", parent=uid(6)))
    pages_of(library, 7, 1)
    library.put_transcript(uid(7), uid(700), "h7-0", "meeting deep down", "m", "lazy")
    everywhere = ids(library.search("meeting", cursor=None, limit=10).items)
    assert sorted(everywhere) == [uid(200), uid(201), uid(300), uid(700)]
    below = ids(library.search("meeting", folder=uid(1), cursor=None, limit=10).items)
    assert sorted(below) == [uid(200), uid(201), uid(700)]
    inner = ids(library.search("meeting", folder=uid(6), cursor=None, limit=10).items)
    assert inner == [uid(700)]
    names = library.search("s", folder=uid(1), cursor=None, limit=10).items
    assert [h.document for h in names if isinstance(h, DocumentHit)] == [uid(4)]
    # A folder is a subtree of UUIDs, not a path prefix: a same-named folder elsewhere is out.
    library.upsert_document(folder(8, "Projects"))
    assert library.search("meeting", folder=uid(8), cursor=None, limit=10).items == []


def test_filters_combine_and_paginate(library: Index) -> None:
    library.replace_tags(uid(2), tagset({"t"}))
    library.replace_tags(uid(4), tagset({"t"}))
    library.replace_tags(uid(3), tagset({"t"}))
    seen: list[str] = []
    cursor = None
    while True:
        page = library.search("meeting", folder=uid(1), tags=["t"], cursor=cursor, limit=1)
        seen.extend(ids(page.items))
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert sorted(seen) == [uid(200), uid(201)]
    seen = []
    cursor = None
    while True:
        page = library.search("t", folder=uid(1), tags=["t"], cursor=cursor, limit=1)
        seen.extend(ids(page.items))
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert sorted(seen) == sorted([uid(200), uid(201), uid(4)])


def test_search_rejects_bad_tag_filters(library: Index) -> None:
    with pytest.raises(InvalidArgumentError):
        library.search("meeting", tags=["\x1b[31m"], cursor=None, limit=5)


# ---- migration --------------------------------------------------------------------------------


def test_v4_keeps_v3_transcripts_searchable(tmp_path: Path) -> None:
    """A deployed V3 index gains tags and a rebuilt FTS table with every transcript in it."""
    path = tmp_path / "index.sqlite"
    conn = open_sqlite(path)
    migrate(conn, ACCOUNT_STEPS[:3])
    with conn:
        conn.execute(
            "INSERT INTO documents(uuid, type, parent, name, last_modified, page_count) "
            "VALUES (?, 'DocumentType', NULL, 'Ledger', '2026-10-01T12:00:00.000000Z', 1)",
            (uid(1),),
        )
        conn.execute(
            "INSERT INTO pages(uuid, document, idx, content_hash) VALUES (?, ?, 0, 'h')",
            (uid(100), uid(1)),
        )
        conn.execute(
            "INSERT INTO transcripts(id, page, content_hash, document, text, model, mode, "
            "created_at, doc_name, doc_path) VALUES (41, ?, 'h', ?, 'harvest moon', 'm', "
            "'lazy', '2026-10-01T12:00:00.000000Z', 'Ledger', '')",
            (uid(100), uid(1)),
        )
    conn.close()
    with Index.open(path, migrate=True) as ix:
        assert ix.version() == len(ACCOUNT_STEPS)
        (hit,) = ix.search("harvest", cursor=None, limit=5).items
        assert isinstance(hit, SearchHit) and hit.tags == ()
        (by_name,) = ix.search("ledger", cursor=None, limit=5).items
        assert isinstance(by_name, SearchHit)
        ix.replace_tags(uid(1), tagset({"autumn"}))
        (tagged,) = ix.search("autumn", cursor=None, limit=5).items
        assert isinstance(tagged, SearchHit) and tagged.tags == ("autumn",)
        row = ix._conn.execute("SELECT id, doc_tags FROM transcripts").fetchone()
        assert (row["id"], row["doc_tags"]) == (41, "autumn")
        fts_ok(ix)


# ---- the shared fake keeps in step ----------------------------------------------------------


def test_the_fake_index_agrees_on_tags(index: Index) -> None:
    fake = FakeIndex()
    index.upsert_document(doc(1, "A"))
    index.upsert_document(doc(2, "B"))
    pages_of(index, 2, 1)
    writes = [
        (uid(1), tagset({"keep", "drop"})),
        (uid(2), tagset({"other"}, {uid(200): {"keep"}})),
        (uid(1), tagset({"keep"})),
    ]
    for document, tags in writes:
        index.replace_tags(document, tags)
        fake.replace_tags(document, tags)
    for document in (uid(1), uid(2), uid(3)):
        assert fake.tags_of(document) == index.tags_of(document)
    for names in (["keep"], ["drop"], [" other "], ["keep", "other"], []):
        assert fake.documents_tagged(names) == index.documents_tagged(names)
    with pytest.raises(InvalidArgumentError):
        fake.replace_tags(uid(1), tagset({" padded "}))
