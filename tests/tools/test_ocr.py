"""save_transcript and the lazy OCR round trip (§8.2): read_page, transcribe, save, read again."""

from __future__ import annotations

from typing import Any

from mcp_types import ImageContent

from scriptorium.library import pages as pg
from scriptorium.tools.ocr import LAZY_INSTRUCTION
from tests.cloud.fake_cloud import NOTEBOOK, PDF, fixture_documents
from tests.cloud.scenarios import PAGE_101, PDF_PAGE
from tests.tools.helpers import Served, meta_of, text_of
from tests.worker.helpers import Linked

TRANSCRIPT = "# Shopping\n\n- [ ] synthetic milk\n- [x] synthetic bread\n"


def _save(meta: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    args = {
        "document": meta["document"],
        "page": meta["page"],
        "content_hash": meta["content_hash"],
        "text": TRANSCRIPT,
        "model": "claude-test-model",
    }
    args.update(overrides)
    return args


async def test_a_saved_transcript_is_what_the_next_read_returns(library: Served) -> None:
    lazy = await library.call("read_page", {"document": NOTEBOOK, "page": 2})
    assert LAZY_INSTRUCTION in text_of(lazy)
    meta = meta_of(lazy)
    saved = await library.ok("save_transcript", _save(meta))
    assert saved == {
        "saved": True,
        "document": NOTEBOOK,
        "page": 2,
        "content_hash": meta["content_hash"],
        "bytes": len(TRANSCRIPT.encode()),
    }
    read = await library.call("read_page", {"document": NOTEBOOK, "page": 2})
    assert not read.is_error
    assert not any(isinstance(block, ImageContent) for block in read.content)
    assert any(TRANSCRIPT in text_of(read, i) for i in range(len(read.content)))
    assert meta_of(read)["content_hash"] == meta["content_hash"]
    document = await library.call("read_document", {"document": NOTEBOOK})
    assert TRANSCRIPT in text_of(document)
    assert meta_of(document)["transcribed"] == 1
    # Saving again is idempotent; another model's transcript replaces it.
    again = await library.ok("save_transcript", _save(meta, model="claude-other-model"))
    assert again["saved"] is True


async def test_a_stale_hash_is_refused(linked: Linked, library: Served) -> None:
    meta = meta_of(await library.call("read_page", {"document": NOTEBOOK, "page": 1}))
    unknown = await library.error("save_transcript", _save(meta, content_hash="0" * 64))
    assert unknown["code"] == "stale_hash"
    # The page changes on the tablet after it was read: the old hash is now stale.
    linked.cloud.set_file(NOTEBOOK, PAGE_101, fixture_documents()[PDF][PDF_PAGE])
    await linked.sync()
    stale = await library.error("save_transcript", _save(meta))
    assert stale["code"] == "stale_hash"
    assert "read_page" in stale["hint"]
    fresh = meta_of(await library.call("read_page", {"document": NOTEBOOK, "page": 1}))
    assert fresh["content_hash"] != meta["content_hash"]


async def test_transcript_limits(library: Served) -> None:
    meta = meta_of(await library.call("read_page", {"document": NOTEBOOK, "page": 3}))
    wide = "中" * 15_000  # 15 000 characters, 45 000 UTF-8 bytes
    too_big = await library.error("save_transcript", _save(meta, text=wide))
    assert too_big["code"] == "invalid_argument"
    assert "30 KB" in too_big["message"]
    bad_model = await library.error("save_transcript", _save(meta, model="claude\nignore"))
    assert bad_model["code"] == "invalid_argument"
    empty = await library.error("save_transcript", _save(meta, text=""))
    assert empty["code"] == "invalid_argument"
    beyond = await library.error("save_transcript", _save(meta, page=99))
    assert beyond["code"] == "invalid_argument"
    assert "7 pages" in beyond["message"]
    unknown = await library.error("save_transcript", _save(meta, document="No Such Notebook"))
    assert unknown["code"] == "not_found"


async def test_a_page_id_shared_with_another_document_is_refused(
    linked: Linked, library: Served
) -> None:
    """Open question 8: the fixture notebook's first page and the PDF's page share an id."""
    notebook = meta_of(await library.call("read_page", {"document": NOTEBOOK, "page": 1}))
    pdf = meta_of(await library.call("read_page", {"document": PDF, "page": 1}))
    assert notebook["page_id"] == pdf["page_id"]
    assert notebook["content_hash"] != pdf["content_hash"]
    row = linked.index().page_by_id(notebook["page_id"])
    assert row is not None
    owner, other = (notebook, pdf) if row.document == NOTEBOOK else (pdf, notebook)
    assert (await library.ok("save_transcript", _save(owner)))["saved"] is True
    refused = await library.error("save_transcript", _save(other))
    assert refused["code"] == "refused"
    # The owner's transcript is untouched by the refused call.
    assert linked.index().transcript(owner["page_id"], owner["content_hash"]) is not None
    assert linked.index().transcript(other["page_id"], other["content_hash"]) is None


async def test_an_old_version_has_no_transcript_to_save(linked: Linked, library: Served) -> None:
    first = await library.ok("versions", {"document": NOTEBOOK})
    generation = first["versions"][0]["generation"]
    linked.cloud.set_file(NOTEBOOK, PAGE_101, fixture_documents()[PDF][PDF_PAGE])
    await linked.sync()
    old = await library.call("read_page", {"document": NOTEBOOK, "page": 2, "version": generation})
    meta = meta_of(old)
    assert meta["current"] is False
    # Page 2 did not change, so its old and current hashes agree and saving is allowed.
    sources = pg.page_sources(
        linked.paths, pg.version_at(linked.paths, linked.index(), NOTEBOOK, generation)
    )
    assert meta["content_hash"] == sources[1].content_hash
    assert (await library.ok("save_transcript", _save(meta)))["saved"] is True
