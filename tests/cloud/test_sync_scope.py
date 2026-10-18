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

from scriptorium.cloud.scope import (
    MAX_TAGS_PER_DOCUMENT,
    dump_scope,
    make_scope,
    scope_fingerprint,
)
from scriptorium.cloud.sync import SyncSettings, _load_seen
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


def test_a_folder_that_lists_pages_is_decided_like_a_document(tmp_path: Path) -> None:
    """The type is the cloud's word: a "folder" carrying a page does not pass the scope."""
    odd = folder(F)
    odd[f"{F}/{F}.rm"] = UNUSED_PAGE
    cloud, h = setup(tmp_path, {F: odd, G: folder(G), B: doc(B, "work")})
    set_scope(h, "allow", "work")

    report = h.poll()

    assert report.outcome == "ok"
    assert sorted(report.committed) == sorted([B, G])  # a plain folder is still mirrored
    assert excluded_at(h, F) is not None
    assert sha256(UNUSED_PAGE) not in h.blob_names()
    assert sha256(UNUSED_PAGE) not in cloud.targets()

    # Unchanged and excluded, it is not let in later as a folder either: a changed folder
    # has every listed item decided again, this one from its excluded (document) row.
    cloud.put_documents({G: folder(G, "elsewhere")})
    again = h.poll()
    assert again.outcome == "ok" and again.committed == (G,)
    assert excluded_at(h, F) is not None
    assert sha256(UNUSED_PAGE) not in h.blob_names()

    # Tagged, it is in scope like any document.
    tagged = folder(F, "work")
    tagged[f"{F}/{F}.rm"] = UNUSED_PAGE
    cloud.put_documents({F: tagged})
    assert F in h.poll().committed
    assert sha256(UNUSED_PAGE) in h.blob_names()


def test_an_excluded_folder_with_pages_keeps_the_clouds_type(tmp_path: Path) -> None:
    odd = folder(F)
    odd[f"{F}/{F}.rm"] = UNUSED_PAGE
    cloud, h = setup(tmp_path, {F: odd, B: doc(B, "work")})
    set_scope(h, "allow", "work")

    h.poll()

    row = h.index.document(F)
    assert row is not None and row.excluded_at is not None
    assert row.type == "CollectionType"  # not rewritten to DocumentType

    tagged = folder(F, "work")
    tagged[f"{F}/{F}.rm"] = UNUSED_PAGE
    cloud.put_documents({F: tagged})
    assert F in h.poll().committed
    back = h.index.document(F)
    assert back is not None and back.type == "CollectionType" and back.excluded_at is None


def test_a_folder_with_pages_archived_before_a_scope_is_decided_like_a_document(
    tmp_path: Path,
) -> None:
    """Archived under ``all``, unchanged since: the archive's file list, not the stored type,
    decides whether it is a folder the scope lets through."""
    odd = folder(F)
    odd[f"{F}/{F}.rm"] = UNUSED_PAGE
    _cloud, h = setup(tmp_path, {F: odd, G: folder(G), B: doc(B, "work")})
    first = h.poll()
    assert sorted(first.committed) == sorted([F, G, B])
    files_before, history_before = h.library_files(F), h.history_of(F)
    set_scope(h, "deny", "private")
    assert h.poll().outcome == "ok"
    assert excluded_at(h, F) is None  # untagged: inside a deny scope
    set_scope(h, "allow", "work")

    report = h.poll()

    assert report.outcome == "ok" and report.committed == ()
    row = h.index.document(F)
    assert row is not None and row.excluded_at is not None and row.type == "CollectionType"
    assert excluded_at(h, G) is None  # a folder-shaped folder is still exempt
    assert h.library_files(F) == files_before  # narrowing keeps what is archived
    assert h.history_of(F) == history_before
    assert report.tombstoned == ()


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
    assert h.state("scope_applied") == "pending"
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


# ---- failing closed: never inside the scope on missing information ----------------------------


@pytest.mark.parametrize("mode", ["deny", "allow"])
@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[]", b'{"tags": "private"}', b'{"tags": ["private"]}', b'{"tags": [{}]}'],
    ids=["unparseable", "not-an-object", "not-a-list", "bare-entry", "no-name"],
)
def test_unreadable_tags_leave_a_document_undecided(
    tmp_path: Path, mode: str, content: bytes
) -> None:
    cloud, h = setup(tmp_path, {A: doc(A), B: doc(B, "work")})
    cloud.set_file(A, f"{A}.content", content)
    set_scope(h, mode, "private" if mode == "deny" else "work")

    report = h.poll()

    assert report.outcome == "partial"
    assert report.failed == {A: "scope_tags_unknown"}
    assert report.committed == (B,)
    assert h.index.document(A) is None  # no row: nothing was decided about it
    assert sha256(cloud.docs[A][f"{A}/{A}.rm"]) not in cloud.targets()
    assert h.state("last_root_hash") is None  # the next poll looks again


@pytest.mark.parametrize("mode", ["deny", "allow"])
def test_an_index_without_content_leaves_a_document_undecided(tmp_path: Path, mode: str) -> None:
    """Every real document has a ``.content``; one whose index leaves it out could be hiding a
    denied tag, so its tags are unknown, not empty (fail closed)."""
    files = doc(A, "private")
    del files[f"{A}.content"]
    cloud, h = setup(tmp_path, {A: files, B: doc(B, "work")})
    set_scope(h, mode, "private" if mode == "deny" else "work")

    report = h.poll()

    assert report.outcome == "partial"
    assert report.failed == {A: "scope_tags_unknown"}
    assert report.committed == (B,)
    assert h.index.document(A) is None
    assert sha256(cloud.docs[A][f"{A}/{A}.rm"]) not in cloud.targets()


def test_a_folder_tag_still_settles_a_document_without_content(tmp_path: Path) -> None:
    files = doc(C, parent=F)
    del files[f"{C}.content"]
    cloud, h = setup(tmp_path, {F: folder(F, "private"), C: files})
    set_scope(h, "deny", "private")

    report = h.poll()

    assert report.outcome == "ok" and report.committed == (F,)
    assert excluded_at(h, C) is not None
    assert sha256(cloud.docs[C][f"{C}/{C}.rm"]) not in cloud.targets()


def test_an_archived_version_without_content_has_unknown_tags(tmp_path: Path) -> None:
    """Archived under ``all`` from an index without ``.content``: once a scope is set, its
    tags are unknown, so it is left as it was rather than decided on "no tags"."""
    files = doc(A, "private")
    del files[f"{A}.content"]
    _cloud, h = setup(tmp_path, {A: files, B: doc(B)})
    h.poll()
    before = h.index.document(A)
    assert before is not None and before.hash_root is not None
    set_scope(h, "deny", "private")

    report = h.poll()

    assert report.failed == {A: "scope_tags_unknown"}
    assert h.index.document(A) == before


def test_an_archived_document_whose_new_tags_are_unreadable_is_left_as_it_was(
    tmp_path: Path,
) -> None:
    cloud, h = setup(tmp_path, {A: doc(A), B: doc(B)})
    set_scope(h, "deny", "private")
    h.poll()
    before = h.index.document(A)
    files_before = h.library_files(A)
    cloud.set_file(A, f"{A}.content", b"{not json")
    cloud.set_file(A, f"{A}/{A}.rm", UNUSED_PAGE)

    report = h.poll()

    assert report.failed == {A: "scope_tags_unknown"} and report.committed == ()
    assert h.index.document(A) == before
    assert h.library_files(A) == files_before
    assert sha256(UNUSED_PAGE) not in cloud.targets()


def test_a_denied_tag_beyond_the_display_cut_still_excludes(tmp_path: Path) -> None:
    # The index keeps the first 100 tags in sort order for display; the scope reads them all.
    many = [f"a{i:03}" for i in range(150)]
    cloud, h = setup(tmp_path, {A: doc(A, *many, "zz-private"), B: doc(B)})
    set_scope(h, "deny", "zz-private")

    report = h.poll()

    assert report.outcome == "ok" and report.committed == (B,)
    assert excluded_at(h, A) is not None
    assert sha256(cloud.docs[A][f"{A}/{A}.rm"]) not in cloud.targets()
    # Too many tags to remember: looked at again on the next poll rather than trusted.
    assert A not in json.loads(h.state("scope_excluded") or "{}")


def test_a_folder_with_unreadable_tags_keeps_what_is_below_it_undecided(tmp_path: Path) -> None:
    cloud, h = setup(
        tmp_path,
        {
            F: folder(F),
            C: doc(C, parent=F),
            E: doc(E, "private", parent=F),  # its own tag settles it
            D: doc(D),
        },
    )
    cloud.set_file(F, f"{F}.content", b"{not json")
    set_scope(h, "deny", "private")

    first = h.poll()

    assert first.failed == {C: "scope_ancestor_unknown"}
    assert sorted(first.committed) == sorted([F, D])  # the folder itself is still mirrored
    assert h.index.document(C) is None
    assert excluded_at(h, E) is not None
    for uuid in (C, E):
        assert sha256(cloud.docs[uuid][f"{uuid}/{uuid}.rm"]) not in cloud.targets()

    # The archived folder has the same unreadable .content: still undecided.
    again = h.poll()
    assert again.failed == {C: "scope_ancestor_unknown"} and again.committed == ()

    cloud.set_file(F, f"{F}.content", json.dumps({"tags": []}).encode())
    fixed = h.poll()
    assert fixed.outcome == "ok"
    assert sorted(fixed.committed) == sorted([F, C])


@pytest.mark.parametrize("broken", ["index", "content"])
def test_a_changed_folder_not_read_this_poll_is_unknown(tmp_path: Path, broken: str) -> None:
    cloud, h = setup(tmp_path, {F: folder(F), C: doc(C, parent=F)})
    set_scope(h, "deny", "private")
    h.poll()
    before = h.index.document(C)
    assert before is not None and before.excluded_at is None

    # On the tablet the folder is tagged and the document below it changes; the folder's
    # new version cannot be read this poll. Its archived (untagged) version must not count.
    tagged = json.dumps({"tags": [{"name": "private", "timestamp": 2}]}).encode()
    cloud.set_file(F, f"{F}.content", tagged)
    cloud.set_file(C, f"{C}/{C}.rm", UNUSED_PAGE)
    target = cloud.doc_hash(F) if broken == "index" else sha256(tagged)
    cloud.status[target] = 404

    report = h.poll()

    assert F in report.failed
    assert report.failed[C] == "scope_ancestor_unknown"
    assert report.committed == ()
    assert sha256(UNUSED_PAGE) not in cloud.targets()
    assert h.index.document(C) == before

    del cloud.status[target]
    later = h.poll()

    assert later.committed == (F,)
    assert excluded_at(h, C) is not None
    assert sha256(UNUSED_PAGE) not in cloud.targets()


def test_a_remembered_entry_with_a_bad_tag_is_dropped_whole() -> None:
    good = "a" * 64
    stored = {
        A: [good, ["x", "y"]],
        B: [good, ["x", " not normalised"]],  # dropping only the bad tag could let B in
        C: [good, [f"t{i:03}" for i in range(MAX_TAGS_PER_DOCUMENT + 1)]],
        D: [good, ["x", 3]],
    }
    assert set(_load_seen(json.dumps(stored))) == {A}


def test_an_interrupted_scope_change_is_redone_whole(tmp_path: Path) -> None:
    cloud, h = setup(tmp_path, {A: doc(A, "y"), B: doc(B, "x"), C: doc(C)})
    set_scope(h, "deny", "x")
    h.poll()
    applied = h.state("scope_applied")
    assert applied == scope_fingerprint(make_scope("deny", ["x"]))
    assert excluded_at(h, A) is None and excluded_at(h, B) is not None

    # A change of scope that a poll applies only in part (C cannot be decided).
    set_scope(h, "deny", "y")
    cloud.set_file(C, f"{C}.content", b"{not json")
    partial = h.poll()
    assert partial.outcome == "partial" and partial.failed == {C: "scope_tags_unknown"}
    assert excluded_at(h, A) is not None and excluded_at(h, B) is None
    assert h.state("scope_applied") == "pending"

    # Back to the scope the last full poll applied: every document is decided again, not
    # only the changed ones, so A does not stay out on the interrupted poll's decision.
    set_scope(h, "deny", "x")
    back = h.poll()
    assert excluded_at(h, A) is None and excluded_at(h, B) is not None
    assert back.failed == {C: "scope_tags_unknown"}
    assert h.state("scope_applied") == "pending"

    cloud.set_file(C, f"{C}.content", json.dumps({"tags": []}).encode())
    done = h.poll()
    assert done.outcome == "ok" and done.committed == (C,)
    assert h.state("scope_applied") == applied
