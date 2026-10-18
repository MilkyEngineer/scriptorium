"""Page lists, content hashes, versions and page PNG paths (library/pages.py)."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from scriptorium.errors import InvalidArgumentError, NotFoundError, PathEscapeError
from scriptorium.library import pages as pg
from scriptorium.library.archive import Archive
from scriptorium.library.index import HistoryRow
from tests.cloud.fake_cloud import NOTEBOOK, PAGES, PDF, FakeCloud, fixture_documents
from tests.cloud.scenarios import PAGE_101, Harness, harness

U = [f"00000000-0000-4000-8000-0000000009{n:02d}" for n in range(10)]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def synced(tmp_path: Path) -> Iterator[tuple[FakeCloud, Harness]]:
    cloud = FakeCloud()
    cloud.put_documents(fixture_documents())
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    assert h.poll().outcome == "ok"
    yield cloud, h
    h.close()


# ---- page lists ------------------------------------------------------------------------------


def test_v2_page_list_orders_by_idx_and_skips_what_it_cannot_use() -> None:
    content = {
        "cPages": {
            "pages": [
                {"id": U[0], "idx": {"value": "bb"}, "redir": {"value": 3}},
                {"id": U[1], "idx": {"value": "ba"}},
                {"id": U[2], "idx": {"value": "a"}, "deleted": {"value": 1}},
                {"id": "../../etc", "idx": {"value": "a"}},
                "not a page",
                {"id": U[3]},  # no idx sorts first
                {"id": U[1], "idx": {"value": "c"}},  # a repeated id keeps its first place
                {"id": U[4], "idx": {"value": "bb"}, "redir": {"value": -1}},
            ]
        }
    }
    assert pg.page_list(content, is_pdf=False) == [
        pg.PageEntry(U[3], None),
        pg.PageEntry(U[1], None),
        pg.PageEntry(U[0], None),
        pg.PageEntry(U[4], None),
    ]
    assert pg.page_list(content, is_pdf=True) == [
        pg.PageEntry(U[3], None),
        pg.PageEntry(U[1], None),
        pg.PageEntry(U[0], 3),
        pg.PageEntry(U[4], None),  # negative redirection: an inserted blank page
    ]


def test_v1_page_list_uses_the_redirection_map() -> None:
    content = {"pages": [U[0], U[1], "x", U[2]], "redirectionPageMap": [0, -1, 5, 1]}
    assert pg.page_list(content, is_pdf=True) == [
        pg.PageEntry(U[0], 0),
        pg.PageEntry(U[1], None),
        pg.PageEntry(U[2], 1),
    ]
    # A PDF without a map is one page per PDF page.
    assert pg.page_list({"pages": [U[0], U[1]]}, is_pdf=True) == [
        pg.PageEntry(U[0], 0),
        pg.PageEntry(U[1], 1),
    ]
    assert pg.page_list({"pages": "nonsense"}, is_pdf=False) == []
    assert pg.page_list({}, is_pdf=False) == []


# ---- hashes and sources from a synced archive ------------------------------------------------


def test_content_hash_is_the_rm_hash_for_ink_on_paper() -> None:
    rm, pdf = "a" * 64, "b" * 64
    assert pg.content_hash(rm, None, None) == rm
    on_pdf = pg.content_hash(rm, pdf, 0)
    assert on_pdf not in (rm, pdf)
    assert pg.content_hash(rm, pdf, 1) != on_pdf
    assert pg.content_hash(None, pdf, 0) != on_pdf
    assert pg.content_hash(None, None, None) == pg.content_hash(None, None, None)
    assert len(pg.content_hash(None, None, None)) == 64


def test_page_sources_of_the_synced_fixture(synced: tuple[FakeCloud, Harness]) -> None:
    _cloud, h = synced
    docs = fixture_documents()
    notebook = pg.current_version(h.paths, h.index, NOTEBOOK)
    assert notebook is not None
    sources = pg.page_sources(h.paths, notebook)
    assert [s.page_id for s in sources] == list(PAGES)
    assert [s.idx for s in sources] == list(range(7))
    for source in sources:
        rm = docs[NOTEBOOK][f"{NOTEBOOK}/{source.page_id}.rm"]
        assert (source.rm_sha, source.pdf_sha, source.pdf_page) == (sha(rm), None, None)
        assert source.content_hash == sha(rm)

    pdf = pg.current_version(h.paths, h.index, PDF)
    assert pdf is not None
    (page,) = pg.page_sources(h.paths, pdf)
    pdf_sha = sha(docs[PDF][f"{PDF}.pdf"])
    rm_sha = sha(docs[PDF][f"{PDF}/{PAGES[0]}.rm"])
    assert (page.page_id, page.rm_sha, page.pdf_sha, page.pdf_page) == (
        PAGES[0],
        rm_sha,
        pdf_sha,
        0,
    )
    assert page.content_hash == pg.content_hash(rm_sha, pdf_sha, 0)
    assert pg.current_version(h.paths, h.index, U[0]) is None


def test_an_old_version_reads_like_the_current_one(synced: tuple[FakeCloud, Harness]) -> None:
    cloud, h = synced
    before = pg.current_version(h.paths, h.index, NOTEBOOK)
    assert before is not None
    new_rm = fixture_documents()[NOTEBOOK][f"{NOTEBOOK}/{PAGES[1]}.rm"]
    cloud.set_file(NOTEBOOK, PAGE_101, new_rm)
    assert h.poll().outcome == "ok"
    after = pg.current_version(h.paths, h.index, NOTEBOOK)
    assert after is not None and after.generation > before.generation
    assert pg.page_sources(h.paths, after)[0].content_hash == sha(new_rm)
    old = pg.version_at(h.paths, h.index, NOTEBOOK, before.generation)
    assert old == before
    assert pg.page_sources(h.paths, old)[0].content_hash != sha(new_rm)
    with pytest.raises(NotFoundError):
        pg.version_at(h.paths, h.index, NOTEBOOK, 999_999)


def test_a_version_without_its_index_blob_has_no_page_list(
    synced: tuple[FakeCloud, Harness],
) -> None:
    _cloud, h = synced
    row = h.index.history(NOTEBOOK)[0]
    with pytest.raises(NotFoundError):
        pg.load_version(h.paths, HistoryRow(row.uuid, row.generation, row.hash_root, row.seen_at))
    with pytest.raises(NotFoundError):
        pg.read_blob(h.paths, "../" + "a" * 61, limit=10)
    with pytest.raises(NotFoundError):
        pg.read_blob(h.paths, "c" * 64, limit=10)
    assert row.index_sha256 is not None
    with pytest.raises(InvalidArgumentError):
        pg.read_blob(h.paths, row.index_sha256, limit=10)


def test_unparseable_content_gives_no_pages(synced: tuple[FakeCloud, Harness]) -> None:
    _cloud, h = synced
    current = pg.current_version(h.paths, h.index, NOTEBOOK)
    assert current is not None
    for body in (b"{not json", json.dumps([1, 2]).encode(), b"\xff\xfe"):
        assert isinstance(h.archive, Archive)
        h.archive.put_blob(sha(body), io.BytesIO(body))
        files = {**current.files, f"{NOTEBOOK}.content": sha(body)}
        version = pg.Version(NOTEBOOK, current.generation, current.hash_root, files)
        assert pg.page_sources(h.paths, version) == []
    files = {k: v for k, v in current.files.items() if not k.endswith(".content")}
    version = pg.Version(NOTEBOOK, current.generation, current.hash_root, files)
    assert pg.page_sources(h.paths, version) == []


# ---- PNG files -------------------------------------------------------------------------------


def test_png_paths_name_the_content(synced: tuple[FakeCloud, Harness]) -> None:
    _cloud, h = synced
    digest = "d" * 64
    rel = pg.png_relpath(PAGES[0], digest)
    assert rel == f"pages/{PAGES[0]}/{digest}.png"
    assert (
        pg.png_relpath(PAGES[0], digest, max_side=2048) == f"pages/{PAGES[0]}/{digest}.2048px.png"
    )
    assert pg.resolve_png(h.paths, rel) == h.paths.pages / PAGES[0] / f"{digest}.png"
    for bad in ("../x", "A" * 36):
        with pytest.raises(InvalidArgumentError):
            pg.png_relpath(bad, digest)
    with pytest.raises(InvalidArgumentError):
        pg.png_relpath(PAGES[0], "z" * 64)
    with pytest.raises(InvalidArgumentError):
        pg.png_relpath(PAGES[0], digest, max_side=0)
    for bad in (
        f"pages/{PAGES[0]}/../{digest}.png",
        f"library/{PAGES[0]}/{digest}.png",
        f"pages/{PAGES[0]}/{digest}.png/x",
        f"pages/{PAGES[0]}/{digest}.12x.png",
        f"pages/{PAGES[0]}/{digest}.0012px.png",
        f"pages/{PAGES[0]}/{digest}.jpg",
        "/etc/passwd",
    ):
        with pytest.raises(InvalidArgumentError):
            pg.resolve_png(h.paths, bad)


def test_write_png_is_atomic_and_stays_inside_pages(synced: tuple[FakeCloud, Harness]) -> None:
    _cloud, h = synced
    blank = pg.blank_page_png()
    assert pg.png_size(blank) == (954, 1696)
    assert pg.png_size(pg.blank_page_png(0.5)) == (477, 848)
    rel = pg.png_relpath(PAGES[0], "e" * 64)
    path = pg.write_png(h.paths, rel, blank)
    assert path.read_bytes() == blank
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]  # no temporary left
    pg.write_png(h.paths, rel, blank)  # rewriting the same content is harmless
    with pytest.raises(InvalidArgumentError):
        pg.write_png(h.paths, rel, b"GIF89a")
    with pytest.raises(InvalidArgumentError):
        pg.png_size(b"\x89PNG\r\n\x1a\n")
    # A symlink planted where a page directory belongs is refused, not followed.
    outside = h.paths.root.parent / "elsewhere"
    outside.mkdir()
    (h.paths.pages / PAGES[1]).symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathEscapeError):
        pg.write_png(h.paths, pg.png_relpath(PAGES[1], "e" * 64), blank)
    assert list(outside.iterdir()) == []


def test_the_fixture_page_ids_collide_across_documents(synced: tuple[FakeCloud, Harness]) -> None:
    """Open question 8: the scrubbed fixture reuses page UUID ...0101 in both documents."""
    _cloud, h = synced
    notebook = pg.current_version(h.paths, h.index, NOTEBOOK)
    pdf = pg.current_version(h.paths, h.index, PDF)
    assert notebook is not None and pdf is not None
    ids = {s.page_id for s in pg.page_sources(h.paths, notebook)}
    assert {s.page_id for s in pg.page_sources(h.paths, pdf)} <= ids
