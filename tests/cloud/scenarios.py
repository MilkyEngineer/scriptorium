"""The synthetic sync scenarios behind the committed cassettes.

A scenario is a sequence of polls; before each poll a step mutates the ``FakeCloud``.
``build_cassettes`` runs the steps against the fake and records the HTTP; the tests
replay the same number of polls against the cassette alone, with no fake behind it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scriptorium.cloud.client import CloudClient
from scriptorium.cloud.sync import SyncOnce, SyncReport, SyncSettings
from tests.cloud.fake_cloud import (
    DEVICE_TOKEN,
    NOTEBOOK,
    PAGES,
    PDF,
    ROOT_KEY,
    TOKEN_KEY,
    FakeCloud,
    fixture_documents,
    renamed,
    sha256,
)
from tests.fakes.archive import FakeArchive
from tests.fakes.index import FakeIndex
from tests.fakes.tombstones import plan

Step = Callable[[FakeCloud], None]

PAGE_101 = f"{NOTEBOOK}/{PAGES[0]}.rm"
PDF_PAGE = f"{PDF}/{PAGES[0]}.rm"


def _docs() -> dict[str, dict[str, bytes]]:
    return fixture_documents()


def nothing(_cloud: FakeCloud) -> None:
    """A poll with no change in the cloud."""


def both(cloud: FakeCloud) -> None:
    cloud.put_documents(_docs())


def notebook_only(cloud: FakeCloud) -> None:
    cloud.put_documents({NOTEBOOK: _docs()[NOTEBOOK]})


def copy_of_pdf(uuid: str) -> dict[str, bytes]:
    """A synthetic document: the PDF's metadata and content, plus one notebook page.

    The page differs per copy, so every copy has its own index hash (``HashEntries``
    covers the file hashes, not their names). All bytes are fixture files.
    """
    docs = _docs()
    files = {name: data for name, data in renamed(docs[PDF], PDF, uuid).items() if "/" not in name}
    files[f"{uuid}/{uuid}.rm"] = docs[NOTEBOOK][f"{NOTEBOOK}/{uuid}.rm"]
    return files


def nine(cloud: FakeCloud) -> None:
    """The two fixture documents plus seven synthetic ones under the page UUIDs."""
    cloud.put_documents({**_docs(), **{uuid: copy_of_pdf(uuid) for uuid in PAGES}})


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    steps: tuple[Step, ...]
    client: dict[str, Any] = field(default_factory=dict)
    root_schema: int = 4


def _change_page(cloud: FakeCloud) -> None:
    # The notebook's first page now holds the PDF page's strokes: a new blob.
    cloud.set_file(NOTEBOOK, PAGE_101, _docs()[PDF][PDF_PAGE])


def _remove_pdf(cloud: FakeCloud) -> None:
    cloud.remove_document(PDF)


def _restore_pdf(cloud: FakeCloud) -> None:
    cloud.put_documents({PDF: _docs()[PDF]})


def _partial(cloud: FakeCloud) -> None:
    # The PDF goes and the notebook changes, but the notebook's new index fails to load.
    cloud.docs.pop(PDF)
    cloud.docs[NOTEBOOK][PAGE_101] = _docs()[PDF][PDF_PAGE]
    cloud.publish()
    cloud.status[cloud.doc_hash(NOTEBOOK)] = 503


def _remove_six(cloud: FakeCloud) -> None:
    cloud.remove_document(*PAGES[:6])


def _regress(cloud: FakeCloud) -> None:
    cloud.docs.pop(PDF)
    cloud.publish(generation=cloud.generation - 1)


def _empty(cloud: FakeCloud) -> None:
    cloud.remove_document(NOTEBOOK, PDF)


def _corrupt(cloud: FakeCloud) -> None:
    both(cloud)
    docs = _docs()
    # Page 101's hash answers with the notebook's .content: a fixture file, the wrong
    # one, and smaller than page 101, so the size cap lets it through to the hash check.
    cloud.corrupt[sha256(docs[NOTEBOOK][PAGE_101])] = docs[NOTEBOOK][f"{NOTEBOOK}.content"]


def _token(status: int) -> Step:
    def step(cloud: FakeCloud) -> None:
        both(cloud)
        cloud.status[TOKEN_KEY] = status

    return step


def _root(status: int, headers: dict[str, str] | None = None) -> Step:
    def step(cloud: FakeCloud) -> None:
        both(cloud)
        cloud.status[ROOT_KEY] = status
        if headers:
            cloud.headers[ROOT_KEY] = headers

    return step


def _root_unreachable(cloud: FakeCloud) -> None:
    both(cloud)
    cloud.errors[ROOT_KEY] = "connect"


def _index_503(cloud: FakeCloud) -> None:
    docs = _docs()
    cloud.put_documents(
        {
            **docs,
            PAGES[0]: copy_of_pdf(PAGES[0]),
            PAGES[1]: copy_of_pdf(PAGES[1]),
        }
    )
    for uuid in (NOTEBOOK, PDF, PAGES[0]):
        cloud.status[cloud.doc_hash(uuid)] = 503


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario(
            "full_listing",
            "First poll of an account holding the fixture notebook and PDF (schema 4 root).",
            (both,),
        ),
        Scenario(
            "full_listing_schema3",
            "The same library behind a schema 3 root addressed by HashEntries.",
            (both,),
            root_schema=3,
        ),
        Scenario(
            "changed_page",
            "The notebook alone, then its first page changes to new content.",
            (notebook_only, _change_page),
        ),
        Scenario(
            "deleted_document",
            "Both documents; the PDF disappears; a quiet poll; the PDF comes back.",
            (both, _remove_pdf, nothing, _restore_pdf),
        ),
        Scenario(
            "partial_listing",
            "Both documents; the PDF disappears while the notebook's index answers 503, twice.",
            (both, _partial, nothing),
        ),
        Scenario(
            "mass_delete",
            "Nine documents; six disappear; a quiet poll; another quiet poll.",
            (nine, _remove_six, nothing, nothing),
        ),
        Scenario(
            "generation_regressed",
            "Both documents; then a root with a lower generation; then a quiet poll.",
            (both, _regress, nothing),
        ),
        Scenario(
            "empty_root",
            "Both documents; then a root that lists nothing.",
            (both, _empty),
        ),
        Scenario(
            "hash_mismatch",
            "Both documents, but one notebook page is served with another page's bytes.",
            (_corrupt,),
        ),
        Scenario(
            "token_rejected_401",
            "The token endpoint answers 401 for the device token.",
            (_token(401),),
        ),
        Scenario(
            "token_rejected_403",
            "The token endpoint answers 403 for the device token.",
            (_token(403),),
        ),
        Scenario(
            "token_unavailable",
            "The token endpoint answers 503: an outage, not a rejection.",
            (_token(503),),
        ),
        Scenario(
            "rate_limited",
            "The root answers 429 with Retry-After: 120.",
            (_root(429, {"retry-after": "120"}),),
        ),
        Scenario(
            "server_unavailable",
            "The root answers 503.",
            (_root(503),),
        ),
        Scenario(
            "network_error",
            "The root cannot be reached.",
            (_root_unreachable,),
        ),
        Scenario(
            "index_503",
            "Four documents; the first three document indexes answer 503.",
            (_index_503,),
        ),
    )
}

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@dataclass
class Harness:
    """One account: a client over some transport, a fake archive and a fake index."""

    client: CloudClient
    archive: FakeArchive
    index: FakeIndex
    clock: Clock
    settings: SyncSettings

    def sync(self) -> SyncOnce:
        return SyncOnce(
            self.client, self.archive, self.index, self.settings, self.clock, planner=plan
        )

    def poll(self) -> SyncReport:
        return self.sync().run()


def harness(
    transport: Any,
    tmp: Path,
    *,
    client: dict[str, Any] | None = None,
    settings: SyncSettings | None = None,
) -> Harness:
    return Harness(
        client=CloudClient(
            DEVICE_TOKEN, cache_dir=tmp / "cache", transport=transport, **(client or {})
        ),
        archive=FakeArchive(tmp / "account"),
        index=FakeIndex(),
        clock=Clock(),
        # One blob worker: a deterministic request order for recording.
        settings=settings or SyncSettings(sync_workers=1),
    )
