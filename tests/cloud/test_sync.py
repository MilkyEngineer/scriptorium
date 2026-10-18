"""SyncOnce against replayed cassettes (and, for fault injection, the in-memory cloud)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from scriptorium.cloud import sync as sync_module
from scriptorium.cloud.client import ROOT_URL
from scriptorium.cloud.sync import SyncReport, SyncSettings
from tests.cloud.cassettes import Player
from tests.cloud.fake_cloud import (
    NOTEBOOK,
    PAGES,
    PDF,
    ROOT_KEY,
    FakeCloud,
    fixture_documents,
    sha256,
)
from tests.cloud.scenarios import PAGE_101, PDF_PAGE, SCENARIOS, Clock, Harness, both, harness, nine
from tests.fakes.archive import SimulatedCrash

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ARCHIVE_OPS = {"stage", "put_blob", "commit", "tombstone", "untombstone", "sweep_staging"}


def replay(name: str, tmp_path: Path, **client: object) -> tuple[Harness, Player, list[SyncReport]]:
    scenario = SCENARIOS[name]
    player = Player.named(name)
    h = harness(player, tmp_path, client={**scenario.client, **client})
    reports = [h.poll() for _ in scenario.steps]
    return h, player, reports


def expected_files(uuid: str) -> dict[str, str]:
    return {name: sha256(data) for name, data in fixture_documents()[uuid].items()}


def assert_safe(h: Harness) -> None:
    h.archive.assert_nothing_lost()
    assert set(h.archive.op_names()) <= _ARCHIVE_OPS
    assert h.index.writes_outside_transaction == 0


# ---- the happy path ----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["full_listing", "full_listing_schema3"])
def test_full_listing_mirrors_the_fixture_in_xochitl_layout(name: str, tmp_path: Path) -> None:
    h, player, (report,) = replay(name, tmp_path)

    assert report.outcome == "ok", report
    assert report.listing_complete
    assert sorted(report.committed) == [NOTEBOOK, PDF]
    assert report.blobs_downloaded == 14
    for uuid in (NOTEBOOK, PDF):
        assert h.archive.library[uuid] == expected_files(uuid)
        for path in h.archive.current_files(uuid).values():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == path.name
    notebook, pdf = h.index.document(NOTEBOOK), h.index.document(PDF)
    assert notebook is not None and pdf is not None
    assert (notebook.name, notebook.type, notebook.parent) == (
        "Scriptorium Test Notebook",
        "DocumentType",
        None,
    )
    assert (notebook.source_kind, notebook.page_count) == ("notebook", 7)
    assert (pdf.name, pdf.source_kind, pdf.page_count) == ("Scriptorium Test PDF", "pdf", 1)
    assert notebook.last_modified == datetime.fromtimestamp(1792230476.439, UTC)
    assert notebook.generation == report.generation
    assert [len(h.index.history_of(u)) for u in (NOTEBOOK, PDF)] == [1, 1]
    assert h.index.state["last_full_listing_ok"] == "true"
    assert h.index.state["last_root_hash"] == report.root_hash
    assert json.loads(h.index.state["previous_listing"]) == {
        "complete": True,
        "generation": report.generation,
        "uuids": [NOTEBOOK, PDF],
    }
    assert player.unplayed() == 0
    assert_safe(h)


def test_changed_page_adds_a_blob_and_a_history_row_and_keeps_the_old_one(
    tmp_path: Path,
) -> None:
    h, _player, (first, second) = replay("changed_page", tmp_path)
    old_sha = expected_files(NOTEBOOK)[PAGE_101]
    new_sha = expected_files(PDF)[PDF_PAGE]

    assert first.outcome == second.outcome == "ok"
    assert second.committed == (NOTEBOOK,)
    assert second.blobs_downloaded == 1
    assert h.archive.library[NOTEBOOK][PAGE_101] == new_sha
    assert h.archive.blob_path(new_sha).is_file()
    old_blob = h.archive.blob_path(old_sha)
    assert hashlib.sha256(old_blob.read_bytes()).hexdigest() == old_sha
    assert [(uuid, files[PAGE_101]) for uuid, _gen, files in h.archive.holding] == [
        (NOTEBOOK, old_sha)
    ]
    history = h.index.history_of(NOTEBOOK)
    assert [gen for gen, _ in history] == [first.generation, second.generation]
    assert history[0][1] != history[1][1]
    row = h.index.document(NOTEBOOK)
    assert row is not None and row.hash_root == history[1][1]
    assert_safe(h)


# ---- tombstones ------------------------------------------------------------------------


def test_deleted_document_is_tombstoned_only_after_two_complete_listings(
    tmp_path: Path,
) -> None:
    h, player, reports = replay("deleted_document", tmp_path)
    _full, gone_once, gone_twice, back = reports

    assert [r.outcome for r in reports] == ["ok"] * 4
    assert gone_once.tombstoned == ()
    assert gone_twice.root_unchanged
    assert gone_twice.tombstoned == (PDF,)
    assert h.archive.op_names().count("tombstone") == 1
    row_after = h.index.document(PDF)
    assert row_after is not None
    # Un-tombstoned in place on the fourth poll, with the same files as before.
    assert back.untombstoned == (PDF,)
    assert back.committed == ()
    assert h.archive.library[PDF] == expected_files(PDF)
    assert row_after.deleted_at is None
    assert player.unplayed() == 0
    assert_safe(h)


def test_tombstone_moves_files_and_marks_the_row(tmp_path: Path) -> None:
    scenario = SCENARIOS["deleted_document"]
    h = harness(Player.named("deleted_document"), tmp_path, client=scenario.client)
    for _ in scenario.steps[:3]:
        h.poll()

    assert PDF not in h.archive.library
    assert h.archive.tombstones[PDF] == expected_files(PDF)
    row = h.index.document(PDF)
    assert row is not None and row.deleted_at is not None
    assert row.deletion_cause == "unknown"
    assert NOTEBOOK in h.archive.library
    assert_safe(h)


def test_partial_listing_tombstones_nothing_and_clears_the_flag(tmp_path: Path) -> None:
    h, player, (_full, partial, again) = replay("partial_listing", tmp_path)

    for report in (partial, again):
        assert report.outcome == "partial"
        assert not report.listing_complete
        assert report.tombstoned == ()
        assert report.failed == {NOTEBOOK: "transient"}
    assert h.index.state["last_full_listing_ok"] == "false"
    assert "last_root_hash" not in h.index.state
    assert json.loads(h.index.state["previous_listing"])["complete"] is False
    # The PDF is gone from the cloud but stays in the archive; the notebook keeps v1.
    assert h.archive.library[PDF] == expected_files(PDF)
    assert h.archive.library[NOTEBOOK] == expected_files(NOTEBOOK)
    assert "tombstone" not in h.archive.op_names()
    assert player.unplayed() == 0
    assert_safe(h)


def test_mass_disappearance_trips_the_breaker_and_then_stays_read_only(tmp_path: Path) -> None:
    scenario = SCENARIOS["mass_delete"]
    player = Player.named("mass_delete")
    h = harness(player, tmp_path, client=scenario.client)
    first, second, third = (h.poll() for _ in range(3))
    requests_before = len(player.played)
    fourth = h.poll()

    assert len(first.committed) == 9
    assert second.outcome == "ok" and second.tombstoned == ()
    assert third.outcome == "anomaly"
    assert third.anomaly == "mass_delete"
    assert third.anomaly_reason == "6 of 9 documents missing from the root"
    assert third.tombstoned == ()
    assert h.index.state["anomaly"] == "mass_delete"
    assert "tombstone" not in h.archive.op_names()
    assert len(h.archive.library) == 9
    # Safe mode: the next poll makes no request at all.
    assert fourth.outcome == "anomaly" and fourth.reason == "mass_delete"
    assert len(player.played) == requests_before
    assert_safe(h)


def test_the_breaker_poll_commits_nothing_either(tmp_path: Path) -> None:
    cloud = FakeCloud()
    nine(cloud)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    h.poll()
    cloud.remove_document(*PAGES[:6])
    h.poll()
    cloud.set_file(NOTEBOOK, PAGE_101, fixture_documents()[PDF][PDF_PAGE])
    commits_before = h.archive.op_names().count("commit")

    report = h.poll()

    assert report.anomaly == "mass_delete"
    assert report.committed == ()
    assert h.archive.op_names().count("commit") == commits_before
    assert h.archive.library[NOTEBOOK] == expected_files(NOTEBOOK)


# ---- anomalies -------------------------------------------------------------------------


def test_generation_regression_is_an_anomaly(tmp_path: Path) -> None:
    h, player, (_full, regressed, after) = replay("generation_regressed", tmp_path)

    assert regressed.outcome == "anomaly"
    assert regressed.anomaly == "generation_regressed"
    assert regressed.committed == () and regressed.tombstoned == ()
    assert h.index.state["anomaly"] == "generation_regressed"
    assert after.outcome == "anomaly"
    assert len(h.archive.library) == 2
    assert player.unplayed() == 0
    assert_safe(h)


def test_empty_root_after_a_full_one_is_an_anomaly(tmp_path: Path) -> None:
    h, _player, (_full, empty) = replay("empty_root", tmp_path)

    assert empty.outcome == "anomaly"
    assert empty.anomaly == "empty_root"
    assert empty.tombstoned == ()
    assert len(h.archive.library) == 2
    assert_safe(h)


def clear_anomaly(h: Harness, *, allow_mass_delete: bool = False) -> None:
    """What ``admin_clear_anomaly`` leaves in ``sync_state`` (Architecture §7 step 4)."""
    with h.index.transaction():
        h.index.set_state("anomaly", None)
        h.index.set_state("anomaly_reason", None)
        if allow_mass_delete:
            h.index.set_state("allow_mass_delete", "true")


def test_a_cleared_generation_regression_resumes_and_still_needs_two_listings(
    tmp_path: Path,
) -> None:
    cloud = FakeCloud()
    both(cloud)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    h.poll()
    cloud.publish(generation=2**63 - 1)  # one bogus pointer is believed...
    assert h.poll().outcome == "ok"
    cloud.publish(generation=5)  # ...so the genuine root now looks regressed

    regressed = h.poll()

    assert regressed.anomaly == "generation_regressed"
    assert regressed.anomaly_reason == f"root generation 5 is lower than {2**63 - 1}"
    assert h.index.state["last_generation"] == "5"
    assert json.loads(h.index.state["previous_listing"])["complete"] is False

    clear_anomaly(h)  # an administrator looked: without this the account stays read-only
    cloud.remove_document(PDF)
    resumed = h.poll()
    assert resumed.outcome == "ok" and resumed.tombstoned == ()  # the previous was unlisted
    assert h.poll().tombstoned == (PDF,)
    assert_safe(h)


def test_a_cleared_empty_root_is_believed_only_after_two_complete_listings(
    tmp_path: Path,
) -> None:
    cloud = FakeCloud()
    both(cloud)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    h.poll()
    cloud.remove_document(NOTEBOOK, PDF)
    assert h.poll().anomaly == "empty_root"
    assert "tombstone" not in h.archive.op_names()

    clear_anomaly(h)
    first = h.poll()
    assert first.outcome == "ok" and first.tombstoned == ()
    second = h.poll()

    # Two documents are under the breaker's floor: they are moved, never deleted.
    assert second.tombstoned == (NOTEBOOK, PDF)
    assert h.archive.tombstones[PDF] == expected_files(PDF)
    assert h.archive.library == {}
    assert_safe(h)


def test_an_approved_mass_delete_is_applied_once(tmp_path: Path) -> None:
    cloud = FakeCloud()
    nine(cloud)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    h.poll()
    cloud.remove_document(*PAGES[:6])
    h.poll()
    assert h.poll().anomaly == "mass_delete"

    clear_anomaly(h)  # cleared without approval: the same candidates trip it again
    again = h.poll()
    assert again.anomaly == "mass_delete" and not again.mass_delete_approved
    assert "tombstone" not in h.archive.op_names()

    clear_anomaly(h, allow_mass_delete=True)
    approved = h.poll()

    assert approved.outcome == "ok" and approved.mass_delete_approved
    assert approved.tombstoned == tuple(sorted(PAGES[:6]))
    assert "allow_mass_delete" not in h.index.state  # consumed
    assert len(h.archive.library) == 3 and len(h.archive.tombstones) == 6
    assert_safe(h)


def test_an_approval_is_consumed_by_the_next_listing_even_unused(tmp_path: Path) -> None:
    cloud = FakeCloud()
    both(cloud)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    h.index.state["allow_mass_delete"] = "true"

    report = h.poll()

    assert report.mass_delete_approved and report.tombstoned == ()
    assert "allow_mass_delete" not in h.index.state


def test_an_approval_survives_a_poll_that_never_reached_the_root(tmp_path: Path) -> None:
    cloud = FakeCloud()
    both(cloud)
    cloud.status[ROOT_KEY] = 503
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    h.index.state["allow_mass_delete"] = "true"

    assert h.poll().outcome == "backoff"
    assert h.index.state["allow_mass_delete"] == "true"


def test_the_index_blob_is_kept_with_each_version(tmp_path: Path) -> None:
    cloud = FakeCloud()
    both(cloud)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    h.poll()

    for uuid in (NOTEBOOK, PDF):
        doc_index = h.client.document_index(uuid, cloud.doc_hash(uuid))
        stored = h.archive.blob_path(hashlib.sha256(doc_index.body).hexdigest())
        assert stored.read_bytes() == doc_index.body
        # The fake cloud addresses document indexes by HashEntries, as S1 saw, so the
        # stored body's name is not the history row's hash_root (see the hand-back).
        assert stored.name != h.index.history_of(uuid)[0][1]


def test_the_library_is_enumerated_page_by_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync_module, "_LIST_PAGE", 2)
    cloud = FakeCloud()
    nine(cloud)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    h.poll()
    calls = h.index.recent_calls
    cloud.remove_document(PAGES[0])
    h.poll()
    report = h.poll()

    assert report.tombstoned == (PAGES[0],)
    assert h.index.recent_calls - calls == 2 * 5  # nine rows, two per page, per poll


def test_uppercase_hashes_from_the_cloud_are_stored_lowercase(tmp_path: Path) -> None:
    cloud = FakeCloud()
    both(cloud)

    def shout(request: httpx.Request) -> httpx.Response:
        response = cloud.handler(request)
        name = request.headers.get("rm-filename", "")
        if str(request.url) == ROOT_URL:
            pointer = json.loads(response.content)
            pointer["hash"] = pointer["hash"].upper()
            return httpx.Response(200, json=pointer)
        if name.endswith(".docSchema") and name != "root.docSchema":
            lines = response.content.decode().split("\n")
            loud = [ln[:64].upper() + ln[64:] if ln[64:65] == ":" else ln for ln in lines]
            return httpx.Response(200, content="\n".join(loud).encode())
        return response

    h = harness(httpx.MockTransport(shout), tmp_path)

    report = h.poll()

    assert report.outcome == "ok" and report.root_hash == cloud.root_hash
    for uuid in (NOTEBOOK, PDF):
        assert h.archive.library[uuid] == expected_files(uuid)
        assert h.index.history_of(uuid) == [(cloud.generation, cloud.doc_hash(uuid))]


def test_unparseable_root_is_a_protocol_anomaly(tmp_path: Path) -> None:
    cloud = FakeCloud()
    both(cloud)
    cloud.corrupt[cloud.root_hash] = b"4\nnot an index\n"
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)

    report = h.poll()

    assert report.outcome == "anomaly"
    assert report.anomaly == "protocol"
    assert h.index.state["anomaly"] == "protocol"
    assert h.archive.ops == []


def test_an_existing_anomaly_short_circuits_without_a_request(tmp_path: Path) -> None:
    cloud = FakeCloud()
    both(cloud)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    h.index.state["anomaly"] = "mass_delete"

    report = h.poll()

    assert report.outcome == "anomaly"
    assert cloud.requests == []


# ---- per-document failures -------------------------------------------------------------


def test_blob_hash_mismatch_fails_that_document_only(tmp_path: Path) -> None:
    h, _player, (report,) = replay("hash_mismatch", tmp_path)
    bad_sha = expected_files(NOTEBOOK)[PAGE_101]

    assert report.outcome == "partial"
    assert report.failed == {NOTEBOOK: "hash_mismatch"}
    assert report.committed == (PDF,)
    assert report.listing_complete
    assert NOTEBOOK not in h.archive.library
    assert h.index.document(NOTEBOOK) is None
    assert h.index.history_of(NOTEBOOK) == []
    assert not h.archive.blob_path(bad_sha).exists()
    assert h.archive.library[PDF] == expected_files(PDF)
    assert "last_root_hash" not in h.index.state  # the next poll tries again
    assert_safe(h)


def test_oversized_document_index_is_refused(tmp_path: Path) -> None:
    h, _player, (report,) = replay("full_listing", tmp_path, max_index_bytes=800)

    assert report.outcome == "partial"
    assert report.failed == {NOTEBOOK: "too_large"}
    assert report.committed == (PDF,)
    assert not report.listing_complete
    assert NOTEBOOK not in h.archive.library
    assert_safe(h)


# ---- failure classes (§7 step 7) ---------------------------------------------------------


@pytest.mark.parametrize("name", ["token_rejected_401", "token_rejected_403"])
def test_token_rejection(name: str, tmp_path: Path) -> None:
    h, player, (report,) = replay(name, tmp_path)

    assert report.outcome == "token_rejected"
    assert [method for method, _url in player.played] == ["POST"]
    assert h.archive.ops == [] and h.index.state == {}


@pytest.mark.parametrize(
    ("name", "reason", "retry_after"),
    [
        ("token_unavailable", "transient", None),
        ("rate_limited", "rate_limited", 120.0),
        ("server_unavailable", "transient", None),
        ("network_error", "transient", None),
    ],
)
def test_outages_back_off(
    name: str, reason: str, retry_after: float | None, tmp_path: Path
) -> None:
    h, _player, (report,) = replay(name, tmp_path)

    assert report.outcome == "backoff"
    assert report.reason == reason
    assert report.retry_after_s == retry_after
    assert h.archive.ops == [] and h.index.state == {}


def test_three_transient_index_failures_end_the_poll(tmp_path: Path) -> None:
    h, player, (report,) = replay("index_503", tmp_path)

    assert report.outcome == "backoff"
    assert set(report.failed) == {NOTEBOOK, PDF, PAGES[0]}
    assert report.committed == ()
    assert not report.listing_complete
    # The fourth document's index was never asked for.
    assert sum(1 for _m, url in player.played if "/sync/v3/files/" in url) == 4  # root + 3
    assert h.archive.ops == []


def test_rate_limit_mid_listing_stops_all_requests(tmp_path: Path) -> None:
    cloud = FakeCloud()
    both(cloud)
    cloud.status[cloud.doc_hash(NOTEBOOK)] = 429
    cloud.headers[cloud.doc_hash(NOTEBOOK)] = {"retry-after": "30"}
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)

    report = h.poll()

    assert report.outcome == "backoff"
    assert report.reason == "rate_limited"
    assert report.retry_after_s == 30.0
    assert cloud.doc_hash(PDF) not in cloud.targets()
    assert h.archive.ops == []


# ---- crash safety ------------------------------------------------------------------------


@pytest.mark.parametrize(("point", "after"), [("put_blob", 3), ("commit", 0)])
def test_crash_between_stage_and_commit_leaves_no_partial_document(
    point: str, after: int, tmp_path: Path
) -> None:
    cloud = FakeCloud()
    both(cloud)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    h.archive.inject(point, SimulatedCrash(), after=after)

    with pytest.raises(SimulatedCrash):
        h.poll()

    assert NOTEBOOK not in h.archive.library  # sorted first: the crash hit it
    assert h.index.document(NOTEBOOK) is None
    assert h.index.history_of(NOTEBOOK) == []
    assert len(h.archive.staging) == 1  # left behind, as by a killed process
    assert "last_root_hash" not in h.index.state

    assert h.archive.sweep_staging() == 1  # what the worker does on start
    report = h.poll()

    assert report.outcome == "ok"
    assert sorted(report.committed) == [NOTEBOOK, PDF]
    assert h.archive.library[NOTEBOOK] == expected_files(NOTEBOOK)
    assert report.blobs_downloaded == 14 - (after if point == "put_blob" else 9)
    assert len(h.index.history_of(NOTEBOOK)) == 1
    assert_safe(h)


def test_parallel_blob_downloads_reach_the_same_state(tmp_path: Path) -> None:
    cloud = FakeCloud()
    both(cloud)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path, settings=SyncSettings(sync_workers=4))

    report = h.poll()

    assert report.outcome == "ok"
    assert h.archive.library[NOTEBOOK] == expected_files(NOTEBOOK)
    assert h.archive.library[PDF] == expected_files(PDF)
    assert report.blobs_downloaded == 14


def test_unchanged_documents_are_not_fetched_again(tmp_path: Path) -> None:
    cloud = FakeCloud()
    both(cloud)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    h.poll()
    cloud.requests.clear()
    cloud.set_file(NOTEBOOK, PAGE_101, fixture_documents()[PDF][PDF_PAGE])

    report = h.poll()

    assert report.committed == (NOTEBOOK,)
    assert report.documents_unchanged == 1
    assert cloud.doc_hash(PDF) not in cloud.targets()
    assert cloud.targets().count(ROOT_KEY) == 1


# ---- untrusted cloud data ----------------------------------------------------------------


def _metadata(**fields: object) -> bytes:
    return json.dumps(fields).encode()


def test_hostile_metadata_becomes_inert_display_data(tmp_path: Path) -> None:
    cloud = FakeCloud()
    uuid = PAGES[0]
    cloud.put_documents(
        {
            uuid: {
                f"{uuid}.metadata": _metadata(
                    visibleName="evil\x00\x1b[31m\ud800\u202ename\n" + "x" * 5000,
                    parent="../../etc",
                    type="'; DROP TABLE documents; --",
                    lastModified="not a number",
                    lastOpened=10**30,
                ),
                f"{uuid}.content": b"[1, 2, 3]",
            }
        }
    )
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)

    report = h.poll()

    row = h.index.document(uuid)
    assert row is not None
    assert row.name.startswith("evil[31mname") and len(row.name) == 1024
    assert not any(ord(c) < 32 for c in row.name)
    row.name.encode("utf-8")  # no lone surrogate left
    assert row.parent is None
    assert row.type == "DocumentType"
    # No usable lastModified: the row records when sync saw this version instead.
    assert row.last_modified == h.index.history[0][3] and row.last_opened is None
    assert row.page_count == 0 and row.source_kind == "notebook"
    assert report.metadata_errors == 1  # the .content was not an object
    assert report.outcome == "partial"


@pytest.mark.parametrize(
    "content",
    [
        {"fileType": ["pdf"]},
        {"fileType": {"a": 1}},
        {"fileType": None, "pageCount": True},
        {"cPages": {"pages": {"x": 1}}},
        {"cPages": [1, 2], "pages": "abc"},
    ],
)
def test_odd_content_types_never_crash_the_poll(tmp_path: Path, content: object) -> None:
    cloud = FakeCloud()
    docs = fixture_documents()
    docs[NOTEBOOK][f"{NOTEBOOK}.content"] = json.dumps(content).encode()
    cloud.put_documents(docs)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)

    report = h.poll()

    assert set(report.committed) == {NOTEBOOK, PDF}
    row = h.index.document(NOTEBOOK)
    assert row is not None and row.source_kind == "notebook" and row.page_count >= 0


def test_an_unexpected_error_fails_one_document_not_the_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = FakeCloud()
    both(cloud)
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)
    real = sync_module._content_facts

    def boom(uuid: str, *args: Any) -> Any:
        if uuid == NOTEBOOK:
            raise TypeError("a case nobody anticipated")
        return real(uuid, *args)

    monkeypatch.setattr(sync_module, "_content_facts", boom)

    report = h.poll()

    assert report.outcome == "partial"
    assert report.failed == {NOTEBOOK: "internal_error"}
    assert report.committed == (PDF,)
    assert h.index.document(NOTEBOOK) is None and NOTEBOOK not in h.archive.library
    assert h.index.get_state("previous_listing") is not None  # the poll finished


def test_file_names_outside_the_allowlist_never_reach_the_archive(tmp_path: Path) -> None:
    cloud = FakeCloud()
    files = dict(fixture_documents()[PDF])
    data = files[f"{PDF}.pagedata"]
    files.update(
        {
            "../../escape": data,
            f"{PDF}/../../escape.rm": data,
            f"/{PDF}.pdf": data,
            f"{NOTEBOOK}.metadata": data,  # another document's file
            f"{PDF}.content\\..\\x": data,
            f"{PDF}.exe": data,
        }
    )
    cloud.put_documents({PDF: files})
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)

    report = h.poll()

    assert report.skipped_files == 6
    assert report.outcome == "partial"
    assert h.archive.library[PDF] == expected_files(PDF)


def test_root_entries_that_are_not_uuids_are_rejected(tmp_path: Path) -> None:
    cloud = FakeCloud()
    docs = fixture_documents()
    cloud.put_documents({**docs, "../../not-a-uuid": {"x.metadata": b"{}"}})
    h = harness(httpx.MockTransport(cloud.handler), tmp_path)

    report = h.poll()

    assert report.rejected_entries == 1
    assert report.documents_listed == 2
    assert sorted(h.archive.library) == [NOTEBOOK, PDF]
    assert report.outcome == "partial"


def test_aged_deletion_cause_when_enabled(tmp_path: Path) -> None:
    cloud = FakeCloud()
    both(cloud)
    h = harness(
        httpx.MockTransport(cloud.handler),
        tmp_path,
        settings=SyncSettings(sync_workers=1, aged_deletion_cause=True),
    )
    h.clock.now = datetime(2027, 6, 1, tzinfo=UTC)  # long after the fixture's lastOpened
    h.poll()
    cloud.remove_document(PDF)
    h.poll()
    report = h.poll()

    assert report.tombstoned == (PDF,)
    row = h.index.document(PDF)
    assert row is not None and row.deletion_cause == "aged"


def test_log_lines_carry_no_identifiers_or_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    replay("hash_mismatch", tmp_path / "a")
    replay("mass_delete", tmp_path / "b")

    ours = [r for r in caplog.records if r.name.startswith("scriptorium")]
    assert ours
    for record in ours:
        text = record.getMessage() + json.dumps(getattr(record, "scriptorium_fields", {}))
        assert not _UUID_RE.search(text)
        assert "Scriptorium Test" not in text
        assert "token" not in text


# ---- helpers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        "[]",
        '{"complete": "yes", "generation": 1, "uuids": []}',
        '{"complete": true, "generation": true, "uuids": []}',
        '{"complete": true, "generation": 1, "uuids": ["../x"]}',
    ],
)
def test_an_unusable_previous_listing_counts_as_none(raw: str | None) -> None:
    assert sync_module._load_listing(raw) is None


def test_settings_are_bounded() -> None:
    with pytest.raises(ValueError):
        SyncSettings(sync_workers=0)
    with pytest.raises(ValueError):
        SyncSettings(tombstone_max_per_poll=-1)


def test_clock_is_injected(tmp_path: Path) -> None:
    h, _player, (report,) = replay("full_listing", tmp_path)
    assert isinstance(h.clock, Clock)
    assert report.started_at < report.finished_at
