"""search and list(tags=) over a synced library (Architecture §11.2; M2 check 5).

Tags are added to the S3 fixture notebook's ``.content`` in the fake cloud and synced by the
real worker; transcripts and tag names are synthetic.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

import pytest

from scriptorium.library.index import DocumentRow, PageRow
from scriptorium.library.tags import TagSet
from scriptorium.logging import JsonFormatter
from tests.cloud.fake_cloud import NOTEBOOK, PAGES, PDF, fixture_documents, renamed
from tests.tools.helpers import Served, meta_of
from tests.worker.helpers import Linked

FOLDER = "00000000-0000-4000-8000-0000000000a1"
INSIDE = "00000000-0000-4000-8000-0000000000a2"
TAGGED = "00000000-0000-4000-8000-0000000000a3"
D1 = "00000000-0000-4000-8000-0000000000a4"
F2 = "00000000-0000-4000-8000-0000000000a5"
D2 = "00000000-0000-4000-8000-0000000000a6"
QUERY_MARKER = "querymarker4b7e"
TAG_MARKER = "tag-marker-c81d"
SEARCH_BOUND_S = 0.5
"""M2 check 5: search returns the page with a snippet in under 500 ms."""


def _metadata(name: str, *, parent: str = "", kind: str = "DocumentType") -> bytes:
    return json.dumps(
        {"lastModified": "1792230476439", "parent": parent, "type": kind, "visibleName": name}
    ).encode()


def _content(**extra: Any) -> bytes:
    content = json.loads(fixture_documents()[NOTEBOOK][f"{NOTEBOOK}.content"])
    content.update(extra)
    return json.dumps(content).encode()


async def _tag_library(linked: Linked) -> None:
    """The notebook tagged ``review`` with page 3 tagged ``retro``; a folder ``Archive``
    tagged ``shelf`` holding a copy of the notebook, ``Inside``, tagged ``review``."""
    linked.cloud.set_file(
        NOTEBOOK,
        f"{NOTEBOOK}.content",
        _content(tags=[{"name": "review"}], pageTags=[{"name": "retro", "pageId": PAGES[2]}]),
    )
    inside = renamed(fixture_documents()[NOTEBOOK], NOTEBOOK, INSIDE)
    inside[f"{INSIDE}.metadata"] = _metadata("Inside", parent=FOLDER)
    inside[f"{INSIDE}.content"] = _content(tags=[{"name": "review"}])
    linked.cloud.put_documents(
        {
            FOLDER: {
                f"{FOLDER}.metadata": _metadata("Archive", kind="CollectionType"),
                f"{FOLDER}.content": json.dumps({"tags": [{"name": "shelf"}]}).encode(),
            },
            INSIDE: inside,
        }
    )
    await linked.sync()


async def _transcribe(library: Served, document: str, page: int, text: str) -> None:
    read = await library.call("read_page", {"document": document, "page": page})
    meta = meta_of(read)
    saved = await library.ok(
        "save_transcript",
        {
            "document": document,
            "page": page,
            "content_hash": meta["content_hash"],
            "text": text,
            "model": "claude-test-model",
        },
    )
    assert saved["saved"] is True


# ---- search ---------------------------------------------------------------------------------


async def test_search_returns_the_page_with_a_snippet(library: Served) -> None:
    await _transcribe(library, NOTEBOOK, 2, "Notes from the sprint retro: keep the demo short")
    payload = await library.ok("search", {"query": "retro"})
    assert payload["next_cursor"] is None
    (hit,) = payload["items"]
    assert hit["kind"] == "page"
    assert hit["document"] == NOTEBOOK and hit["page"] == 2 and hit["page_id"] == PAGES[1]
    assert hit["current"] is True
    assert "retro" in hit["snippet"]
    assert hit["tags"] == [] and hit["name"] == "Scriptorium Test Notebook"
    assert hit["path"] == ""
    # The page ref is enough to come back for the full text.
    read = await library.call("read_page", {"document": hit["document"], "page": hit["page"]})
    assert meta_of(read)["content_hash"] == hit["content_hash"]


async def test_untranscribed_documents_are_found_by_name(library: Served) -> None:
    payload = await library.ok("search", {"query": "test pdf"})
    assert payload["items"] == [
        {
            "kind": "document",
            "document": PDF,
            "name": "Scriptorium Test PDF",
            "path": "",
            "tags": [],
            "folder_tags": [],
        }
    ]


async def test_an_edited_page_marks_its_old_transcript_not_current(
    library: Served, linked: Linked
) -> None:
    await _transcribe(library, NOTEBOOK, 1, "lighthouse keeper")
    linked.cloud.set_file(
        NOTEBOOK,
        f"{NOTEBOOK}/{PAGES[0]}.rm",
        fixture_documents()[NOTEBOOK][f"{NOTEBOOK}/{PAGES[3]}.rm"],
    )
    await linked.sync()
    (hit,) = (await library.ok("search", {"query": "lighthouse"}))["items"]
    assert hit["current"] is False and hit["page"] == 1


async def test_search_filters_by_tag_and_folder(library: Served, linked: Linked) -> None:
    await _tag_library(linked)
    await _transcribe(library, NOTEBOOK, 2, "meeting about plans")
    await _transcribe(library, NOTEBOOK, 3, "meeting retrospective")
    await _transcribe(library, INSIDE, 1, "meeting archived")

    everything = await library.ok("search", {"query": "meeting"})
    assert len(everything["items"]) == 3
    # Page 3 carries its own tag and the document's.
    page3 = next(i for i in everything["items"] if i.get("page") == 3)
    assert page3["tags"] == ["retro", "review"] and page3["folder_tags"] == []

    retro = await library.ok("search", {"query": "meeting", "tags": ["retro"]})
    assert [(i["document"], i["page"]) for i in retro["items"]] == [(NOTEBOOK, 3)]
    review = await library.ok("search", {"query": "meeting", "tags": ["review"]})
    assert len(review["items"]) == 3  # document tags cover every page
    below = await library.ok("search", {"query": "meeting", "folder": "Archive"})
    assert below["folder"] == FOLDER
    assert [(i["document"], i["page"]) for i in below["items"]] == [(INSIDE, 1)]
    # A folder's tag covers what is in it (the live M2 acceptance case).
    (shelved,) = (await library.ok("search", {"query": "meeting", "tags": ["shelf"]}))["items"]
    assert (shelved["document"], shelved["page"]) == (INSIDE, 1)
    assert shelved["tags"] == ["review"] and shelved["folder_tags"] == ["shelf"]

    # Tag names are searchable as text; an untranscribed document is found by its own tags.
    by_tag = await library.ok("search", {"query": "retro"})
    assert [(i["document"], i["page"]) for i in by_tag["items"]] == [(NOTEBOOK, 3)]
    pdf_free = await library.ok("search", {"query": "shelf"})
    assert pdf_free["items"] == []  # a folder is not a document hit


async def test_search_rejects_bad_arguments(library: Served) -> None:
    for args in (
        {"query": "   "},
        {"query": "x", "tags": ["bell\x07"]},
        {"query": "x", "tags": [""]},
        {"query": "x", "tags": [f"t{n}" for n in range(101)]},
        {"query": "x", "folder": NOTEBOOK},
        {"query": "x", "cursor": "nonsense"},
        {"query": "x", "path": "/etc"},
    ):
        assert (await library.error("search", args))["code"] == "invalid_argument", args
    assert (await library.error("search", {"query": "x", "folder": "Nowhere"}))["code"] == (
        "not_found"
    )


async def test_search_operators_are_literal(library: Served) -> None:
    await _transcribe(library, NOTEBOOK, 1, 'say "NEAR" OR not')
    assert len((await library.ok("search", {"query": '"NEAR" OR ('}))["items"]) == 1
    assert (await library.ok("search", {"query": "NOT AND"}))["items"] == []


async def test_search_audits_no_query_or_tag_names(
    library: Served, linked: Linked, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await _tag_library(linked)
    linked.cloud.set_file(NOTEBOOK, f"{NOTEBOOK}.content", _content(tags=[{"name": TAG_MARKER}]))
    await linked.sync()
    await library.ok("search", {"query": QUERY_MARKER, "tags": [TAG_MARKER], "folder": FOLDER})
    await library.ok("list", {"tags": [TAG_MARKER]})
    stored = json.dumps([list(row) for row in library.audit_rows()], default=str)
    formatter = JsonFormatter()
    logged = "\n".join(
        formatter.format(r)
        for r in caplog.records
        if r.name.startswith("scriptorium") or r.levelno >= logging.WARNING
    )
    for marker in (QUERY_MARKER, TAG_MARKER, "review", "retro", "shelf"):
        assert marker not in stored, marker
        assert marker not in logged, marker


# ---- list(tags=) ----------------------------------------------------------------------------


async def test_list_shows_tags_and_filters_recursively(library: Served, linked: Linked) -> None:
    await _tag_library(linked)
    root = await library.ok("list")
    tags = {item["uuid"]: item["tags"] for item in root["items"]}
    assert tags == {FOLDER: ["shelf"], NOTEBOOK: ["review"], PDF: []}
    assert all(item["folder_tags"] == [] for item in root["items"])
    assert root["recursive"] is False
    review = await library.ok("list", {"tags": ["review"]})
    assert review["recursive"] is True
    assert {item["uuid"] for item in review["items"]} == {NOTEBOOK, INSIDE}
    either = await library.ok("list", {"tags": ["shelf", "review"]})
    assert either["items"][0]["type"] == "folder"  # folders first
    assert {item["uuid"] for item in either["items"]} == {FOLDER, NOTEBOOK, INSIDE}
    inside = await library.ok("list", {"folder": "Archive", "tags": ["review"]})
    assert [item["uuid"] for item in inside["items"]] == [INSIDE]
    # A page tag finds its document (the item shows the document's own tags).
    retro = (await library.ok("list", {"tags": ["retro"]}))["items"]
    assert [(item["uuid"], item["tags"]) for item in retro] == [(NOTEBOOK, ["review"])]
    # A folder's tag covers what is in it.
    shelf = (await library.ok("list", {"tags": ["shelf"]}))["items"]
    assert [(i["uuid"], i["tags"], i["folder_tags"]) for i in shelf] == [
        (FOLDER, ["shelf"], []),
        (INSIDE, ["review"], ["shelf"]),
    ]
    assert (await library.error("list", {"tags": ["\x00"]}))["code"] == "invalid_argument"


async def _nest_library(linked: Linked) -> None:
    """``Tagged`` (folder, tagged ``team``) > ``D1`` (tagged ``own``), ``F2`` (folder) > ``D2``:
    the shape of the live M2 acceptance run, one level deeper."""

    def notebook(uuid: str, name: str, parent: str, **content: Any) -> dict[str, bytes]:
        files = renamed(fixture_documents()[NOTEBOOK], NOTEBOOK, uuid)
        files[f"{uuid}.metadata"] = _metadata(name, parent=parent)
        files[f"{uuid}.content"] = _content(**content)
        return files

    def folder(name: str, parent: str, tags: list[str]) -> dict[str, bytes]:
        return {
            "metadata": _metadata(name, parent=parent, kind="CollectionType"),
            "content": json.dumps({"tags": [{"name": t} for t in tags]}).encode(),
        }

    tagged, f2 = folder("Tagged", "", ["team"]), folder("F2", TAGGED, [])
    linked.cloud.put_documents(
        {
            TAGGED: {
                f"{TAGGED}.metadata": tagged["metadata"],
                f"{TAGGED}.content": tagged["content"],
            },
            F2: {f"{F2}.metadata": f2["metadata"], f"{F2}.content": f2["content"]},
            D1: notebook(D1, "First", TAGGED, tags=[{"name": "own"}]),
            D2: notebook(D2, "Second", F2),
        }
    )
    await linked.sync()


async def test_list_and_search_inherit_folder_tags(library: Served, linked: Linked) -> None:
    await _nest_library(linked)
    team = (await library.ok("list", {"tags": ["team"]}))["items"]
    shape = [(i["uuid"], i["type"], i["tags"], i["folder_tags"]) for i in team]
    assert shape == [
        (F2, "folder", [], ["team"]),
        (TAGGED, "folder", ["team"], []),
        (D1, "document", ["own"], ["team"]),  # untranscribed: found all the same
        (D2, "document", [], ["team"]),
    ]
    inside = (await library.ok("list", {"folder": "Tagged/F2", "tags": ["team"]}))["items"]
    assert [i["uuid"] for i in inside] == [D2]

    await _transcribe(library, D2, 1, "quarterly budget")
    (hit,) = (await library.ok("search", {"query": "budget", "tags": ["team"]}))["items"]
    assert (hit["kind"], hit["document"], hit["page"]) == ("page", D2, 1)
    assert hit["tags"] == [] and hit["folder_tags"] == ["team"]
    assert hit["path"] == "Tagged/F2"
    (first,) = (await library.ok("search", {"query": "first", "tags": ["team"]}))["items"]
    assert first["kind"] == "document" and first["document"] == D1
    assert first["tags"] == ["own"] and first["folder_tags"] == ["team"]

    # Untagging the folder on the tablet takes the inherited tag away.
    linked.cloud.set_file(TAGGED, f"{TAGGED}.content", json.dumps({"tags": []}).encode())
    await linked.sync()
    assert (await library.ok("list", {"tags": ["team"]}))["items"] == []
    assert (await library.ok("search", {"query": "budget", "tags": ["team"]}))["items"] == []


# ---- check 5: latency over a few thousand transcripts -----------------------------------------

DOCS = 300
PAGES_PER_DOC = 10


def _populate(linked: Linked) -> int:
    """3000 synthetic transcripts in 300 documents under 10 folders; about 2 % mention the
    query word, every 10th document is tagged, and so is folder F3."""
    rng = random.Random(5)
    vocabulary = [f"w{n}" + "".join(rng.choices("aeioulnrst", k=4)) for n in range(2000)]
    index = linked.index()
    with index.transaction():
        for f in range(10):
            index.upsert_document(
                DocumentRow(
                    f"00000000-0000-4000-8001-{f:012x}",
                    "CollectionType",
                    None,
                    f"F{f}",
                    linked.clock(),
                )
            )
            if f == 3:
                index.replace_tags(
                    f"00000000-0000-4000-8001-{f:012x}", TagSet(frozenset({"benchfolder"}))
                )
        for d in range(DOCS):
            uuid = f"00000000-0000-4000-8002-{d:012x}"
            parent = f"00000000-0000-4000-8001-{d % 10:012x}"
            index.upsert_document(
                DocumentRow(
                    uuid,
                    "DocumentType",
                    parent,
                    f"Notebook {d}",
                    linked.clock(),
                    page_count=PAGES_PER_DOC,
                )
            )
            pages = [
                PageRow(
                    f"00000000-0000-4000-8003-{d * PAGES_PER_DOC + p:012x}", uuid, p, f"{d}-{p}"
                )
                for p in range(PAGES_PER_DOC)
            ]
            index.replace_pages(uuid, pages)
            if d % 10 == 0:
                index.replace_tags(uuid, TagSet(frozenset({"bench"})))
            for page in pages:
                words = rng.choices(vocabulary, k=180)
                if rng.random() < 0.02:
                    words[rng.randrange(len(words))] = "retro"
                index.put_transcript(
                    uuid, page.page_id, page.content_hash, " ".join(words), "m", "lazy"
                )
    return DOCS * PAGES_PER_DOC


async def test_search_answers_within_500_ms_over_thousands_of_transcripts(
    library: Served, linked: Linked
) -> None:
    assert _populate(linked) == 3000
    calls: list[tuple[str, dict[str, Any]]] = [
        ("retro", {}),
        ("retro", {"tags": ["bench"]}),
        ("retro", {"tags": ["benchfolder"]}),
        ("notebook", {"tags": ["benchfolder"]}),
        ("retro", {"folder": "F3"}),
        ("notebook", {}),
    ]
    timings: list[float] = []
    for _ in range(5):
        for query, extra in calls:
            started = time.perf_counter()
            payload = await library.ok("search", {"query": query, **extra})
            timings.append(time.perf_counter() - started)
            assert payload["items"], (query, extra)
    worst = max(timings)
    assert worst < SEARCH_BOUND_S, f"slowest search {worst * 1000:.0f} ms"
    hits = (await library.ok("search", {"query": "retro"}))["items"]
    assert all("retro" in hit["snippet"] for hit in hits)
