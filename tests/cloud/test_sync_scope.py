"""The sync scope in ``SyncOnce`` (§11.1 ``set_sync_scope``, plan 05 §3.4).

Driven by ``FakeCloud`` with synthetic documents: copies of the fixture PDF whose
``.metadata`` and ``.content`` are rewritten here to carry a parent and tags, and synthetic
folders. Names and tags are made up for the test; nothing is a real note.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from scriptorium.cloud.scope import dump_scope, make_scope, scope_fingerprint
from scriptorium.cloud.sync import SyncSettings
from tests.cloud.fake_cloud import NOTEBOOK, PAGES, PDF, FakeCloud, fixture_documents, sha256
from tests.cloud.scenarios import Harness, both, copy_of_pdf, harness

A, B, C, D, E = PAGES[:5]
F = "00000000-0000-4000-8000-0000000000f1"
G = "00000000-0000-4000-8000-0000000000f2"
UNUSED_PAGE = fixture_documents()[NOTEBOOK][f"{NOTEBOOK}/{PAGES[6]}.rm"]
"""Fixture bytes no document below uses: a new version of a page."""


def doc(uuid: str, *tags: str, parent: str = "") -> dict[str, bytes]:
    files = copy_of_pdf(uuid)
    meta = json.loads(files[f"{uuid}.metadata"])
    meta.update(parent=parent, visibleName=f"Synthetic {uuid[-3:]}")
    files[f"{uuid}.metadata"] = json.dumps(meta).encode()
    content = json.loads(files[f"{uuid}.content"])
    content["tags"] = [{"name": tag, "timestamp": 1} for tag in tags]
    files[f"{uuid}.content"] = json.dumps(content).encode()
    return files


def folder(uuid: str, *tags: str, parent: str = "") -> dict[str, bytes]:
    meta = {
        "type": "CollectionType",
        "visibleName": f"Folder {uuid[-3:]}",
        "parent": parent,
        "lastModified": "1792230716566",
    }
    content = {"tags": [{"name": tag, "timestamp": 1} for tag in tags]}
    return {
        f"{uuid}.metadata": json.dumps(meta).encode(),
        f"{uuid}.content": json.dumps(content).encode(),
    }


def setup(
    tmp_path: Path, docs: dict[str, dict[str, bytes]], **settings: int
) -> tuple[FakeCloud, Harness]:
    cloud = FakeCloud()
    cloud.put_documents(docs)
    h = harness(
        httpx.MockTransport(cloud.handler),
        tmp_path,
        settings=SyncSettings(sync_workers=1, **settings),
    )
    return cloud, h


def set_scope(h: Harness, mode: str, *tags: str) -> None:
    h.index.set_setting("sync_scope", dump_scope(make_scope(mode, tags)))


def own_files(uuid: str) -> tuple[str, ...]:
    """The files of a synthetic document no other document shares."""
    return (f"{uuid}.metadata", f"{uuid}.content", f"{uuid}/{uuid}.rm")


def excluded_at(h: Harness, uuid: str) -> object:
    row = h.index.document(uuid)
    assert row is not None
    return row.excluded_at


# ---- excluding ------------------------------------------------------------------------------


def test_deny_scope_downloads_nothing_of_an_excluded_document(tmp_path: Path) -> None:
    cloud, h = setup(
        tmp_path,
        {
            A: doc(A, "private"),
            B: doc(B, "work"),
            F: folder(F, "private"),
            C: doc(C, parent=F),
        },
    )
    set_scope(h, "deny", "private")

    report = h.poll()

    assert report.outcome == "ok"
    assert sorted(report.committed) == sorted([B, F])  # a tagged folder is still mirrored
    assert report.excluded == 2
    assert h.library_uuids() == {B, F}
    for uuid in (A, C):
        row = h.index.document(uuid)
        assert row is not None and row.excluded_at is not None
        assert row.hash_root is None and row.generation is None
        assert row.name == f"Synthetic {uuid[-3:]}"  # the row comes from .metadata
        for name in own_files(uuid):
            assert sha256(cloud.docs[uuid][name]) not in h.blob_names()
        assert sha256(cloud.docs[uuid][f"{uuid}/{uuid}.rm"]) not in cloud.targets()
        assert h.history_of(uuid) == []
    row_c = h.index.document(C)
    assert row_c is not None and row_c.parent == F
    assert h.state("scope_applied") == scope_fingerprint(make_scope("deny", ["private"]))


def test_an_unchanged_excluded_document_is_not_fetched_again(tmp_path: Path) -> None:
    cloud, h = setup(tmp_path, {A: doc(A, "private"), B: doc(B)})
    set_scope(h, "deny", "private")
    h.poll()
    cloud.requests.clear()
    cloud.set_file(B, f"{B}/{B}.rm", UNUSED_PAGE)

    report = h.poll()

    assert report.committed == (B,)
    assert report.excluded == 1
    assert cloud.doc_hash(A) not in cloud.targets()
    assert set(json.loads(h.state("scope_excluded") or "{}")) == {A}


def test_narrowing_the_scope_keeps_everything_archived(tmp_path: Path) -> None:
    cloud, h = setup(tmp_path, {A: doc(A, "private"), B: doc(B)})
    h.poll()
    files_before, history_before = h.library_files(A), h.history_of(A)
    archived = h.index.document(A)
    assert archived is not None and archived.excluded_at is None

    set_scope(h, "deny", "private")
    narrowed = h.poll()

    assert narrowed.outcome == "ok"
    assert (narrowed.committed, narrowed.tombstoned, narrowed.excluded) == ((), (), 1)
    row = h.index.document(A)
    assert row is not None and row.excluded_at is not None
    assert (row.hash_root, row.generation) == (archived.hash_root, archived.generation)
    assert h.library_files(A) == files_before
    assert h.tombstoned_uuids() == set()

    # A new version on the tablet is not downloaded; the archived one stays as it was.
    cloud.set_file(A, f"{A}/{A}.rm", UNUSED_PAGE)
    changed = h.poll()

    assert changed.committed == () and changed.tombstoned == ()
    assert sha256(UNUSED_PAGE) not in h.blob_names()
    assert h.library_files(A) == files_before
    assert h.history_of(A) == history_before
    again = h.index.document(A)
    assert again is not None and again.hash_root == archived.hash_root


def test_widening_the_scope_brings_documents_back(tmp_path: Path) -> None:
    cloud, h = setup(tmp_path, {A: doc(A, "private"), B: doc(B)})
    h.poll()  # all: A and B archived
    set_scope(h, "deny", "private")
    cloud.put_document(C, doc(C, "private"))
    h.poll()  # A leaves (archived, unchanged), C is new and never archived
    assert excluded_at(h, A) is not None and excluded_at(h, C) is not None
    assert h.library_uuids() == {A, B}
    cloud.requests.clear()

    set_scope(h, "all")
    report = h.poll()

    assert report.outcome == "ok"
    assert report.committed == (C,)  # committed like a new document
    assert cloud.doc_hash(A) not in cloud.targets()  # its archived version is current
    assert excluded_at(h, A) is None and excluded_at(h, C) is None
    assert h.library_uuids() == {A, B, C}
    assert report.excluded == 0
    assert h.state("scope_excluded") is None


def test_an_excluded_document_that_changed_is_committed_when_it_returns(tmp_path: Path) -> None:
    cloud, h = setup(tmp_path, {A: doc(A, "private"), B: doc(B)})
    h.poll()
    set_scope(h, "deny", "private")
    h.poll()
    cloud.set_file(A, f"{A}/{A}.rm", UNUSED_PAGE)
    h.poll()  # the change is seen (small files only) and remembered
    assert sha256(UNUSED_PAGE) not in h.blob_names()

    set_scope(h, "deny", "other")
    report = h.poll()

    assert report.committed == (A,)
    assert h.library_files(A)[f"{A}/{A}.rm"] == sha256(UNUSED_PAGE)
    assert excluded_at(h, A) is None
    assert len(h.history_of(A)) == 2


def test_folder_tags_reach_the_documents_below(tmp_path: Path) -> None:
    cloud, h = setup(
        tmp_path,
        {
            F: folder(F, "work"),
            G: folder(G, parent=F),
            C: doc(C, parent=G),
            D: doc(D),
            E: doc(E, "work"),
        },
    )
    set_scope(h, "allow", "work")

    first = h.poll()

    assert sorted(first.committed) == sorted([F, G, C, E])
    assert excluded_at(h, D) is not None

    # Moving D under the tagged folder brings it in.
    moved = json.loads(cloud.docs[D][f"{D}.metadata"])
    moved["parent"] = F
    cloud.set_file(D, f"{D}.metadata", json.dumps(moved).encode())
    second = h.poll()

    assert second.committed == (D,)
    assert excluded_at(h, D) is None

    # Untagging the folder takes everything below it out; nothing archived is removed.
    cloud.set_file(F, f"{F}.content", json.dumps({"tags": []}).encode())
    third = h.poll()

    assert third.committed == (F,) and third.tombstoned == ()
    assert third.excluded == 2
    assert excluded_at(h, C) is not None and excluded_at(h, D) is not None
    assert excluded_at(h, E) is None and excluded_at(h, G) is None
    assert h.library_uuids() == {F, G, C, D, E}


def test_a_parent_cycle_in_the_cloud_ends(tmp_path: Path) -> None:
    _cloud, h = setup(
        tmp_path,
        {F: folder(F, "private", parent=G), G: folder(G, parent=F), C: doc(C, parent=G)},
    )
    set_scope(h, "deny", "private")

    report = h.poll()

    assert report.outcome == "ok"
    assert excluded_at(h, C) is not None


# ---- what the scope does not change ---------------------------------------------------------


def test_an_excluded_document_deleted_in_the_cloud_is_tombstoned(tmp_path: Path) -> None:
    cloud, h = setup(tmp_path, {A: doc(A, "private"), B: doc(B)})
    set_scope(h, "deny", "private")
    h.poll()
    cloud.remove_document(A)
    h.poll()

    report = h.poll()

    assert report.tombstoned == (A,)
    row = h.index.document(A)
    assert row is not None and row.deleted_at is not None


def test_a_malformed_scope_stops_the_poll_before_any_request(tmp_path: Path) -> None:
    cloud, h = setup(tmp_path, {A: doc(A, "private")})
    h.index.set_setting("sync_scope", '{"mode": "deny", "tags": []}')

    report = h.poll()

    assert (report.outcome, report.reason) == ("backoff", "scope_malformed")
    assert cloud.requests == []
    assert h.library_uuids() == set()


def test_a_budget_stop_in_the_scope_pass_applies_nothing(tmp_path: Path) -> None:
    _cloud, h = setup(tmp_path, {A: doc(A, "private"), B: doc(B), C: doc(C)}, sync_max_poll_bytes=1)
    set_scope(h, "deny", "private")

    report = h.poll()

    assert (report.outcome, report.reason) == ("partial", "byte_budget")
    assert report.committed == ()
    assert h.state("scope_applied") is None
    assert h.state("last_root_hash") is None


def test_tag_names_never_reach_the_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secret = "synthetic-tag-7f3a"
    _cloud, h = setup(tmp_path, {A: doc(A, secret), B: doc(B)})
    set_scope(h, "deny", secret)
    h.poll()

    assert excluded_at(h, A) is not None
    text = repr(
        [(r.getMessage(), r.args, getattr(r, "scriptorium_fields", {})) for r in caplog.records]
    )
    assert secret not in text
    assert A not in text


# ---- archive repair (journal recovery with a vanished source) ---------------------------------


def test_a_damaged_document_is_committed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = FakeCloud()
    both(cloud)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    h.poll()
    answers = [frozenset({NOTEBOOK}), frozenset({PDF})]
    monkeypatch.setattr(h.archive, "take_damaged", lambda: answers.pop(0) if answers else set())

    # Damage reported before the poll: the same poll lists and commits it again.
    first = h.poll()
    assert first.committed == (NOTEBOOK,) and not first.root_unchanged
    row = h.index.document(NOTEBOOK)
    assert row is not None and row.hash_root == cloud.doc_hash(NOTEBOOK)

    # Damage reported during a poll: hash_root is nulled and the next poll commits it.
    pdf = h.index.document(PDF)
    assert pdf is not None and pdf.hash_root is None
    assert h.state("last_root_hash") is None
    second = h.poll()
    assert second.committed == (PDF,)
    pdf = h.index.document(PDF)
    assert pdf is not None and pdf.hash_root == cloud.doc_hash(PDF)
