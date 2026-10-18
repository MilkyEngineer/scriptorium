"""The worker's OCR (Plan 05 §3.2): the auto-OCR scan, ``ocr`` and ``ocr_batch`` jobs.

A real worker, index and queue against the fake cloud, with F's seams faked
(``tests/fakes/ocr.py``): no Claude API call is made. These are §5 checks 1 and 2: ten polls
give one transcription per content hash, and a page that changes on every poll is not
transcribed until it stops changing.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from scriptorium.jobs.kinds import OCR_KINDS, OcrSpec
from scriptorium.jobs.queue import JobRow
from scriptorium.library import pages as pg
from scriptorium.ocr._ports import BATCH_DISCOUNT
from scriptorium.ocr.debounce import STATE_OCR_POLL_AT
from scriptorium.ocr.pipeline import AUTO_OCR_KEY, AutoOcr, dump_auto_ocr, enqueue_page
from scriptorium.users.store import global_db_path
from scriptorium.worker import AccountLoop
from tests.cloud.fake_cloud import NOTEBOOK, PAGES, PDF, fixture_documents
from tests.cloud.scenarios import PAGE_101
from tests.fakes.ocr import (
    FakeOcrClient,
    FakeResolver,
    FakeSpendLedger,
    OcrItemError,
    OcrRefused,
    OcrUnavailable,
    Transcription,
    cost_usd,
    default_text,
    fake_ports,
)
from tests.worker.helpers import Linked, link

FOLDER = "00000000-0000-4000-8000-0000000000f0"
SECRET_TEXT = "synthetic secret shopping list"


def _metadata(name: str, *, parent: str = "", kind: str = "DocumentType") -> bytes:
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


def _move_notebook_into_folder(linked: Linked) -> None:
    files = fixture_documents()[NOTEBOOK]
    meta = json.loads(files[f"{NOTEBOOK}.metadata"])
    meta["parent"] = FOLDER
    files[f"{NOTEBOOK}.metadata"] = json.dumps(meta).encode()
    linked.cloud.put_documents(
        {
            FOLDER: {
                f"{FOLDER}.metadata": _metadata("Inbox", kind="CollectionType"),
                f"{FOLDER}.content": b"{}",
            },
            NOTEBOOK: files,
        }
    )


class Harness:
    def __init__(
        self, linked: Linked, client: FakeOcrClient, ledger: FakeSpendLedger, resolver: FakeResolver
    ) -> None:
        self.linked = linked
        self.client = client
        self.ledger = ledger
        self.resolver = resolver
        self.loop: AccountLoop | None = None

    def enable(self, *folders: str) -> None:
        self.linked.index().set_setting(AUTO_OCR_KEY, dump_auto_ocr(AutoOcr(True, folders)))

    async def poll(self, times: int = 1) -> None:
        """Poll, then run every render job and every leasable OCR job."""
        linked = self.linked
        if self.loop is None:
            self.loop = linked.loop()
        for _ in range(times):
            await self.loop.poll_once(linked.context())
            await linked.worker.drain_jobs()
            await linked.worker.drain_jobs(kinds=OCR_KINDS)

    def jobs(self, kind: str = "ocr") -> list[JobRow]:
        rows = self.linked.queue._conn.execute(
            "SELECT id FROM jobs WHERE kind = ? ORDER BY id", (kind,)
        ).fetchall()
        out = []
        for row in rows:
            job = self.linked.queue.get(int(row[0]), self.linked.uid)
            assert job is not None
            out.append(job)
        return out

    def transcribed(self) -> dict[str, str]:
        index = self.linked.index()
        out = {}
        for page in index.pages(NOTEBOOK):
            t = index.transcript(NOTEBOOK, page.page_id, page.content_hash)
            if t is not None:
                out[page.page_id] = t.text
        return out


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    linked = link(tmp_path)
    client, ledger, resolver = FakeOcrClient(), FakeSpendLedger(), FakeResolver()
    linked.worker.ocr = fake_ports(client=client, ledger=ledger, resolver=resolver)
    _move_notebook_into_folder(linked)
    h = Harness(linked, client, ledger, resolver)
    h.enable(FOLDER)
    yield h
    linked.close()


# ---- §5 check 1: one transcription per content hash ------------------------------------------


async def test_ten_polls_transcribe_each_page_version_once(harness: Harness) -> None:
    await harness.poll()
    # First sight of every page: nothing is stable yet.
    assert harness.client.calls() == 0 and harness.jobs() == []
    assert harness.linked.index().get_state(STATE_OCR_POLL_AT) is not None
    await harness.poll(9)
    assert harness.client.calls() == len(PAGES)
    assert len(harness.jobs()) == len(PAGES)
    assert all(j.result is not None and j.result["state"] == "stored" for j in harness.jobs())
    transcripts = harness.transcribed()
    assert set(transcripts) == set(PAGES)
    # Spend: one ledger row per page, priced by F's cost_usd, under the resolved mode.
    assert harness.ledger.pages() == len(PAGES)
    expected = cost_usd(harness.linked.settings, 1500, 200)
    assert all(
        r.mode == "server" and r.cost_usd == pytest.approx(expected) for r in harness.ledger.rows
    )
    index = harness.linked.index()
    page = index.page(NOTEBOOK, 0)
    assert page is not None
    row = index.transcript(NOTEBOOK, page.page_id, page.content_hash)
    assert row is not None
    assert (row.mode, row.model, row.prompt_version) == ("server", "claude-fake-1", "v1")
    assert (row.input_tokens, row.output_tokens) == (1500, 200)
    assert row.cost_usd == pytest.approx(expected)
    # The PDF at the root is outside the auto-OCR folders.
    (pdf_page,) = index.pages(PDF)
    assert index.transcript(PDF, pdf_page.page_id, pdf_page.content_hash) is None


async def test_force_re_transcribes_under_an_after_key(harness: Harness) -> None:
    await harness.poll(2)
    assert harness.client.calls() == len(PAGES)
    page = harness.linked.index().page(NOTEBOOK, 2)
    assert page is not None
    version = pg.current_version(harness.linked.paths, harness.linked.index(), NOTEBOOK)
    assert version is not None
    spec = OcrSpec(NOTEBOOK, page.page_id, page.content_hash, version.generation, force=True)
    harness.client.text = lambda png: "a better transcript"
    job, how = enqueue_page(
        harness.linked.queue,
        uid=harness.linked.uid,
        acct=harness.linked.acct,
        spec=spec,
        retry_failed=False,
    )
    assert job is not None and how == "rerun" and ":after:" in job.dedupe_key
    assert await harness.linked.worker.drain_jobs(kinds=OCR_KINDS) == 1
    assert harness.client.calls() == len(PAGES) + 1
    assert harness.transcribed()[page.page_id] == "a better transcript"
    # Without force the same page version is never sent again.
    await harness.poll(3)
    assert harness.client.calls() == len(PAGES) + 1


# ---- §5 check 2: no OCR while a page keeps changing -------------------------------------------


async def test_a_page_that_changes_every_poll_waits_until_it_is_stable(harness: Harness) -> None:
    docs = fixture_documents()[NOTEBOOK]
    variants = [docs[f"{NOTEBOOK}/{PAGES[1]}.rm"], docs[f"{NOTEBOOK}/{PAGES[2]}.rm"]]
    for i in range(10):
        harness.linked.cloud.set_file(NOTEBOOK, PAGE_101, variants[i % 2])
        await harness.poll()
    transcripts = harness.transcribed()
    assert PAGES[0] not in transcripts
    assert set(transcripts) == set(PAGES[1:])
    assert harness.client.calls() == len(PAGES) - 1
    assert not any(j.payload["page"] == PAGES[0] for j in harness.jobs())
    # It stops changing: the next poll finds the version the previous poll saw, and
    # transcribes it, once.
    await harness.poll()
    assert PAGES[0] in harness.transcribed()
    await harness.poll(3)
    assert harness.client.calls() == len(PAGES)


async def test_thirty_minutes_unchanged_is_stable_without_a_complete_poll(
    harness: Harness,
) -> None:
    await harness.poll()
    harness.linked.index().set_state(STATE_OCR_POLL_AT, None)
    harness.linked.clock.now += timedelta(minutes=31)
    await harness.linked.worker.ocr_runner.scan(harness.linked.context(), complete=False)
    assert len(harness.jobs()) == len(PAGES)


# ---- the mode and the caps --------------------------------------------------------------------


async def test_nothing_is_queued_without_a_key_or_with_auto_ocr_off(harness: Harness) -> None:
    harness.resolver.mode = "lazy"
    await harness.poll(3)
    assert harness.jobs() == [] and harness.client.calls() == 0
    harness.resolver.mode = "server"
    harness.linked.index().set_setting(AUTO_OCR_KEY, dump_auto_ocr(AutoOcr(False, (FOLDER,))))
    await harness.poll(2)
    assert harness.jobs() == [] and harness.client.calls() == 0
    harness.enable()  # enabled with no folders
    await harness.poll(2)
    assert harness.jobs() == []


async def test_without_ports_the_worker_stays_lazy(harness: Harness) -> None:
    harness.linked.worker.ocr = None
    await harness.poll(3)
    assert harness.jobs() == []
    # A job queued anyway (by an older configuration) is deferred, not run.
    harness.linked.worker.ocr = fake_ports(client=harness.client)
    await harness.linked.worker.ocr_runner.scan(harness.linked.context(), complete=False)
    harness.linked.worker.ocr = None
    job = harness.jobs()[0]
    assert job.state == "queued"
    await harness.linked.worker.drain_jobs(kinds=OCR_KINDS)
    done = harness.linked.queue.get(job.id, harness.linked.uid)
    assert done is not None and done.result == {"state": "deferred", "reason": "not_configured"}


async def test_spend_stops_at_the_cap(harness: Harness) -> None:
    # Room for two pages at the scan's generous estimate, then the cap is in sight.
    harness.ledger.user_cap = 0.08
    await harness.poll(10)
    assert 0 < harness.ledger.pages() < len(PAGES)
    spent = harness.ledger.month(
        uid=harness.linked.uid, mode="server", at=harness.linked.clock.now
    ).cost_usd
    assert spent <= 0.08
    calls = harness.client.calls()
    # Degraded: nothing more is queued, and a queued job is deferred.
    harness.ledger.user_cap = spent
    await harness.poll(3)
    assert harness.client.calls() == calls


async def test_a_deferred_page_is_queued_again_once_a_key_is_back(harness: Harness) -> None:
    await harness.poll()
    harness.resolver.mode = "lazy"
    index = harness.linked.index()
    page = index.page(NOTEBOOK, 0)
    version = pg.current_version(harness.linked.paths, index, NOTEBOOK)
    assert page is not None and version is not None
    spec = OcrSpec(NOTEBOOK, page.page_id, page.content_hash, version.generation)
    job, _ = enqueue_page(
        harness.linked.queue,
        uid=harness.linked.uid,
        acct=harness.linked.acct,
        spec=spec,
        retry_failed=False,
    )
    assert job is not None
    await harness.linked.worker.drain_jobs(kinds=OCR_KINDS)
    deferred = harness.linked.queue.get(job.id, harness.linked.uid)
    assert deferred is not None and deferred.result == {"state": "deferred", "reason": "no_key"}
    harness.resolver.mode = "server"
    await harness.poll(2)
    assert PAGES[0] in harness.transcribed()


# ---- single jobs ------------------------------------------------------------------------------


async def _one_job(harness: Harness, *, hash_: str | None = None) -> JobRow:
    await harness.poll()
    index = harness.linked.index()
    page = index.page(NOTEBOOK, 0)
    version = pg.current_version(harness.linked.paths, index, NOTEBOOK)
    assert page is not None and version is not None
    spec = OcrSpec(NOTEBOOK, page.page_id, hash_ or page.content_hash, version.generation)
    job, _ = enqueue_page(
        harness.linked.queue,
        uid=harness.linked.uid,
        acct=harness.linked.acct,
        spec=spec,
        retry_failed=False,
    )
    assert job is not None
    await harness.linked.worker.drain_jobs(kinds=OCR_KINDS)
    after = harness.linked.queue.get(job.id, harness.linked.uid)
    assert after is not None
    return after


async def test_a_stale_job_does_nothing(harness: Harness) -> None:
    job = await _one_job(harness, hash_="0" * 64)
    assert (job.state, job.result) == ("done", {"state": "stale"})
    assert harness.client.calls() == 0 and harness.ledger.pages() == 0


async def test_a_refused_page_dies_at_once(harness: Harness) -> None:
    harness.client.errors.append(OcrRefused("image rejected: " + SECRET_TEXT))
    job = await _one_job(harness)
    assert (job.state, job.error, job.attempts) == ("dead", "ocr_refused", 1)
    assert harness.ledger.pages() == 0


async def test_an_unavailable_api_is_retried_with_backoff(harness: Harness) -> None:
    harness.client.errors.append(OcrUnavailable("overloaded"))
    job = await _one_job(harness)
    assert (job.state, job.error) == ("failed", "ocr_unavailable")
    harness.linked.clock.now += timedelta(minutes=5)
    await harness.linked.worker.drain_jobs(kinds=OCR_KINDS)
    done = harness.linked.queue.get(job.id, harness.linked.uid)
    assert done is not None and done.state == "done" and done.result is not None
    assert done.result["state"] == "stored"


async def test_an_oversized_transcript_is_billed_and_not_stored(harness: Harness) -> None:
    harness.client.text = lambda png: "x" * (31 * 1024)
    job = await _one_job(harness)
    assert (job.state, job.error) == ("dead", "too_large")
    assert harness.ledger.pages() == 1
    assert harness.transcribed() == {}


async def test_transcripts_never_reach_jobs_or_logs(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="scriptorium")
    harness.client.text = lambda png: SECRET_TEXT
    await harness.poll(3)
    assert harness.transcribed()
    dump = harness.linked.queue._conn.execute("SELECT * FROM jobs").fetchall()
    assert SECRET_TEXT not in json.dumps([list(r) for r in dump], default=str)
    for record in caplog.records:
        assert SECRET_TEXT not in record.getMessage()
        assert SECRET_TEXT not in json.dumps(record.__dict__, default=str)
    raw = global_db_path(harness.linked.settings.data_dir).read_bytes()
    assert SECRET_TEXT.encode() not in raw


# ---- batches ----------------------------------------------------------------------------------


@pytest.fixture
def batching(tmp_path: Path) -> Iterator[Harness]:
    linked = link(tmp_path, settings={"ocr_batch_min": 3})
    client, ledger, resolver = FakeOcrClient(), FakeSpendLedger(), FakeResolver()
    linked.worker.ocr = fake_ports(client=client, ledger=ledger, resolver=resolver)
    _move_notebook_into_folder(linked)
    h = Harness(linked, client, ledger, resolver)
    h.enable(FOLDER)
    yield h
    linked.close()


async def _until_ended(h: Harness, batch_id: str) -> None:
    h.client.states[batch_id] = "ended"
    h.linked.clock.now += timedelta(hours=2)
    await h.linked.worker.drain_jobs(kinds=OCR_KINDS)


async def test_more_than_the_minimum_goes_as_one_batch(batching: Harness) -> None:
    h = batching
    await h.poll(2)
    (batch_id,) = h.client.batches
    assert [i.custom_id for i in h.client.batches[batch_id]] == [f"p{n}" for n in range(7)]
    assert h.client.transcribed == [] and h.jobs() == []
    (job,) = h.jobs("ocr_batch")
    assert job.dedupe_key == f"ocr_batch:{h.linked.acct}:{batch_id}"
    assert job.payload["mode"] == "server" and len(job.payload["items"]) == 7
    # Still running: polled with backoff, and the scan does not send its pages again.
    h.linked.clock.now += timedelta(minutes=2)
    await h.poll(3)
    polled = h.linked.queue.get(job.id, h.linked.uid)
    assert polled is not None and polled.state == "failed" and polled.error == "batch_pending"
    assert h.client.status_calls >= 1 and h.client.calls() == 7
    await _until_ended(h, batch_id)
    done = h.linked.queue.get(job.id, h.linked.uid)
    assert done is not None and done.state == "done" and done.result is not None
    assert done.result["stored"] == 7 and done.result["fallback"] == 0
    assert set(h.transcribed()) == set(PAGES)
    # Spend at the batch price, one row per page, under the batch's mode.
    full = cost_usd(h.linked.settings, 1500, 200)
    assert h.ledger.pages() == 7
    assert all(r.cost_usd == pytest.approx(full * BATCH_DISCOUNT) for r in h.ledger.rows)
    await h.poll(5)
    assert h.client.calls() == 7


async def test_batch_results_are_checked_against_the_jobs_items(batching: Harness) -> None:
    h = batching
    await h.poll(2)
    (batch_id,) = h.client.batches
    items = h.client.batches[batch_id]
    good = Transcription(default_text(items[0].png))
    h.client.results[batch_id] = [
        ("p0", good),
        ("p0", Transcription("a second answer for p0")),
        ("p77", Transcription("not one of ours")),
        ("p1", OcrItemError("overloaded_error")),
        ("p2", Transcription("x" * (31 * 1024))),
    ]
    await _until_ended(h, batch_id)  # runs the batch job, then the single jobs it queued
    (job,) = h.jobs("ocr_batch")
    assert job.result is not None
    assert (job.result["stored"], job.result["unexpected"]) == (1, 2)
    # p1 (an item error), p2 (too large) and p3..p6 (missing) fall back to single jobs.
    assert job.result["fallback"] == 6
    full = cost_usd(h.linked.settings, 1500, 200)
    batch_rows = [r for r in h.ledger.rows if r.cost_usd == pytest.approx(full * BATCH_DISCOUNT)]
    assert len(batch_rows) == 2  # p0 and the oversized p2 were billed at the batch price
    assert len(h.client.transcribed) == 6 and len(h.ledger.rows) == 8
    transcripts = h.transcribed()
    assert set(transcripts) == set(PAGES)
    assert transcripts[PAGES[0]] == good.text
    assert "a second answer for p0" not in transcripts.values()
    assert "not one of ours" not in transcripts.values()


async def test_a_batch_whose_key_is_gone_falls_back_to_single_jobs(batching: Harness) -> None:
    h = batching
    await h.poll(2)
    (batch_id,) = h.client.batches

    def no_key(settings: Any, box: Any, mode: str) -> None:
        return None

    h.linked.worker.ocr = dataclasses.replace(fake_ports(client=h.client), batch_key=no_key)
    await _until_ended(h, batch_id)
    (job,) = h.jobs("ocr_batch")
    assert job.result == {"state": "orphaned", "fallback": 7}
    await h.linked.worker.drain_jobs(kinds=OCR_KINDS)
    assert set(h.transcribed()) == set(PAGES)
