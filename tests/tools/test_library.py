"""list, recent, read_page, read_document, page_image, versions, trash over a synced library."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
from datetime import timedelta
from typing import Any

import httpx
import pytest

from scriptorium.jobs.kinds import RERUN_CAP, render_key
from scriptorium.library import pages as pg
from scriptorium.tools._conventions import ErrorCode, ToolError
from scriptorium.tools._library import fit_png
from scriptorium.tools.library import MODEL_IMAGE_SIDE, PAGE_IMAGE_SIDE, image_side
from scriptorium.tools.ocr import LAZY_INSTRUCTION
from scriptorium.users.store import global_db_path
from tests.cloud.fake_cloud import NOTEBOOK, PAGES, PDF, fixture_documents, renamed
from tests.cloud.scenarios import PAGE_101, PDF_PAGE
from tests.conftest import FakeIssuer
from tests.tools.helpers import Served, image_of, meta_of, serve, text_of
from tests.worker.helpers import Linked

NOTEBOOK_NAME = "Scriptorium Test Notebook"
PDF_NAME = "Scriptorium Test PDF"
FOLDER = "00000000-0000-4000-8000-000000000010"
COPY = "00000000-0000-4000-8000-000000000011"
TWIN = "00000000-0000-4000-8000-000000000012"


def _metadata(name: str, *, parent: str = "", kind: str = "DocumentType") -> bytes:
    """Synthetic metadata in the fixture's shape (no real names or ids)."""
    return json.dumps(
        {
            "deleted": False,
            "lastModified": "1792230476439",
            "createdTime": "1792229694025",
            "metadatamodified": False,
            "modified": False,
            "parent": parent,
            "pinned": False,
            "synced": False,
            "type": kind,
            "version": 0,
            "visibleName": name,
        }
    ).encode()


def _with_metadata(files: dict[str, bytes], uuid: str, metadata: bytes) -> dict[str, bytes]:
    return {**files, f"{uuid}.metadata": metadata}


def _sources(linked: Linked, uuid: str) -> list[pg.PageSource]:
    version = pg.current_version(linked.paths, linked.index(), uuid)
    assert version is not None
    return pg.page_sources(linked.paths, version)


def _archive_png(linked: Linked, source: pg.PageSource) -> bytes:
    rel = pg.png_relpath(source.page_id, source.content_hash)
    return pg.resolve_png(linked.paths, rel).read_bytes()


async def _add_tree(linked: Linked) -> None:
    """A folder "Projects" holding a copy of the notebook, and a same-named twin at the root."""
    notebook = fixture_documents()[NOTEBOOK]
    linked.cloud.put_documents(
        {
            FOLDER: {
                f"{FOLDER}.metadata": _metadata("Projects", kind="CollectionType"),
                f"{FOLDER}.content": b"{}",
            },
            COPY: _with_metadata(
                renamed(notebook, NOTEBOOK, COPY), COPY, _metadata("Plan", parent=FOLDER)
            ),
            TWIN: _with_metadata(renamed(notebook, NOTEBOOK, TWIN), TWIN, _metadata(NOTEBOOK_NAME)),
        }
    )
    await linked.sync()


# ---- list -----------------------------------------------------------------------------------


async def test_list_root(library: Served, linked: Linked) -> None:
    payload = await library.ok("list")
    assert payload["account"] == linked.acct
    assert payload["next_cursor"] is None
    items = {item["uuid"]: item for item in payload["items"]}
    assert set(items) == {NOTEBOOK, PDF}
    notebook = items[NOTEBOOK]
    assert notebook["name"] == NOTEBOOK_NAME
    assert notebook["type"] == "document"
    assert notebook["pages"] == 7
    assert notebook["state"] == "live"
    assert notebook["modified"].endswith("Z")


async def test_list_folder_by_path_and_pagination(library: Served, linked: Linked) -> None:
    await _add_tree(linked)
    first = await library.ok("list", {"limit": 1})
    assert [item["type"] for item in first["items"]] == ["folder"]  # folders first
    rest = await library.ok("list", {"limit": 100, "cursor": first["next_cursor"]})
    names = [item["name"] for item in rest["items"]]
    assert names == sorted(names, key=str.lower)
    assert len(rest["items"]) == 3 and rest["next_cursor"] is None
    inside = await library.ok("list", {"folder": "Projects"})
    assert [item["uuid"] for item in inside["items"]] == [COPY]
    by_uuid = await library.ok("list", {"folder": FOLDER})
    assert by_uuid["items"] == inside["items"]


async def test_list_rejects_a_document_as_folder_and_bad_cursor(library: Served) -> None:
    assert (await library.error("list", {"folder": NOTEBOOK}))["code"] == "invalid_argument"
    assert (await library.error("list", {"cursor": "nonsense"}))["code"] == "invalid_argument"
    assert (await library.error("list", {"folder": "Nowhere"}))["code"] == "not_found"


async def test_ambiguous_path_lists_candidates(library: Served, linked: Linked) -> None:
    await _add_tree(linked)
    error = await library.error("read_page", {"document": NOTEBOOK_NAME, "page": 1})
    assert error["code"] == "ambiguous"
    assert sorted(error["candidates"]) == sorted([NOTEBOOK, TWIN])
    nested = await library.ok("versions", {"document": "Projects/Plan"})
    assert nested["document"] == COPY


async def test_list_shrinks_a_page_that_would_exceed_the_cap(
    library: Served, linked: Linked
) -> None:
    notebook = fixture_documents()[NOTEBOOK]
    wide = "\u4e2d" * 1000  # three UTF-8 bytes each; names are cut to 256 characters
    docs = {}
    for n in range(40):
        uuid = f"00000000-0000-4000-8000-0000000002{n:02d}"
        docs[uuid] = _with_metadata(
            renamed(notebook, NOTEBOOK, uuid), uuid, _metadata(f"{n:02d}{wide}")
        )
    linked.cloud.put_documents(docs)
    await linked.sync()
    result = await library.call("list", {"limit": 100})
    assert len(text_of(result).encode()) <= 32 * 1024
    payload = json.loads(text_of(result))
    assert payload["next_cursor"] is not None
    assert all(len(item["name"]) <= 256 for item in payload["items"])
    seen = len(payload["items"])
    while payload["next_cursor"]:
        payload = await library.ok("list", {"limit": 100, "cursor": payload["next_cursor"]})
        seen += len(payload["items"])
    assert seen == 42


# ---- recent -----------------------------------------------------------------------------------


async def test_recent_reports_changed_pages(library: Served, linked: Linked) -> None:
    before = await library.ok("recent", {"since": "2000-01-01T00:00:00Z"})
    assert {item["uuid"] for item in before["items"]} == {NOTEBOOK, PDF}
    mark = linked.clock.now + timedelta(seconds=1)
    linked.cloud.set_file(NOTEBOOK, PAGE_101, fixture_documents()[PDF][PDF_PAGE])
    await linked.sync()
    since = mark.isoformat().replace("+00:00", "Z")
    after = await library.ok("recent", {"since": "2000-01-01T00:00:00Z"})
    notebook = next(item for item in after["items"] if item["uuid"] == NOTEBOOK)
    assert notebook["changed_page_count"] == 7  # every page is new since 2000
    changed = await library.ok("recent", {"since": since})
    notebook = next(item for item in changed["items"] if item["uuid"] == NOTEBOOK)
    assert notebook["changed_pages"] == [1]
    assert notebook["changed_page_count"] == 1
    assert (await library.error("recent", {"since": "yesterday"}))["code"] == "invalid_argument"


async def test_recent_marks_an_excluded_document(library: Served, linked: Linked) -> None:
    """recent reports the same state as list: excluded, not live."""
    index = linked.index()
    row = index.document(NOTEBOOK)
    assert row is not None
    index.upsert_document(dataclasses.replace(row, excluded_at=linked.clock.now))
    changes = await library.ok("recent", {"since": "2000-01-01T00:00:00Z"})
    states = {item["uuid"]: item["state"] for item in changes["items"]}
    assert states == {NOTEBOOK: "excluded", PDF: "live"}
    listed = await library.ok("list")
    assert {i["uuid"]: i["state"] for i in listed["items"]}[NOTEBOOK] == "excluded"


# ---- read_page --------------------------------------------------------------------------------


async def test_read_page_lazy_returns_image_and_instruction(
    library: Served, linked: Linked
) -> None:
    result = await library.call("read_page", {"document": NOTEBOOK, "page": 2})
    assert not result.is_error
    assert text_of(result).startswith(LAZY_INSTRUCTION)
    meta = meta_of(result)
    assert meta["document"] == NOTEBOOK
    assert meta["page"] == 2 and meta["page_count"] == 7
    assert meta["page_id"] == PAGES[1]
    assert meta["current"] is True and meta["transcript"] is None
    png = image_of(result)
    assert png == fit_png(_archive_png(linked, _sources(linked, NOTEBOOK)[1]), MODEL_IMAGE_SIDE)
    assert max(pg.png_size(png)) <= MODEL_IMAGE_SIDE


async def test_read_page_by_path_and_out_of_range(library: Served) -> None:
    result = await library.call("read_page", {"document": f"/{PDF_NAME}", "page": 1})
    assert meta_of(result)["document"] == PDF
    error = await library.error("read_page", {"document": NOTEBOOK, "page": 8})
    assert error["code"] == "invalid_argument"
    assert "7 pages" in error["message"]
    assert (await library.error("read_page", {"document": NOTEBOOK, "page": 0}))[
        "code"
    ] == "invalid_argument"


async def test_read_page_of_a_folder_is_invalid(library: Served, linked: Linked) -> None:
    await _add_tree(linked)
    error = await library.error("read_page", {"document": "Projects", "page": 1})
    assert error["code"] == "invalid_argument"


async def test_read_page_unknown_uuid(library: Served) -> None:
    error = await library.error(
        "read_page", {"document": "00000000-0000-4000-8000-00000000ffff", "page": 1}
    )
    assert error["code"] == "not_found"


async def test_read_page_waits_for_the_render(
    tmp_path: Any, issuer: FakeIssuer, make_validator: Any
) -> None:
    from tests.worker.helpers import link

    linked = link(tmp_path, settings={"pending_wait_seconds": 0.0})
    try:
        loop = linked.loop()
        await loop.poll_once(linked.context())  # archived and queued, nothing rendered
        async with serve(linked.settings, issuer, make_validator, clock=linked.clock) as library:
            pending = await library.error("read_page", {"document": NOTEBOOK, "page": 1})
            assert pending["code"] == "pending"
            job = library.services.queue.get(pending["job"], linked.uid)
            assert job is not None and job.kind == "render"
            await linked.worker.drain_jobs()
            result = await library.call("read_page", {"document": NOTEBOOK, "page": 1})
            assert not result.is_error
            image_of(result)
    finally:
        linked.close()


async def test_read_page_polls_until_the_worker_renders(
    tmp_path: Any, issuer: FakeIssuer, make_validator: Any
) -> None:
    from tests.worker.helpers import link

    linked = link(tmp_path, settings={"pending_wait_seconds": 30.0})
    try:
        loop = linked.loop()
        await loop.poll_once(linked.context())
        async with serve(linked.settings, issuer, make_validator, clock=linked.clock) as library:

            async def render_soon() -> None:
                await asyncio.sleep(0.2)
                await linked.worker.drain_jobs()

            result, _ = await asyncio.gather(
                library.call("read_page", {"document": NOTEBOOK, "page": 3}), render_soon()
            )
            assert not result.is_error
            image_of(result)
    finally:
        linked.close()


async def test_dead_renders_are_refused_only_for_a_verdict_and_otherwise_rerun(
    tmp_path: Any, issuer: FakeIssuer, make_validator: Any
) -> None:
    from tests.worker.helpers import link

    linked = link(tmp_path, settings={"pending_wait_seconds": 0.0})
    try:
        await linked.loop().poll_once(linked.context())  # archived and queued, not rendered
        sources = _sources(linked, NOTEBOOK)
        verdict_key = render_key(linked.acct, PAGES[1], sources[1].content_hash)
        gone_key = render_key(linked.acct, PAGES[2], sources[2].content_hash)
        broken_key = render_key(linked.acct, PAGES[3], sources[3].content_hash)
        # Found gone while it named another version (a later generation) of the same page.
        moved_key = render_key(linked.acct, PAGES[4], sources[4].content_hash)
        conn = sqlite3.connect(global_db_path(linked.settings.data_dir))
        try:
            with conn:
                conn.execute(
                    "UPDATE jobs SET state = 'dead', error = 'invalid_argument' "
                    "WHERE dedupe_key = ?",
                    (verdict_key,),
                )
                conn.execute(
                    "UPDATE jobs SET state = 'done', result = ? WHERE dedupe_key IN (?, ?)",
                    ('{"state":"gone"}', gone_key, moved_key),
                )
                conn.execute(
                    "UPDATE jobs SET payload = json_set(payload, '$.generation', 999) "
                    "WHERE dedupe_key = ?",
                    (moved_key,),
                )
                # Died of its run (a broken render pool), not of its page.
                conn.execute(
                    "UPDATE jobs SET state = 'dead', error = 'internal' WHERE dedupe_key = ?",
                    (broken_key,),
                )
            ids = {
                key: conn.execute("SELECT id FROM jobs WHERE dedupe_key = ?", (key,)).fetchone()[0]
                for key in (gone_key, broken_key, moved_key)
            }
        finally:
            conn.close()

        def reruns(key: str) -> int:
            conn = sqlite3.connect(global_db_path(linked.settings.data_dir))
            try:
                row = conn.execute(
                    "SELECT count(*) FROM jobs WHERE dedupe_key LIKE ?", (key + ":after:%",)
                ).fetchone()
                return int(row[0])
            finally:
                conn.close()

        async with serve(linked.settings, issuer, make_validator, clock=linked.clock) as library:
            dead = await library.error("read_page", {"document": NOTEBOOK, "page": 2})
            assert dead["code"] == "refused"
            # This very version was found gone: not found, and none of its re-runs spent.
            gone = await library.error("read_page", {"document": NOTEBOOK, "page": 3})
            assert (gone["code"], gone["job"]) == ("not_found", ids[gone_key])
            assert reruns(gone_key) == 0
            for page, key in ((4, broken_key), (5, moved_key)):
                rerun = await library.error("read_page", {"document": NOTEBOOK, "page": page})
                assert rerun["code"] == "pending"
                assert rerun["job"] != ids[key]
                job = library.services.queue.get(rerun["job"], linked.uid)
                assert job is not None and job.dedupe_key == f"{key}:after:{ids[key]}"
                assert job.payload["generation"] != 999  # the version that was asked for
            await linked.worker.drain_jobs()
            image_of(await library.call("read_page", {"document": NOTEBOOK, "page": 4}))
            image_of(await library.call("read_page", {"document": NOTEBOOK, "page": 5}))
            again = await library.error("read_page", {"document": NOTEBOOK, "page": 2})
            assert again["code"] == "refused"  # a verdict is not re-run
    finally:
        linked.close()


async def test_a_page_whose_reruns_are_spent_is_refused_without_a_new_job(
    tmp_path: Any, issuer: FakeIssuer, make_validator: Any
) -> None:
    """A page version that keeps killing its renders is re-run three times for all time."""
    from tests.worker.helpers import link

    linked = link(tmp_path, settings={"pending_wait_seconds": 0.0})
    try:
        await linked.loop().poll_once(linked.context())
        sources = _sources(linked, NOTEBOOK)
        key = render_key(linked.acct, PAGES[4], sources[4].content_hash)
        db = global_db_path(linked.settings.data_dir)

        def die(dedupe_key: str) -> None:
            conn = sqlite3.connect(db)
            try:
                with conn:
                    conn.execute(
                        "UPDATE jobs SET state = 'dead', error = 'internal' WHERE dedupe_key = ?",
                        (dedupe_key,),
                    )
            finally:
                conn.close()

        def count() -> int:
            conn = sqlite3.connect(db)
            try:
                return int(conn.execute("SELECT count(*) FROM jobs").fetchone()[0])
            finally:
                conn.close()

        die(key)
        async with serve(linked.settings, issuer, make_validator, clock=linked.clock) as library:
            for _ in range(RERUN_CAP):  # each call re-runs once; the re-run dies again
                pending = await library.error("read_page", {"document": NOTEBOOK, "page": 5})
                assert pending["code"] == "pending"
                job = library.services.queue.get(pending["job"], linked.uid)
                assert job is not None and job.dedupe_key.startswith(f"{key}:after:")
                die(job.dedupe_key)
            jobs = count()
            refused = await library.error("read_page", {"document": NOTEBOOK, "page": 5})
            assert refused["code"] == "refused"
            assert "keeps failing" in refused["hint"]
            assert count() == jobs  # nothing new was queued
            assert library.services.queue.revive_dead_renders() == 0  # nor at a worker start
    finally:
        linked.close()


# ---- versions and read_page(version=) (acceptance 3) ---------------------------------------


async def test_edit_keeps_old_version_readable(library: Served, linked: Linked) -> None:
    before = await library.call("read_page", {"document": NOTEBOOK, "page": 1})
    old_meta, old_png = meta_of(before), image_of(before)
    linked.cloud.set_file(NOTEBOOK, PAGE_101, fixture_documents()[PDF][PDF_PAGE])
    await linked.sync()

    versions = await library.ok("versions", {"document": NOTEBOOK})
    assert len(versions["versions"]) == 2
    newest, oldest = versions["versions"]
    assert newest["current"] is True and oldest["current"] is False
    assert oldest["generation"] == old_meta["generation"]
    assert newest["generation"] > oldest["generation"]

    now = await library.call("read_page", {"document": NOTEBOOK, "page": 1})
    assert meta_of(now)["content_hash"] != old_meta["content_hash"]
    assert image_of(now) != old_png
    then = await library.call(
        "read_page", {"document": NOTEBOOK, "page": 1, "version": oldest["generation"]}
    )
    then_meta = meta_of(then)
    assert then_meta["current"] is False
    assert then_meta["content_hash"] == old_meta["content_hash"]
    assert image_of(then) == old_png
    assert "older version" in text_of(then)
    # Both PNGs and the old blob are still on disk.
    old_rel = pg.png_relpath(PAGES[0], old_meta["content_hash"])
    assert pg.resolve_png(linked.paths, old_rel).is_file()
    assert (linked.paths.blobs / old_meta["content_hash"]).is_file()
    missing = await library.error(
        "read_page", {"document": NOTEBOOK, "page": 1, "version": 999_999}
    )
    assert missing["code"] == "not_found"


async def test_versions_pagination(library: Served, linked: Linked) -> None:
    pdf_page = fixture_documents()[PDF][PDF_PAGE]
    notebook_page = fixture_documents()[NOTEBOOK][PAGE_101]
    for data in (pdf_page, notebook_page, pdf_page):
        linked.cloud.set_file(NOTEBOOK, PAGE_101, data)
        await linked.sync()
    first = await library.ok("versions", {"document": NOTEBOOK, "limit": 3})
    assert len(first["versions"]) == 3 and first["next_cursor"]
    rest = await library.ok(
        "versions", {"document": NOTEBOOK, "limit": 3, "cursor": first["next_cursor"]}
    )
    assert len(rest["versions"]) == 1 and rest["next_cursor"] is None
    generations = [v["generation"] for v in first["versions"] + rest["versions"]]
    assert generations == sorted(generations, reverse=True)
    other = await library.error("versions", {"document": PDF, "cursor": first["next_cursor"]})
    assert other["code"] == "invalid_argument"


# ---- page_image --------------------------------------------------------------------------------


async def test_page_image_is_1024_with_a_download_link(library: Served, linked: Linked) -> None:
    result = await library.call("page_image", {"document": NOTEBOOK, "page": 1})
    assert not result.is_error
    png = image_of(result)
    assert max(pg.png_size(png)) == PAGE_IMAGE_SIDE
    link = text_of(result, 1)
    assert link.startswith(
        "[Download Scriptorium_Test_Notebook-p1.png](https://mcp.example.com/dl/"
    )
    url = link.split("(", 1)[1].split(")", 1)[0]
    transport = httpx.ASGITransport(app=library.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example.com") as http:
        response = await http.get(url.removeprefix("https://mcp.example.com"))
    assert response.status_code == 200
    assert response.content == png
    assert 'filename="Scriptorium_Test_Notebook-p1.png"' in response.headers["content-disposition"]
    # The downscaled variant is cached next to the archive PNG.
    meta = await library.call("read_page", {"document": NOTEBOOK, "page": 1})
    rel = pg.png_relpath(PAGES[0], meta_of(meta)["content_hash"], max_side=PAGE_IMAGE_SIDE)
    assert pg.resolve_png(linked.paths, rel).read_bytes() == png


async def test_page_image_larger_than_the_archive_is_a_worker_job(
    tmp_path: Any, issuer: FakeIssuer, make_validator: Any
) -> None:
    from tests.worker.helpers import link

    linked = link(tmp_path, settings={"pending_wait_seconds": 0.0})
    try:
        await linked.sync()
        async with serve(linked.settings, issuer, make_validator, clock=linked.clock) as library:
            page = next(
                n
                for n, source in enumerate(_sources(linked, NOTEBOOK), start=1)
                if max(pg.png_size(_archive_png(linked, source))) < 2 * PAGE_IMAGE_SIDE
            )
            args = {"document": NOTEBOOK, "page": page, "scale": 2}
            pending = await library.error("page_image", args)
            assert pending["code"] == "pending"
            job = library.services.queue.get(pending["job"], linked.uid)
            assert job is not None and job.kind == "page_image"
            await linked.worker.drain_jobs()
            result = await library.call("page_image", args)
            assert 2 * PAGE_IMAGE_SIDE - 8 <= max(pg.png_size(image_of(result))) <= 2048
    finally:
        linked.close()


async def test_page_image_scale_bounds(library: Served) -> None:
    for scale in (0.1, 3):
        error = await library.error("page_image", {"document": NOTEBOOK, "page": 1, "scale": scale})
        assert error["code"] == "invalid_argument"


def test_image_sizes_come_from_eight_steps() -> None:
    assert image_side(1) == 1024
    assert image_side(0.25) == 256 and image_side(2) == 2048
    assert image_side(1.0001) == image_side(0.9) == 1024
    assert image_side(0.3) == 256 and image_side(1.9) == 2048
    sides = {image_side(0.25 + n / 1000) for n in range(1751)}
    assert sides == {256 * n for n in range(1, 9)}


async def test_many_scales_make_few_cached_variants(library: Served, linked: Linked) -> None:
    """A client cannot fill the disk with sizes: scales snap to steps of 0.25 (§8.3)."""
    source = _sources(linked, NOTEBOOK)[1]
    archive = max(pg.png_size(_archive_png(linked, source)))
    for n in range(40):
        scale = 0.25 + n * 0.031  # 0.25 .. 1.459
        if image_side(scale) > archive:
            continue
        result = await library.call("page_image", {"document": NOTEBOOK, "page": 2, "scale": scale})
        assert max(pg.png_size(image_of(result))) == image_side(scale)
    folder = pg.resolve_png(linked.paths, pg.png_relpath(source.page_id, source.content_hash))
    variants = sorted(p.name for p in folder.parent.glob(f"{source.content_hash}.*px.png"))
    assert 1 <= len(variants) <= 6  # 256 .. 1536 px, whatever the scales asked for


def test_fit_png_refuses_an_image_beyond_the_canvas_budget() -> None:
    header = b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
    huge = header + (10_000).to_bytes(4, "big") + (10_000).to_bytes(4, "big") + bytes(5)
    with pytest.raises(ToolError) as caught:
        fit_png(huge, 1024)  # refused from the header, before anything is decoded
    assert caught.value.code == ErrorCode.REFUSED


# ---- read_document -----------------------------------------------------------------------------


async def test_read_document_markdown_and_pagination(library: Served, linked: Linked) -> None:
    index = linked.index()
    for n, source in enumerate(_sources(linked, NOTEBOOK), start=1):
        index.put_transcript(
            NOTEBOOK, source.page_id, source.content_hash, f"Line {n}\n" * 900, "m", "lazy"
        )
    first = await library.call("read_document", {"document": NOTEBOOK})
    text, meta = text_of(first, 0), json.loads(text_of(first, 1))
    assert text.startswith(f"# {NOTEBOOK_NAME}\n\n## Page 1\n\nLine 1\n")
    assert meta["next_cursor"] is not None
    assert len(text.encode()) + len(text_of(first, 1).encode()) <= 32 * 1024
    seen = [meta["pages"]]
    while meta["next_cursor"]:
        more = await library.call(
            "read_document", {"document": NOTEBOOK, "cursor": meta["next_cursor"]}
        )
        text, meta = text_of(more, 0), json.loads(text_of(more, 1))
        assert not text.startswith("# ")
        seen.append(meta["pages"])
    assert seen[-1].endswith("-7")
    ranged = await library.call("read_document", {"document": NOTEBOOK, "pages": "3-4"})
    ranged_meta = json.loads(text_of(ranged, 1))
    assert ranged_meta["pages"] == "3-4" and ranged_meta["next_cursor"] is None
    assert "## Page 3" in text_of(ranged, 0) and "## Page 5" not in text_of(ranged, 0)


async def test_read_document_marks_untranscribed_pages(library: Served) -> None:
    result = await library.call("read_document", {"document": NOTEBOOK, "pages": "2-"})
    meta = json.loads(text_of(result, 1))
    assert meta["not_transcribed"] == [2, 3, 4, 5, 6, 7]
    assert "Not transcribed yet" in text_of(result, 0)


async def test_read_document_pdf_is_not_yet_available(library: Served) -> None:
    error = await library.error("read_document", {"document": NOTEBOOK, "format": "pdf"})
    assert error["code"] == "invalid_argument"
    bad = await library.error("read_document", {"document": NOTEBOOK, "pages": "5-2"})
    assert bad["code"] == "invalid_argument"


# ---- trash (acceptance 4) ----------------------------------------------------------------------


async def test_deleted_document_is_tombstoned_after_two_polls(
    library: Served, linked: Linked
) -> None:
    linked.cloud.remove_document(PDF)
    await linked.sync()
    assert (await library.ok("trash"))["items"] == []  # one listing is not enough
    await linked.sync()
    items = (await library.ok("trash"))["items"]
    assert [item["uuid"] for item in items] == [PDF]
    assert items[0]["state"] == "deleted"
    assert items[0]["deletion_cause"] in {"unknown", "aged", "user"}
    assert items[0]["deleted_at"].endswith("Z")
    # Hidden from list unless asked; its pages still read from the archive.
    assert [i["uuid"] for i in (await library.ok("list"))["items"]] == [NOTEBOOK]
    shown = await library.ok("list", {"include_deleted": True})
    assert {i["uuid"] for i in shown["items"]} == {NOTEBOOK, PDF}
    result = await library.call("read_page", {"document": PDF, "page": 1})
    assert not result.is_error
    assert (linked.paths.tombstones / PDF).is_dir()


async def test_trash_lists_trashed_documents(library: Served, linked: Linked) -> None:
    linked.cloud.set_file(
        NOTEBOOK, f"{NOTEBOOK}.metadata", _metadata(NOTEBOOK_NAME, parent="trash")
    )
    await linked.sync()
    items = (await library.ok("trash"))["items"]
    assert [(i["uuid"], i["state"]) for i in items] == [(NOTEBOOK, "trashed")]
    assert NOTEBOOK not in {i["uuid"] for i in (await library.ok("list"))["items"]}
    with_trash = await library.ok("list", {"include_trash": True})
    assert NOTEBOOK in {i["uuid"] for i in with_trash["items"]}


@pytest.mark.parametrize("tool", ["list", "trash", "recent", "account_status"])
async def test_tools_need_no_account_argument(library: Served, tool: str) -> None:
    args = {"since": "2000-01-01T00:00:00Z"} if tool == "recent" else {}
    await library.ok(tool, args)


def test_a_cached_variant_of_a_removed_account_is_not_found(tmp_path: Any) -> None:
    """write_png never recreates a moved account root; the cache write says not_found."""
    from types import SimpleNamespace

    from scriptorium.tools._library import _cached_variant
    from scriptorium.users.store import account_paths

    paths = account_paths(tmp_path, "0123456789abcdef", "01ARZ3NDEKTSV4RRFFQ69G5FAV")
    account: Any = SimpleNamespace(paths=paths)  # never created: as if moved aside
    source: Any = SimpleNamespace(page_id=PAGES[0], content_hash="e" * 64)
    with pytest.raises(ToolError) as caught:
        _cached_variant(account, source, pg.blank_page_png(), 256)
    assert caught.value.code == ErrorCode.NOT_FOUND
    assert not paths.root.exists()
