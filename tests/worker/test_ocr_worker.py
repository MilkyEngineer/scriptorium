"""The worker's OCR (Plan 05 §3.2): the auto-OCR scan, ``ocr`` and ``ocr_batch`` jobs.

A real worker, index and queue against the fake cloud. Most tests fake F's ledger and resolver
(``tests/fakes/ocr.py``) to set caps and switch modes directly; ``test_the_real_binding_*``
runs F's own ``resolve`` and ``SpendLedger`` through ``build_ocr_ports``. The client is always
the fake: no Claude API call is made. These are §5 checks 1 and 2: ten polls give one
transcription per content hash, and a page that changes on every poll is not transcribed
until it stops changing.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from scriptorium.jobs.kinds import OCR_KINDS, OcrSpec
from scriptorium.jobs.queue import JobRow
from scriptorium.library import pages as pg
from scriptorium.ocr import runner as runner_mod
from scriptorium.ocr._ports import build_ocr_ports
from scriptorium.ocr.client import OcrItemError
from scriptorium.ocr.debounce import STATE_OCR_POLL_AT
from scriptorium.ocr.errors import OcrRefused, OcrUnavailable
from scriptorium.ocr.ledger import SpendLedger, cost_usd
from scriptorium.ocr.modes import USER_KEY_SECRET
from scriptorium.ocr.pipeline import (
    AUTO_OCR_KEY,
    KEY_REFUSED_KEY,
    PENDING_BATCHES_KEY,
    AutoOcr,
    clear_key_refusal,
    dump_auto_ocr,
    enqueue_page,
    note_key_refusal,
)
from scriptorium.ocr.prompt import Prompt
from scriptorium.users.secrets import SecretBox
from scriptorium.users.store import global_db_path
from scriptorium.worker import AccountLoop
from tests.cloud.fake_cloud import NOTEBOOK, PAGES, PDF, fixture_documents
from tests.cloud.scenarios import PAGE_101
from tests.fakes.ocr import (
    DEFAULT_MODEL,
    FakeOcrClient,
    FakeOcrFactory,
    FakeResolver,
    FakeSpendLedger,
    canned,
    default_text,
    fake_ports,
)
from tests.worker.helpers import Linked, link

FOLDER = "00000000-0000-4000-8000-0000000000f0"
SECRET_TEXT = "synthetic secret shopping list"
SERVER_KEY = "sk-ant-server-key-for-tests"
USER_KEY = "sk-ant-users-own-key-for-tests"


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
    client, ledger, resolver = FakeOcrClient(text=default_text), FakeSpendLedger(), FakeResolver()
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
    assert harness.client.pages_sent() == 0 and harness.jobs() == []
    assert harness.linked.index().get_state(STATE_OCR_POLL_AT) is not None
    await harness.poll(9)
    assert harness.client.pages_sent() == len(PAGES)
    assert len(harness.jobs()) == len(PAGES)
    assert all(j.result is not None and j.result["state"] == "stored" for j in harness.jobs())
    transcripts = harness.transcribed()
    assert set(transcripts) == set(PAGES)
    # Spend: one ledger row per page, priced by F's cost_usd, under the resolved mode.
    assert harness.ledger.pages() == len(PAGES)
    expected = cost_usd(harness.linked.settings, 2300, 150)
    assert all(
        r.mode == "server" and r.cost_usd == pytest.approx(expected) for r in harness.ledger.rows
    )
    index = harness.linked.index()
    page = index.page(NOTEBOOK, 0)
    assert page is not None
    row = index.transcript(NOTEBOOK, page.page_id, page.content_hash)
    assert row is not None
    assert (row.mode, row.model, row.prompt_version) == ("server", DEFAULT_MODEL, "v1")
    assert (row.input_tokens, row.output_tokens) == (2300, 150)
    assert row.cost_usd == pytest.approx(expected)
    # The PDF at the root is outside the auto-OCR folders.
    (pdf_page,) = index.pages(PDF)
    assert index.transcript(PDF, pdf_page.page_id, pdf_page.content_hash) is None


async def test_force_re_transcribes_under_an_after_key(harness: Harness) -> None:
    await harness.poll(2)
    assert harness.client.pages_sent() == len(PAGES)
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
    assert harness.client.pages_sent() == len(PAGES) + 1
    assert harness.transcribed()[page.page_id] == "a better transcript"
    # Without force the same page version is never sent again.
    await harness.poll(3)
    assert harness.client.pages_sent() == len(PAGES) + 1


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
    assert harness.client.pages_sent() == len(PAGES) - 1
    assert not any(j.payload["page"] == PAGES[0] for j in harness.jobs())
    # It stops changing: the next poll finds the version the previous poll saw, and
    # transcribes it, once.
    await harness.poll()
    assert PAGES[0] in harness.transcribed()
    await harness.poll(3)
    assert harness.client.pages_sent() == len(PAGES)


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
    assert harness.jobs() == [] and harness.client.pages_sent() == 0
    harness.resolver.mode = "server"
    harness.linked.index().set_setting(AUTO_OCR_KEY, dump_auto_ocr(AutoOcr(False, (FOLDER,))))
    await harness.poll(2)
    assert harness.jobs() == [] and harness.client.pages_sent() == 0
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
    # Room for two pages at the scan's generous estimate ($0.025 at the test prices); each
    # page costs less ($0.0061), so a few more fit before the room is under one estimate.
    harness.ledger.user_cap = 0.05
    await harness.poll(10)
    assert 0 < harness.ledger.pages() < len(PAGES)
    spent = harness.ledger.month(
        uid=harness.linked.uid, mode="server", at=harness.linked.clock.now
    ).cost_usd
    assert spent <= 0.05
    calls = harness.client.pages_sent()
    # Degraded: nothing more is queued, and a queued job is deferred.
    harness.ledger.user_cap = spent
    await harness.poll(3)
    assert harness.client.pages_sent() == calls


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
    assert harness.client.pages_sent() == 0 and harness.ledger.pages() == 0


async def test_a_refused_page_dies_at_once_and_its_billed_tokens_are_recorded(
    harness: Harness,
) -> None:
    harness.client.transcriptions.append(OcrRefused("refused", input_tokens=2100, output_tokens=12))
    job = await _one_job(harness)
    assert (job.state, job.error, job.attempts) == ("dead", "refused", 1)
    (row,) = harness.ledger.rows
    assert (row.mode, row.input_tokens, row.output_tokens) == ("server", 2100, 12)
    assert row.cost_usd == pytest.approx(cost_usd(harness.linked.settings, 2100, 12))
    assert harness.linked.index().get_setting(KEY_REFUSED_KEY) is None  # the page, not the key


async def test_an_image_the_api_would_refuse_is_never_sent(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_mod.batches, "fits", lambda png: "image_too_large")
    job = await _one_job(harness)
    assert (job.state, job.error) == ("dead", "image_too_large")
    assert harness.client.count("transcribe") == 0 and harness.ledger.pages() == 0


async def test_an_unavailable_api_is_retried_with_backoff(harness: Harness) -> None:
    harness.client.transcriptions.append(OcrUnavailable("overloaded"))
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


# ---- a refused key -----------------------------------------------------------------------------


async def test_a_refused_key_fails_the_job_and_pauses_the_account(harness: Harness) -> None:
    h = harness
    h.client.transcriptions.append(OcrRefused("bad_key"))
    first = await _one_job(h)
    assert (first.state, first.error, first.attempts) == ("dead", "key_bad_key", 1)
    note = json.loads(h.linked.index().get_setting(KEY_REFUSED_KEY) or "{}")
    assert (note["mode"], note["reason"]) == ("server", "bad_key")
    assert set(note) == {"mode", "reason", "at"}  # a reason code, never the key
    sent = h.client.pages_sent()
    # While the note applies: the scan queues nothing and no page is sent.
    await h.poll(3)
    assert h.client.pages_sent() == sent
    assert all(j.state in ("dead", "queued") for j in h.jobs())
    # Once it is cleared (a new key, or the hold has passed), the page is adopted again.
    clear_key_refusal(h.linked.index())
    await h.poll(2)
    assert set(h.transcribed()) == set(PAGES)


async def test_a_refused_key_does_not_pause_the_other_mode(harness: Harness) -> None:
    h = harness
    note_key_refusal(h.linked.index(), "user", "bad_key", h.linked.clock.now)
    await h.poll(10)  # the resolver says server: the user key's note does not apply
    assert set(h.transcribed()) == set(PAGES)


async def test_a_queued_job_defers_while_the_key_is_refused(harness: Harness) -> None:
    h = harness
    note_key_refusal(h.linked.index(), "server", "billing", h.linked.clock.now)
    job = await _one_job(h)
    assert (job.state, job.result) == ("done", {"state": "deferred", "reason": "key_refused"})
    assert h.client.pages_sent() == 0


# ---- the real binding -----------------------------------------------------------------------


@pytest.fixture
def bound(tmp_path: Path) -> Iterator[tuple[Linked, FakeOcrFactory]]:
    """F's resolve and SpendLedger, bound by build_ocr_ports; only the client is fake."""
    linked = link(tmp_path, settings={"anthropic_api_key": SERVER_KEY})
    linked.store.set_server_key_allowed(linked.uid, True)
    factory = FakeOcrFactory(FakeOcrClient(text=default_text))
    ledger = SpendLedger.open(global_db_path(linked.settings.data_dir), migrate=False)
    linked.worker.ocr = build_ocr_ports(ledger, client=factory)
    _move_notebook_into_folder(linked)
    linked.index().set_setting(AUTO_OCR_KEY, dump_auto_ocr(AutoOcr(True, (FOLDER,))))
    yield linked, factory
    linked.close()


async def _bound_poll(linked: Linked, times: int) -> None:
    loop = linked.loop()
    for _ in range(times):
        await loop.poll_once(linked.context())
        await linked.worker.drain_jobs()
        await linked.worker.drain_jobs(kinds=OCR_KINDS)


async def test_the_real_binding_spends_the_server_key_into_the_real_ledger(
    bound: tuple[Linked, FakeOcrFactory],
) -> None:
    linked, factory = bound
    await _bound_poll(linked, 10)
    assert factory.client.pages_sent() == len(PAGES)
    assert set(factory.keys) == {SERVER_KEY}
    ports = linked.worker.ocr
    assert ports is not None
    spend = ports.ledger.month(uid=linked.uid, mode="server", at=linked.clock.now)
    assert spend.pages == len(PAGES)
    assert spend.cost_usd == pytest.approx(len(PAGES) * cost_usd(linked.settings, 2300, 150))


async def test_the_real_binding_prefers_the_users_own_key(
    bound: tuple[Linked, FakeOcrFactory],
) -> None:
    linked, factory = bound
    SecretBox(linked.settings, linked.uid).seal(USER_KEY_SECRET, USER_KEY.encode())
    await _bound_poll(linked, 10)
    assert set(factory.keys) == {USER_KEY}
    ports = linked.worker.ocr
    assert ports is not None
    assert ports.ledger.month(uid=linked.uid, mode="user", at=linked.clock.now).pages == 7
    assert ports.ledger.month(uid=linked.uid, mode="server", at=linked.clock.now).pages == 0


async def test_the_real_binding_stops_at_the_users_server_allowance(tmp_path: Path) -> None:
    linked = link(
        tmp_path,
        settings={"anthropic_api_key": SERVER_KEY, "ocr_user_monthly_cap_usd": 0.05},
    )
    try:
        linked.store.set_server_key_allowed(linked.uid, True)
        factory = FakeOcrFactory(FakeOcrClient(text=default_text))
        ledger = SpendLedger.open(global_db_path(linked.settings.data_dir), migrate=False)
        linked.worker.ocr = build_ocr_ports(ledger, client=factory)
        _move_notebook_into_folder(linked)
        linked.index().set_setting(AUTO_OCR_KEY, dump_auto_ocr(AutoOcr(True, (FOLDER,))))
        await _bound_poll(linked, 10)
        assert 0 < factory.client.pages_sent() < len(PAGES)
        spent = ledger.month(uid=linked.uid, mode="server", at=linked.clock.now).cost_usd
        assert spent <= 0.05
    finally:
        linked.close()


# ---- batches ----------------------------------------------------------------------------------


@pytest.fixture
def batching(tmp_path: Path) -> Iterator[Harness]:
    linked = link(tmp_path, settings={"ocr_batch_min": 3})
    client, ledger, resolver = FakeOcrClient(text=default_text), FakeSpendLedger(), FakeResolver()
    linked.worker.ocr = fake_ports(client=client, ledger=ledger, resolver=resolver)
    _move_notebook_into_folder(linked)
    h = Harness(linked, client, ledger, resolver)
    h.enable(FOLDER)
    yield h
    linked.close()


async def _until_ended(h: Harness, batch_id: str, **finish: Any) -> None:
    h.client.finish_batch(batch_id, **finish)
    h.linked.clock.now += timedelta(hours=2)
    await h.linked.worker.drain_jobs(kinds=OCR_KINDS)


async def test_more_than_the_minimum_goes_as_one_batch(batching: Harness) -> None:
    h = batching
    await h.poll(2)
    (batch_id,) = h.client.batches
    assert h.client.batches[batch_id].custom_ids == [f"p{n}" for n in range(7)]
    assert h.client.count("transcribe") == 0 and h.jobs() == []
    (job,) = h.jobs("ocr_batch")
    assert job.dedupe_key == f"ocr_batch:{h.linked.acct}:{batch_id}"
    assert job.payload["mode"] == "server" and len(job.payload["items"]) == 7
    assert job.payload["prompt_version"] == "v1"
    assert h.linked.index().get_setting(PENDING_BATCHES_KEY) is None
    # Still running: polled with backoff, and the scan does not send its pages again.
    h.linked.clock.now += timedelta(minutes=2)
    await h.poll(3)
    polled = h.linked.queue.get(job.id, h.linked.uid)
    assert polled is not None and polled.state == "failed" and polled.error == "batch_pending"
    assert h.client.count("batch_status") >= 1 and h.client.pages_sent() == 7
    await _until_ended(h, batch_id)
    done = h.linked.queue.get(job.id, h.linked.uid)
    assert done is not None and done.state == "done" and done.result is not None
    assert done.result["stored"] == 7 and done.result["fallback"] == 0
    assert set(h.transcribed()) == set(PAGES)
    # Spend at F's batch price (half), one row per page, under the batch's mode.
    half = cost_usd(h.linked.settings, 2300, 150, batch=True)
    assert half == pytest.approx(cost_usd(h.linked.settings, 2300, 150) / 2)
    assert h.ledger.pages() == 7
    assert all(r.mode == "server" and r.cost_usd == pytest.approx(half) for r in h.ledger.rows)
    await h.poll(5)
    assert h.client.pages_sent() == 7


async def test_batch_results_are_checked_against_the_jobs_items(batching: Harness) -> None:
    h = batching
    await h.poll(2)
    (batch_id,) = h.client.batches
    pngs = h.client.batches[batch_id].pngs
    good = canned(default_text(pngs["p0"]), batch=True)
    await _until_ended(  # runs the batch job, then the single jobs it queued
        h,
        batch_id,
        stream=[
            ("p0", good),
            ("p0", canned("a second answer for p0", batch=True)),
            ("p77", canned("not one of ours", batch=True)),
            ("p1", OcrItemError("overloaded", True)),
            ("p2", canned("x" * (31 * 1024), batch=True)),
        ],
    )
    (job,) = h.jobs("ocr_batch")
    assert job.result is not None
    assert (job.result["stored"], job.result["unexpected"]) == (1, 2)
    # p1 (a retryable item error) and p3..p6 (missing) fall back to single jobs; p2's
    # transcript is too large and would be again, so it gets a dead job and no retry.
    assert (job.result["fallback"], job.result["failed"]) == (5, 1)
    half = cost_usd(h.linked.settings, 2300, 150, batch=True)
    batch_rows = [r for r in h.ledger.rows if r.cost_usd == pytest.approx(half)]
    assert len(batch_rows) == 2  # p0 and the oversized p2 were billed at the batch price
    assert h.client.count("transcribe") == 5 and len(h.ledger.rows) == 7
    transcripts = h.transcribed()
    assert set(transcripts) == set(PAGES) - {PAGES[2]}
    assert transcripts[PAGES[0]] == good.text
    assert "a second answer for p0" not in transcripts.values()
    assert "not one of ours" not in transcripts.values()
    (dead,) = [j for j in h.jobs() if j.state == "dead"]
    assert (dead.payload["page"], dead.error) == (PAGES[2], "too_large")
    await h.poll(3)
    assert h.client.pages_sent() == 7 + 5  # nothing is sent again


async def test_a_page_refused_in_a_batch_is_not_sent_again(batching: Harness) -> None:
    h = batching
    await h.poll(2)
    (batch_id,) = h.client.batches
    results = {f"p{n}": canned(f"page {n}", batch=True) for n in range(7)}
    results["p3"] = OcrItemError("refused", False, 1800, 9)
    await _until_ended(h, batch_id, results=results)
    (job,) = h.jobs("ocr_batch")
    assert job.result is not None
    assert (job.result["stored"], job.result["failed"], job.result["fallback"]) == (6, 1, 0)
    assert len(h.ledger.rows) == 7  # the refusal's tokens are billed at the batch price too
    (billed,) = [r for r in h.ledger.rows if r.input_tokens == 1800]
    assert billed.cost_usd == pytest.approx(cost_usd(h.linked.settings, 1800, 9, batch=True))
    await h.poll(3)
    assert h.client.pages_sent() == 7 and h.client.count("transcribe") == 0
    (dead,) = h.jobs()
    assert (dead.state, dead.error, dead.payload["page"]) == ("dead", "refused", PAGES[3])


async def test_prompt_version_is_the_one_the_batch_was_sent_with(batching: Harness) -> None:
    h = batching
    await h.poll(2)
    (batch_id,) = h.client.batches
    ports = h.linked.worker.ocr
    assert ports is not None
    h.linked.worker.ocr = dataclasses.replace(ports, prompt=lambda: Prompt("A newer prompt.", "v2"))
    await _until_ended(h, batch_id)
    index = h.linked.index()
    for page in index.pages(NOTEBOOK):
        row = index.transcript(NOTEBOOK, page.page_id, page.content_hash)
        assert row is not None and row.prompt_version == "v1"


async def test_a_batch_whose_job_was_not_queued_is_adopted_by_the_next_scan(
    batching: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = batching
    queue = h.linked.worker.queue
    real = queue.enqueue
    failures = []

    def flaky(**kwargs: Any) -> JobRow:
        if kwargs["kind"] == "ocr_batch" and not failures:
            failures.append(kwargs["dedupe_key"])
            raise sqlite3.OperationalError("database is locked")
        return real(**kwargs)

    monkeypatch.setattr(queue, "enqueue", flaky)
    await h.poll(2)
    (batch_id,) = h.client.batches
    assert failures and h.jobs("ocr_batch") == []
    pending = json.loads(h.linked.index().get_setting(PENDING_BATCHES_KEY) or "[]")
    assert [p["batch_id"] for p in pending] == [batch_id]
    # Its pages are not sent again while the batch waits to be adopted.
    await h.poll()
    assert len(h.client.batches) == 1 and h.client.count("transcribe") == 0
    (job,) = h.jobs("ocr_batch")
    assert job.payload["batch_id"] == batch_id
    assert h.linked.index().get_setting(PENDING_BATCHES_KEY) is None
    await _until_ended(h, batch_id)
    assert set(h.transcribed()) == set(PAGES)
    assert h.client.pages_sent() == 7


async def test_a_refused_key_in_a_batch_defers_the_fallback(batching: Harness) -> None:
    h = batching
    await h.poll(2)
    (batch_id,) = h.client.batches
    results = {f"p{n}": OcrItemError("bad_key", False) for n in range(7)}
    await _until_ended(h, batch_id, results=results)
    note = json.loads(h.linked.index().get_setting(KEY_REFUSED_KEY) or "{}")
    assert (note["mode"], note["reason"]) == ("server", "bad_key")
    assert all(j.result == {"state": "deferred", "reason": "key_refused"} for j in h.jobs())
    assert h.client.count("transcribe") == 0


async def test_a_batch_whose_key_is_gone_falls_back_to_single_jobs(batching: Harness) -> None:
    h = batching
    await h.poll(2)
    (batch_id,) = h.client.batches

    def no_key(settings: Any, box: Any, mode: str) -> None:
        return None

    h.linked.worker.ocr = dataclasses.replace(fake_ports(client=h.client), batch_key=no_key)
    await _until_ended(h, batch_id)
    (job,) = h.jobs("ocr_batch")
    assert job.result == {"state": "orphaned", "reason": "no_key", "fallback": 7}
    await h.linked.worker.drain_jobs(kinds=OCR_KINDS)
    assert set(h.transcribed()) == set(PAGES)
