"""The worker's OCR (Plan 05 §3.2): the auto-OCR scan, ``ocr`` and ``ocr_batch`` jobs.

A real worker, index and queue against the fake cloud. Most tests fake F's ledger and resolver
(``tests/fakes/ocr.py``) to set caps and switch modes directly; ``test_the_real_binding_*``
runs F's own ``resolve`` and ``SpendLedger`` through ``build_ocr_ports``. The client is always
the fake: no Claude API call is made. These are §5 checks 1 and 2: ten polls give one
transcription per content hash, and a page that changes on every poll is not transcribed
until it stops changing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from scriptorium.jobs.kinds import (
    KEY_ERROR_PREFIX,
    OCR_KINDS,
    RERUN_CAP,
    OcrSpec,
    parse_ocr_batch_payload,
)
from scriptorium.jobs.queue import JobRow, deferral_reason, is_deferred
from scriptorium.library import pages as pg
from scriptorium.ocr import runner as runner_mod
from scriptorium.ocr._ports import build_ocr_ports
from scriptorium.ocr.client import OcrItemError, Transcription
from scriptorium.ocr.debounce import STATE_OCR_POLL_AT
from scriptorium.ocr.errors import OcrRefused, OcrUnavailable
from scriptorium.ocr.ledger import SpendLedger, cost_usd, month_bounds
from scriptorium.ocr.modes import USER_KEY_SECRET
from scriptorium.ocr.pipeline import (
    AUTO_OCR_KEY,
    KEY_REFUSED_HOLD,
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

    def get(self, job_id: int) -> JobRow:
        job = self.linked.queue.get(job_id, self.linked.uid)
        assert job is not None
        return job

    def spec(self, n: int, *, force: bool = False) -> OcrSpec:
        """The current version of the notebook's page ``n`` (from 0)."""
        index = self.linked.index()
        page = index.page(NOTEBOOK, n)
        version = pg.current_version(self.linked.paths, index, NOTEBOOK)
        assert page is not None and version is not None
        return OcrSpec(NOTEBOOK, page.page_id, page.content_hash, version.generation, force)

    def enqueue(self, spec: OcrSpec, **kwargs: bool) -> tuple[JobRow | None, str]:
        linked = self.linked
        kwargs.setdefault("retry_failed", False)
        return enqueue_page(linked.queue, uid=linked.uid, acct=linked.acct, spec=spec, **kwargs)

    def after_keys(self) -> list[JobRow]:
        return [j for j in self.jobs() if ":after:" in j.dedupe_key]

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
    later = harness.get(job.id)
    assert (later.state, deferral_reason(later), later.attempts) == ("queued", "not_configured", 0)
    assert later.result is None
    wait = later.next_run_at - harness.linked.clock.now
    assert timedelta(minutes=55) < wait <= runner_mod.DEFER_RECHECK


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


async def test_a_deferred_page_runs_under_its_own_key_once_a_key_is_back(
    harness: Harness,
) -> None:
    h = harness
    await h.poll()
    h.resolver.mode = "lazy"
    job, _ = h.enqueue(h.spec(0))
    assert job is not None
    await h.linked.worker.drain_jobs(kinds=OCR_KINDS)
    deferred = h.get(job.id)
    assert (deferred.state, deferral_reason(deferred), deferred.attempts) == ("queued", "no_key", 0)
    # The scan wakes it once a key is there, whether or not auto-OCR covers the page.
    h.linked.index().set_setting(AUTO_OCR_KEY, None)
    h.resolver.mode = "server"
    await h.poll()
    done = h.get(job.id)
    assert (done.state, done.attempts) == ("done", 1)
    assert done.result is not None and done.result["state"] == "stored"
    assert set(h.transcribed()) == {PAGES[0]} and h.after_keys() == []


async def test_waiting_for_a_key_never_spends_an_attempt_or_a_rerun(harness: Harness) -> None:
    """Review scenario C: deferrals used to finish the job, so every read_page while no key
    (or no room) was available minted an :after: re-run, and RERUN_CAP was spent waiting."""
    h = harness
    await h.poll()
    h.resolver.mode = "lazy"
    spec = h.spec(0)
    first, how = h.enqueue(spec)
    assert first is not None and how == "queued"
    for _ in range(RERUN_CAP + 2):
        # Each round wakes the job (as transcribe does) and lets the worker look at it again.
        job, how = h.enqueue(spec, wake=True)
        assert job is not None and (job.id, how) == (first.id, "active")
        await h.linked.worker.drain_jobs(kinds=OCR_KINDS)
        later = h.get(first.id)
        assert (later.state, deferral_reason(later), later.attempts) == ("queued", "no_key", 0)
    assert len(h.linked.queue.family(h.linked.uid, first.dedupe_key)) == 1
    assert h.client.pages_sent() == 0
    # The key is back: the next poll wakes the job and every page is transcribed once.
    h.resolver.mode = "server"
    await h.poll(3)
    assert set(h.transcribed()) == set(PAGES)
    assert h.client.pages_sent() == len(PAGES) and h.after_keys() == []
    done = h.get(first.id)
    assert (done.state, done.attempts) == ("done", 1)
    # The page's re-runs are all still there for the user.
    forced, how = h.enqueue(h.spec(0, force=True), retry_failed=True)
    assert forced is not None and how == "rerun"


async def test_a_capped_job_waits_for_the_month_unless_room_comes_back(harness: Harness) -> None:
    h = harness
    await h.poll()
    h.ledger.user_cap = 0.0
    job, _ = h.enqueue(h.spec(1))
    assert job is not None
    await h.linked.worker.drain_jobs(kinds=OCR_KINDS)
    later = h.get(job.id)
    assert (deferral_reason(later), later.attempts) == ("cap_reached_user", 0)
    assert later.next_run_at == month_bounds(h.linked.clock.now)[1]
    # The scan sees the cap and queues nothing, leaving the job asleep.
    await h.poll()
    assert h.get(job.id).next_run_at == later.next_run_at and len(h.jobs()) == 1
    # Room again (the operator raised the cap): the scan wakes it.
    h.ledger.user_cap = 10.0
    await h.poll()
    done = h.get(job.id)
    assert done.state == "done" and done.result is not None and done.result["state"] == "stored"
    assert h.after_keys() == []


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
    at = h.linked.clock.now
    note_key_refusal(h.linked.index(), "server", "billing", at)
    job = await _one_job(h)
    assert (job.state, deferral_reason(job), job.attempts) == ("queued", "key_refused", 0)
    assert job.next_run_at == at + KEY_REFUSED_HOLD and job.result is None
    assert h.client.pages_sent() == 0
    # Once the hold has passed, it runs under its own key.
    h.linked.clock.now = at + KEY_REFUSED_HOLD + timedelta(minutes=1)
    await h.linked.worker.drain_jobs(kinds=OCR_KINDS)
    done = h.get(job.id)
    assert done.state == "done" and done.result is not None and done.result["state"] == "stored"
    assert h.after_keys() == []


async def test_a_key_refused_across_several_holds_still_transcribes_every_page(
    harness: Harness,
) -> None:
    """Review scenario D: every hold used to end with each waiting page re-run once, so a
    key refused for a few days left every page at its re-run cap, never transcribed."""
    h = harness
    cycles = RERUN_CAP + 1
    for _ in range(cycles):
        h.client.transcriptions.append(OcrRefused("billing"))
    await h.poll()  # first sight: nothing is stable yet
    for cycle in range(1, cycles + 1):
        h.linked.clock.now += KEY_REFUSED_HOLD + timedelta(hours=1)
        await h.poll()
        jobs = h.jobs()
        dead = [j for j in jobs if j.state == "dead"]
        assert len(dead) == cycle and all(j.error == "key_billing" for j in dead)
        assert all(j.attempts <= 1 for j in jobs)
        waiting = [j for j in jobs if j.state != "dead"]
        assert all(is_deferred(j) and deferral_reason(j) == "key_refused" for j in waiting)
        # Re-runs only replace jobs that died of the refused key; waiting mints none.
        by_id = {j.id: j for j in jobs}
        for rerun in h.after_keys():
            parent = by_id[int(rerun.dedupe_key.rsplit(":", 1)[1])]
            assert parent.state == "dead" and (parent.error or "").startswith(KEY_ERROR_PREFIX)
        assert len(h.after_keys()) == cycle - 1
    # The billing problem is fixed and the hold has passed: every page is transcribed.
    h.linked.clock.now += KEY_REFUSED_HOLD + timedelta(hours=1)
    await h.poll(2)
    assert set(h.transcribed()) == set(PAGES)
    assert h.client.pages_sent() == cycles + len(PAGES)
    # Only the key deaths were re-run, and none of them counted against the cap.
    assert len(h.after_keys()) == cycles
    forced, how = h.enqueue(h.spec(0, force=True), retry_failed=True)
    assert forced is not None and how == "rerun"


# ---- a removed account ------------------------------------------------------------------------


async def test_a_job_for_a_removed_account_sends_nothing(harness: Harness) -> None:
    h = harness
    await h.poll()
    job, _ = h.enqueue(h.spec(0))
    assert job is not None
    # Marked removed, directory not moved yet: the row is still there, with removed_at.
    h.linked.store.mark_removed(h.linked.uid, h.linked.acct, at=h.linked.clock.now)
    await h.linked.worker.drain_jobs(kinds=OCR_KINDS)
    assert h.get(job.id).result == {"state": "gone"}
    assert h.client.calls == [] and h.ledger.pages() == 0


async def test_an_account_removed_while_its_page_renders_sends_nothing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    await h.poll()
    job, _ = h.enqueue(h.spec(0))
    assert job is not None
    real = runner_mod.batches.fits

    def removed_meanwhile(png: bytes) -> str | None:
        h.linked.store.mark_removed(h.linked.uid, h.linked.acct, at=h.linked.clock.now)
        return real(png)

    monkeypatch.setattr(runner_mod.batches, "fits", removed_meanwhile)
    await h.linked.worker.drain_jobs(kinds=OCR_KINDS)
    assert h.get(job.id).result == {"state": "gone"}
    assert h.client.calls == [] and h.ledger.pages() == 0


async def test_a_scan_of_a_removed_account_submits_nothing(batching: Harness) -> None:
    h = batching
    await h.poll()  # first sight
    h.linked.store.mark_removed(h.linked.uid, h.linked.acct, at=h.linked.clock.now)
    ctx = h.linked.context()
    assert await h.linked.worker.ocr_runner.scan(ctx, complete=True) == 0
    assert h.client.calls == [] and h.jobs() == [] and h.jobs("ocr_batch") == []
    # The same scan with the mark lifted would have sent the batch.
    h.linked.store.unmark_removed(h.linked.uid, h.linked.acct)
    assert await h.linked.worker.ocr_runner.scan(ctx, complete=True) == len(PAGES)
    assert len(h.client.batches) == 1


async def test_an_account_removed_during_a_scan_submits_no_batch(
    batching: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = batching
    real = runner_mod.batches.plan

    def removed_meanwhile(pages: Any) -> Any:
        planned = real(pages)
        if planned:
            h.linked.store.mark_removed(h.linked.uid, h.linked.acct, at=h.linked.clock.now)
        return planned

    monkeypatch.setattr(runner_mod.batches, "plan", removed_meanwhile)
    await h.poll(2)
    assert h.client.count("submit_batch") == 0 and h.client.batches == {}
    assert h.jobs("ocr_batch") == []


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


async def test_a_batch_fits_the_cap_even_if_every_item_costs_the_most_it_can(
    batching: Harness,
) -> None:
    """A batch is paid for item by item after it is sent, with no further cap check, so the
    scan admits only as many items as the room covers at OCR_MAX_TOKENS of output each."""
    h = batching
    settings = h.linked.settings
    worst = cost_usd(settings, runner_mod.EST_INPUT_TOKENS, settings.ocr_max_tokens, batch=True)
    typical = cost_usd(
        settings, runner_mod.EST_INPUT_TOKENS, runner_mod.EST_OUTPUT_TOKENS, batch=True
    )
    room = 5.5 * worst
    assert room / typical >= len(PAGES)  # the typical estimate would have sent every page
    h.ledger.user_cap = room
    await h.poll(2)
    (batch_id,) = h.client.batches
    sent = len(h.client.batches[batch_id].custom_ids)
    assert sent == 5 and sent * worst <= room


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


async def test_reading_a_batch_again_after_a_failed_stream_bills_each_item_once(
    batching: Harness,
) -> None:
    """Review scenario A: a result stream that failed half-way was read again from the start,
    and every billed item without a stored transcript was recorded again on each read."""
    h = batching
    await h.poll(2)
    (batch_id,) = h.client.batches
    batch = h.client.batches[batch_id]
    cids = batch.custom_ids
    results: dict[str, Transcription | OcrItemError] = {
        cid: canned(default_text(batch.pngs[cid]), batch=True) for cid in cids
    }
    results[cids[1]] = canned("x" * (31 * 1024), batch=True)  # billed, too large to store
    results[cids[2]] = OcrItemError("refused", False, 1800, 9)  # billed, refused for good
    h.client.finish_batch(batch_id, results=results)
    real = h.client.batch_results
    failures = [3]

    def failing_stream(bid: str) -> Iterator[tuple[str, Transcription | OcrItemError]]:
        for n, entry in enumerate(real(bid), start=1):
            yield entry
            if n == 3 and failures[0]:
                failures[0] -= 1
                raise OcrUnavailable("stream_failed")

    h.client.batch_results = failing_stream  # type: ignore[method-assign]
    for _ in range(4):
        h.linked.clock.now += timedelta(hours=2)
        await h.linked.worker.drain_jobs(kinds=OCR_KINDS)
    (job,) = h.jobs("ocr_batch")
    assert job.state == "done" and failures == [0]
    # One spend row per item, at the batch price, however many reads.
    assert sorted((r.batch_id, r.custom_id) for r in h.ledger.rows) == sorted(
        (batch_id, cid) for cid in cids
    )
    (refused,) = [r for r in h.ledger.rows if r.custom_id == cids[2]]
    assert refused.cost_usd == pytest.approx(cost_usd(h.linked.settings, 1800, 9, batch=True))
    dead = sorted((j.payload["page"], j.error) for j in h.jobs())
    assert dead == sorted([(PAGES[1], "too_large"), (PAGES[2], "refused")])
    assert h.after_keys() == [] and set(h.transcribed()) == set(PAGES) - {PAGES[1], PAGES[2]}
    # Reading the results once more (a lost lease, a crash before completion) adds nothing.
    bspec = parse_ocr_batch_payload(job.payload)
    runner = h.linked.worker.ocr_runner
    again = await asyncio.to_thread(runner._collect, h.linked.context(), job, bspec, h.client)
    assert (again["cost_usd"], again["failed"], again["fallback"]) == (0, 0, 0)
    assert len(h.ledger.rows) == len(cids) and len(h.jobs()) == 2 and h.after_keys() == []
    # The refused pages' re-runs are all still there for the user.
    rerun, how = h.enqueue(h.spec(1), retry_failed=True)
    assert rerun is not None and how == "rerun"


async def test_a_batch_item_billed_after_a_lazy_save_is_still_recorded(batching: Harness) -> None:
    """Review scenario B: an item whose page was saved lazily before the results were read
    was billed by the API but never recorded."""
    h = batching
    await h.poll(2)
    (batch_id,) = h.client.batches
    index = h.linked.index()
    page = index.page(NOTEBOOK, 0)
    assert page is not None
    index.put_transcript(
        NOTEBOOK,
        page.page_id,
        page.content_hash,
        "saved by the user's own session",
        DEFAULT_MODEL,
        "lazy",
        prompt_version="v1",
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
    )
    await _until_ended(h, batch_id)
    (job,) = h.jobs("ocr_batch")
    assert job.result is not None
    assert (job.result["stored"], job.result["exists"]) == (len(PAGES) - 1, 1)
    half = cost_usd(h.linked.settings, 2300, 150, batch=True)
    assert len(h.ledger.rows) == len(PAGES)
    assert job.result["cost_usd"] == pytest.approx(len(PAGES) * half, abs=1e-6)
    kept = index.transcript(NOTEBOOK, page.page_id, page.content_hash)
    assert kept is not None and kept.mode == "lazy"


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
    assert len(h.jobs()) == 7
    assert all(deferral_reason(j) == "key_refused" and j.attempts == 0 for j in h.jobs())
    assert h.client.count("transcribe") == 0
    h.linked.clock.now += KEY_REFUSED_HOLD + timedelta(minutes=1)
    await h.poll()
    assert set(h.transcribed()) == set(PAGES) and h.after_keys() == []


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
