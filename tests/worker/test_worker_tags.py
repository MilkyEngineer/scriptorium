"""The worker's commit hook keeps ``tags`` in step with each document's ``.content`` (§7 step 3).

The S3 fixture notebook has no tags; these tests add synthetic ones to its ``.content``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scriptorium.library import pages as pg
from scriptorium.library.tags import TagSet
from scriptorium.logging import JsonFormatter
from tests.cloud.fake_cloud import NOTEBOOK, PAGES, fixture_documents
from tests.worker.helpers import Linked, link

FOLDER = "00000000-0000-4000-8000-0000000000f1"
SECRET_TAG = "tag-name-marker-5d1e"


def content_with(**tags: Any) -> bytes:
    content = json.loads(fixture_documents()[NOTEBOOK][f"{NOTEBOOK}.content"])
    content.update(tags)
    return json.dumps(content).encode()


def folder_files(tags: list[dict[str, Any]]) -> dict[str, bytes]:
    metadata = {
        "lastModified": "1792230476439",
        "parent": "",
        "type": "CollectionType",
        "visibleName": "Tagged folder",
    }
    return {
        f"{FOLDER}.metadata": json.dumps(metadata).encode(),
        f"{FOLDER}.content": json.dumps({"tags": tags}).encode(),
    }


@pytest.fixture
def linked(tmp_path: Path) -> Iterator[Linked]:
    account = link(tmp_path)
    yield account
    account.close()


async def test_a_poll_stores_tags_and_a_later_poll_replaces_them(linked: Linked) -> None:
    linked.cloud.set_file(
        NOTEBOOK,
        f"{NOTEBOOK}.content",
        content_with(
            tags=[{"name": "alpha"}, {"name": "beta"}],
            pageTags=[{"name": "gamma", "pageId": PAGES[2]}],
        ),
    )
    linked.cloud.put_documents({FOLDER: folder_files([{"name": "shelf"}])})
    await linked.sync()
    index = linked.index()
    tags = index.tags_of(NOTEBOOK)
    assert tags.document == {"alpha", "beta"}
    assert dict(tags.pages) == {PAGES[2]: {"gamma"}}
    assert index.tags_of(FOLDER).document == {"shelf"}  # a folder has tags, no pages
    assert index.pages(FOLDER) == []
    row = index.document(NOTEBOOK)
    assert row is not None and row.tags == ("alpha", "beta")

    # Removing a tag on the tablet removes it here on the next poll.
    linked.cloud.set_file(NOTEBOOK, f"{NOTEBOOK}.content", content_with(tags=[{"name": "beta"}]))
    await linked.sync()
    assert index.tags_of(NOTEBOOK) == TagSet(frozenset({"beta"}))
    assert index.documents_tagged(["alpha"]) == frozenset()
    assert index.documents_tagged(["beta", "shelf"]) == {NOTEBOOK, FOLDER}


async def test_pages_and_tags_come_from_the_same_version(linked: Linked) -> None:
    await linked.sync()
    index = linked.index()
    assert index.tags_of(NOTEBOOK) == TagSet()
    version = pg.current_version(linked.paths, index, NOTEBOOK)
    assert version is not None
    assert pg.content_tags(linked.paths, version) == TagSet()
    linked.cloud.set_file(NOTEBOOK, f"{NOTEBOOK}.content", content_with(tags=[{"name": "fresh"}]))
    await linked.sync()
    version = pg.current_version(linked.paths, index, NOTEBOOK)
    assert version is not None
    assert pg.content_tags(linked.paths, version).document == {"fresh"}
    assert index.tags_of(NOTEBOOK).document == {"fresh"}
    assert [p.page_id for p in index.pages(NOTEBOOK)] == list(PAGES)


async def test_skipped_tags_are_logged_as_a_count_never_by_name(
    linked: Linked, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="scriptorium")
    linked.cloud.set_file(
        NOTEBOOK,
        f"{NOTEBOOK}.content",
        content_with(
            tags=[{"name": SECRET_TAG}, {"name": f"{SECRET_TAG}\x00"}],
            pageTags=[{"name": SECRET_TAG, "pageId": "00000000-0000-4000-8000-000000000999"}],
        ),
    )
    await linked.sync()
    assert linked.index().tags_of(NOTEBOOK).document == {SECRET_TAG}
    skipped = [r for r in caplog.records if r.getMessage() == "tags_skipped"]
    assert skipped
    formatter = JsonFormatter()
    lines = [formatter.format(r) for r in caplog.records]
    assert any('"count":2' in line and '"acct"' in line for line in lines)
    assert not any(SECRET_TAG in line for line in lines)
