"""transcribe, ocr_status, set_auto_ocr and read_page with a user or server key (M2, §8.1).

The server binds F's modules itself (``Services`` -> ``build_ocr_ports``): F's resolve, the
real SpendLedger in ``scriptorium.sqlite``, and the client factory, which here is the fake in
``tests/fakes/ocr.py``; nothing calls an API. The worker that drains the jobs gets ports over
the server's ledger, so spend recorded by one is what the other reports.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta

import pytest
from mcp_types import ImageContent
from pydantic import SecretStr

from scriptorium.auth.oidc import OIDCValidator
from scriptorium.jobs.kinds import OCR_KINDS
from scriptorium.jobs.queue import deferral_reason, is_deferred
from scriptorium.ocr._ports import OcrPorts, build_ocr_ports
from scriptorium.ocr.errors import OcrRefused
from scriptorium.ocr.ledger import cost_usd
from scriptorium.ocr.pipeline import KEY_REFUSED_HOLD, KEY_REFUSED_KEY
from scriptorium.tools.ocr import LAZY_INSTRUCTION
from tests.cloud.fake_cloud import NOTEBOOK, PAGES, PDF, fixture_documents
from tests.conftest import FakeIssuer
from tests.fakes.ocr import FakeOcrClient, FakeOcrFactory, default_text
from tests.tools.helpers import Served, meta_of, serve, text_of
from tests.worker.helpers import Linked

FOLDER = "00000000-0000-4000-8000-0000000000f0"
NOTEBOOK_PAGES = 7
SERVER_KEY = "sk-ant-server-key-for-tests"
USER_KEY = "sk-ant-users-own-key-for-tests"


@dataclass
class Ocr:
    linked: Linked
    served: Served
    client: FakeOcrClient
    factory: FakeOcrFactory
    ports: OcrPorts

    async def drain(self) -> None:
        await self.linked.worker.drain_jobs(kinds=OCR_KINDS)

    def lazy(self) -> None:
        """Withdraw the user's permission for the server key (F's resolve then says lazy)."""
        self.linked.store.set_server_key_allowed(self.linked.uid, False)

    def spend(self) -> tuple[int, float]:
        month = self.ports.ledger.month(uid=self.linked.uid, mode="server", at=self.linked.clock())
        return month.pages, month.cost_usd


@pytest.fixture
async def ocr(
    linked: Linked, issuer: FakeIssuer, make_validator: Callable[..., OIDCValidator]
) -> AsyncIterator[Ocr]:
    factory = FakeOcrFactory(FakeOcrClient(text=default_text))
    settings = linked.settings.model_copy(
        update={"pending_wait_seconds": 0.2, "anthropic_api_key": SecretStr(SERVER_KEY)}
    )
    linked.store.set_server_key_allowed(linked.uid, True)
    async with serve(
        settings, issuer, make_validator, clock=linked.clock, ocr_client=factory
    ) as served:
        # One ledger per process; the worker here shares the server's /data, so its own
        # ports over the same ledger see the same spend.
        ports = build_ocr_ports(served.services.ledger, client=factory)
        linked.worker.settings = settings
        linked.worker.ocr = ports
        yield Ocr(linked, served, factory.client, factory, served.services.ports)


# ---- transcribe ---------------------------------------------------------------------------------


async def test_transcribe_queues_jobs_and_read_page_returns_the_result(ocr: Ocr) -> None:
    queued = await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [1, 2]})
    assert queued["mode"] == "server"
    assert [j["page"] for j in queued["jobs"]] == [1, 2]
    assert all(isinstance(j["job"], int) and j["state"] == "queued" for j in queued["jobs"])
    # Asking again before the worker ran finds the same jobs, not new ones.
    again = await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [2, 1]})
    assert [j["job"] for j in again["jobs"]] == [j["job"] for j in queued["jobs"]]
    assert {j["how"] for j in again["jobs"]} == {"active"}
    await ocr.drain()
    assert ocr.client.count("transcribe") == 2
    assert set(ocr.factory.keys) == {SERVER_KEY}
    read = await ocr.served.call("read_page", {"document": NOTEBOOK, "page": 1})
    assert not read.is_error
    assert not any(isinstance(block, ImageContent) for block in read.content)
    meta = meta_of(read)
    assert meta["transcript"]["mode"] == "server"
    assert text_of(read, 1).startswith("# Page ")
    # Transcribed pages are skipped unless forced; force queues a re-run.
    done = await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [1]})
    assert done["jobs"] == [{"page": 1, "state": "transcribed"}]
    forced = await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [1], "force": True})
    (job,) = forced["jobs"]
    assert job["how"] == "rerun" and job["state"] == "queued"
    await ocr.drain()
    assert ocr.client.count("transcribe") == 3
    assert ocr.spend()[0] == 3


async def test_transcribe_with_no_pages_means_every_page(ocr: Ocr) -> None:
    queued = await ocr.served.ok("transcribe", {"document": NOTEBOOK})
    assert [j["page"] for j in queued["jobs"]] == list(range(1, NOTEBOOK_PAGES + 1))
    beyond = await ocr.served.error("transcribe", {"document": NOTEBOOK, "pages": [99]})
    assert beyond["code"] == "invalid_argument"


async def test_lazy_transcribe_returns_at_most_three_images(ocr: Ocr) -> None:
    ocr.lazy()
    four = await ocr.served.error("transcribe", {"document": NOTEBOOK, "pages": [1, 2, 3, 4]})
    assert four["code"] == "invalid_argument"
    assert "4 pages" in four["message"]
    every = await ocr.served.error("transcribe", {"document": NOTEBOOK})
    assert f"{NOTEBOOK_PAGES} pages" in every["message"]
    lazy = await ocr.served.call("transcribe", {"document": NOTEBOOK, "pages": [1, 2]})
    assert not lazy.is_error
    assert LAZY_INSTRUCTION in text_of(lazy)
    images = [block for block in lazy.content if isinstance(block, ImageContent)]
    assert len(images) == 2
    assert meta_of(lazy)["mode"] == "lazy"
    assert ocr.client.pages_sent() == 0
    # read_page in lazy mode is M1's: the image and the instruction, nothing queued.
    read = await ocr.served.call("read_page", {"document": NOTEBOOK, "page": 3})
    assert LAZY_INSTRUCTION in text_of(read)
    status = await ocr.served.ok("ocr_status")
    assert sum(q["queued"] for q in status["queue"].values()) == 0


# ---- read_page ---------------------------------------------------------------------------------


async def test_read_page_answers_pending_then_the_transcript(ocr: Ocr) -> None:
    pending = await ocr.served.error("read_page", {"document": NOTEBOOK, "page": 4})
    assert pending["code"] == "pending"
    assert isinstance(pending["job"], int)
    # A second read while queued waits on the same job.
    again = await ocr.served.error("read_page", {"document": NOTEBOOK, "page": 4})
    assert again["job"] == pending["job"]
    await ocr.drain()
    read = await ocr.served.call("read_page", {"document": NOTEBOOK, "page": 4})
    assert not read.is_error
    assert meta_of(read)["transcript"]["mode"] == "server"
    assert ocr.client.pages_sent() == 1


async def test_read_page_falls_back_to_lazy_when_the_job_fails(ocr: Ocr) -> None:
    ocr.client.transcriptions.append(OcrRefused("refused", input_tokens=1200, output_tokens=4))
    pending = await ocr.served.error("read_page", {"document": NOTEBOOK, "page": 5})
    assert pending["code"] == "pending"
    await ocr.drain()
    read = await ocr.served.call("read_page", {"document": NOTEBOOK, "page": 5})
    assert not read.is_error
    assert LAZY_INSTRUCTION in text_of(read)
    status = await ocr.served.ok("ocr_status")
    assert status["dead"] == 1
    assert status["recent_errors"][0]["reason"] == "refused"
    assert status["key_refused"] is None  # the page was refused, not the key
    assert status["spend_this_month"]["pages"] == 1  # its tokens were billed


async def test_an_old_version_is_never_queued(ocr: Ocr) -> None:
    from tests.cloud.scenarios import PAGE_101, PDF_PAGE

    ocr.linked.cloud.set_file(NOTEBOOK, PAGE_101, fixture_documents()[PDF][PDF_PAGE])
    await ocr.linked.sync()
    versions = await ocr.served.ok("versions", {"document": NOTEBOOK})
    old = min(v["generation"] for v in versions["versions"])
    read = await ocr.served.call("read_page", {"document": NOTEBOOK, "page": 1, "version": old})
    assert not read.is_error
    status = await ocr.served.ok("ocr_status")
    assert status["queue"]["ocr"]["queued"] == 0


# ---- ocr_status and account_status --------------------------------------------------------------


async def test_ocr_status_reports_queue_spend_and_remaining(ocr: Ocr) -> None:
    await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [1, 2, 3]})
    before = await ocr.served.ok("ocr_status")
    assert (before["mode"], before["reason"]) == ("server", "server_key")
    assert before["queue"]["ocr"]["queued"] == 3
    assert before["spend_this_month"]["pages"] == 0
    await ocr.drain()
    after = await ocr.served.ok("ocr_status")
    assert after["queue"]["ocr"]["done"] == 3
    spend = after["spend_this_month"]
    assert spend["pages"] == 3 and spend["server_key"]["pages"] == 3
    assert spend["usd"] == pytest.approx(3 * cost_usd(ocr.served.settings, 2300, 150), abs=1e-4)
    caps = after["caps"]["server_key"]
    assert caps["monthly_cap_usd"] == ocr.served.settings.ocr_user_monthly_cap_usd
    assert caps["remaining_usd"] == pytest.approx(caps["monthly_cap_usd"] - spend["usd"], abs=1e-4)
    assert caps["server_budget_spent"] is False
    assert after["caps"]["own_key"] is None and after["key_refused"] is None


async def test_ocr_status_and_account_status_show_the_same_numbers(ocr: Ocr) -> None:
    await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [1, 2]})
    await ocr.drain()
    status = await ocr.served.ok("ocr_status")
    account = (await ocr.served.ok("account_status"))["ocr"]
    for field in ("mode", "reason", "own_key_stored", "month", "resets_at", "spend_this_month"):
        assert status[field] == account[field], field
    assert status["caps"] == account["caps"]
    assert status["queue"] == account["queue"]
    assert account["queue"]["ocr"]["done"] == 2
    assert account["queue"]["ocr_batch"]["queued"] == 0


async def test_ocr_status_is_lazy_without_permission_for_the_server_key(ocr: Ocr) -> None:
    ocr.lazy()
    status = await ocr.served.ok("ocr_status")
    assert (status["mode"], status["reason"]) == ("lazy", "server_key_not_allowed")
    assert status["caps"]["server_key"] is None
    account = await ocr.served.ok("account_status")
    assert account["ocr"]["mode"] == "lazy"
    assert "queue" in account["ocr"]


async def test_ocr_status_without_a_key_is_lazy(library: Served) -> None:
    status = await library.ok("ocr_status")
    assert (status["mode"], status["reason"]) == ("lazy", "no_key")
    assert status["spend_this_month"]["pages"] == 0
    account = await library.ok("account_status")
    assert account["ocr"]["mode"] == "lazy"
    assert "queue" in account["ocr"]


async def test_a_refused_key_shows_in_ocr_status_and_never_the_key(ocr: Ocr) -> None:
    await ocr.served.ok("set_claude_api_key", {"key": USER_KEY})
    ocr.client.transcriptions.append(OcrRefused("bad_key"))
    await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [1]})
    await ocr.drain()
    assert ocr.factory.keys[-1] == USER_KEY
    status = await ocr.served.ok("ocr_status")
    assert status["key_refused"]["mode"] == "user"
    assert status["key_refused"]["reason"] == "bad_key"
    assert status["key_refused"]["paused"] is True
    assert status["recent_errors"][0]["reason"] == "key_bad_key"
    assert "set_claude_api_key" in status["note"]
    dump = json.dumps(status) + json.dumps(await ocr.served.ok("account_status"))
    assert USER_KEY not in dump and SERVER_KEY not in dump
    assert USER_KEY not in repr(ocr.served.audit_rows())
    # Storing a key again clears the note; the page can be transcribed again.
    await ocr.served.ok("set_claude_api_key", {"key": USER_KEY})
    assert ocr.linked.index().get_setting(KEY_REFUSED_KEY) is None
    again = await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [1]})
    assert again["jobs"][0]["state"] == "queued"
    await ocr.drain()
    assert (await ocr.served.ok("ocr_status"))["key_refused"] is None


async def test_clearing_the_key_clears_its_refusal(ocr: Ocr) -> None:
    await ocr.served.ok("set_claude_api_key", {"key": USER_KEY})
    ocr.client.transcriptions.append(OcrRefused("billing"))
    await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [2]})
    await ocr.drain()
    assert ocr.linked.index().get_setting(KEY_REFUSED_KEY) is not None
    await ocr.served.ok("clear_claude_api_key")
    assert ocr.linked.index().get_setting(KEY_REFUSED_KEY) is None
    status = await ocr.served.ok("ocr_status")
    assert (status["mode"], status["key_refused"]) == ("server", None)


async def test_read_page_never_reruns_a_refused_key_and_answers_at_once_while_it_is_paused(
    ocr: Ocr,
) -> None:
    """Review scenario E: read_page used to re-run a job that died of a refused key on every
    read, and to queue (and wait for) jobs during the key's 6-hour hold."""
    queue, uid = ocr.linked.queue, ocr.linked.uid
    await ocr.served.ok("set_claude_api_key", {"key": USER_KEY})
    ocr.client.transcriptions.append(OcrRefused("bad_key"))
    await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [1]})
    await ocr.drain()
    assert queue.counts(uid)["ocr"] == {"dead": 1}
    sent = ocr.client.pages_sent()
    # During the hold: the dead page and a page with no job both answer as lazy mode, at once,
    # and nothing is queued.
    for page in (1, 2):
        read = await ocr.served.call("read_page", {"document": NOTEBOOK, "page": page})
        assert not read.is_error and LAZY_INSTRUCTION in text_of(read)
    assert queue.counts(uid)["ocr"] == {"dead": 1}
    # transcribe still queues; the worker defers the job until the hold is over, and asking
    # again says so rather than waking it.
    queued = await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [2]})
    await ocr.drain()
    again = await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [2]})
    (item,) = again["jobs"]
    assert item["job"] == queued["jobs"][0]["job"]
    assert (item["state"], item["how"], item["deferred"]) == ("queued", "active", "key_refused")
    # After the hold: read_page still leaves the dead job alone; the deferred one runs.
    ocr.linked.clock.now += KEY_REFUSED_HOLD + timedelta(minutes=1)
    read = await ocr.served.call("read_page", {"document": NOTEBOOK, "page": 1})
    assert not read.is_error and LAZY_INSTRUCTION in text_of(read)
    await ocr.drain()
    assert queue.counts(uid)["ocr"] == {"dead": 1, "done": 1}
    assert ocr.client.pages_sent() == sent + 1
    # transcribe re-runs the dead page on request.
    rerun = await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [1]})
    assert rerun["jobs"][0]["how"] == "rerun"


async def test_ocr_status_counts_deferred_jobs_and_read_page_does_not_wait_for_them(
    ocr: Ocr,
) -> None:
    queue, uid = ocr.linked.queue, ocr.linked.uid
    ports = ocr.linked.worker.ocr
    ocr.linked.worker.ocr = None  # a worker without OCR defers every OCR job it leases
    queued = await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [1, 2, 3]})
    ids = [j["job"] for j in queued["jobs"]]
    await ocr.drain()
    status = await ocr.served.ok("ocr_status")
    assert status["queue"]["ocr"] == {
        "queued": 0,
        "deferred": 3,
        "running": 0,
        "retrying": 0,
        "done": 0,
        "dead": 0,
    }
    assert status["deferred_reasons"] == {"not_configured": 3}
    account = (await ocr.served.ok("account_status"))["ocr"]
    assert account["queue"] == status["queue"]
    # read_page answers as lazy mode at once and leaves the job asleep.
    read = await ocr.served.call("read_page", {"document": NOTEBOOK, "page": 1})
    assert not read.is_error and LAZY_INSTRUCTION in text_of(read)
    first = queue.get(ids[0], uid)
    assert first is not None and deferral_reason(first) == "not_configured"
    # Storing a key wakes them; each runs under its own job, without a re-run.
    ocr.linked.worker.ocr = ports
    await ocr.served.ok("set_claude_api_key", {"key": USER_KEY})
    woken = [queue.get(i, uid) for i in ids]
    assert all(j is not None and not is_deferred(j) for j in woken)
    await ocr.drain()
    status = await ocr.served.ok("ocr_status")
    assert (status["queue"]["ocr"]["deferred"], status["queue"]["ocr"]["done"]) == (0, 3)
    assert status["deferred_reasons"] == {}
    rows = queue._conn.execute("SELECT dedupe_key FROM jobs WHERE kind = 'ocr'").fetchall()
    assert len(rows) == 3 and not any(":after:" in r[0] for r in rows)
    assert ocr.factory.keys[-1] == USER_KEY


# ---- set_auto_ocr ------------------------------------------------------------------------------


def _metadata(name: str, kind: str) -> bytes:
    return json.dumps(
        {
            "deleted": False,
            "lastModified": "1792230476439",
            "parent": "",
            "type": kind,
            "version": 0,
            "visibleName": name,
        }
    ).encode()


async def test_set_auto_ocr_validates_and_stores_folders(ocr: Ocr) -> None:
    ocr.linked.cloud.put_documents(
        {
            FOLDER: {
                f"{FOLDER}.metadata": _metadata("Inbox", "CollectionType"),
                f"{FOLDER}.content": b"{}",
            }
        }
    )
    await ocr.linked.sync()
    on = await ocr.served.ok("set_auto_ocr", {"folders": ["Inbox", FOLDER], "enabled": True})
    assert on["enabled"] is True
    assert on["folders"] == [{"uuid": FOLDER, "name": "Inbox"}]
    assert on["tags"] == [] and on["unmatched_tags"] == []
    status = await ocr.served.ok("ocr_status")
    assert status["auto_ocr"] == {"enabled": True, "folders": [FOLDER], "tags": []}
    stored = ocr.linked.index().get_setting("auto_ocr")
    assert stored is not None
    assert json.loads(stored) == {"enabled": True, "folders": [FOLDER], "tags": []}
    not_folder = await ocr.served.error("set_auto_ocr", {"folders": [NOTEBOOK], "enabled": True})
    assert not_folder["code"] == "invalid_argument"
    unknown = await ocr.served.error(
        "set_auto_ocr", {"folders": ["00000000-0000-4000-8000-0000000000ff"], "enabled": True}
    )
    assert unknown["code"] == "not_found"
    empty = await ocr.served.error("set_auto_ocr", {"folders": [], "enabled": True})
    assert empty["code"] == "invalid_argument"
    off = await ocr.served.ok("set_auto_ocr", {"folders": [], "enabled": False})
    assert off["enabled"] is False and off["folders"] == []
    assert (await ocr.served.ok("ocr_status"))["auto_ocr"]["enabled"] is False


DOC_TAG = "Synthetic OCR"
"""Tags the tests put on the fixture notebook (no real tag names)."""
PAGE_TAG = "Synthetic Page"
MISSING_TAG = "Synthetic Missing"


def _tag_notebook(linked: Linked) -> None:
    files = linked.cloud.docs[NOTEBOOK]
    content = json.loads(files[f"{NOTEBOOK}.content"])
    content["tags"] = [{"name": DOC_TAG, "timestamp": 1}]
    content["pageTags"] = [{"name": PAGE_TAG, "pageId": PAGES[2], "timestamp": 1}]
    linked.cloud.set_file(NOTEBOOK, f"{NOTEBOOK}.content", json.dumps(content).encode())


async def test_set_auto_ocr_stores_tags_and_names_the_unmatched_ones(
    ocr: Ocr, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _tag_notebook(ocr.linked)
    await ocr.linked.sync()
    on = await ocr.served.ok(
        "set_auto_ocr",
        {"tags": [f"  {PAGE_TAG} ", MISSING_TAG, DOC_TAG, PAGE_TAG], "enabled": True},
    )
    # Normalised, deduplicated and sorted; a page tag counts as carried.
    assert on["enabled"] is True and on["folders"] == []
    assert on["tags"] == sorted([DOC_TAG, MISSING_TAG, PAGE_TAG])
    assert on["unmatched_tags"] == [MISSING_TAG]
    assert "spelling" in on["note"]
    assert {"account", "mode", "reason"} <= set(on)
    status = await ocr.served.ok("ocr_status")
    assert status["auto_ocr"] == {"enabled": True, "folders": [], "tags": on["tags"]}
    stored = ocr.linked.index().get_setting("auto_ocr")
    assert stored is not None
    assert json.loads(stored) == {"enabled": True, "folders": [], "tags": on["tags"]}

    matched = await ocr.served.ok("set_auto_ocr", {"tags": [DOC_TAG], "enabled": True})
    assert matched["unmatched_tags"] == [] and "spelling" not in matched["note"]
    # Tags may be given with auto-OCR off too; the call replaces the whole selection.
    off = await ocr.served.ok("set_auto_ocr", {"tags": [PAGE_TAG], "enabled": False})
    assert off["enabled"] is False and off["tags"] == [PAGE_TAG]

    # Tag names are the user's: returned to them, never logged or audited; only counted.
    server_side = "\n".join(
        f"{r.getMessage()} {getattr(r, 'scriptorium_fields', '')}"
        for r in caplog.records
        if not r.name.startswith("mcp.client")
    )
    for name in (DOC_TAG, PAGE_TAG, MISSING_TAG):
        assert name not in server_side
    audit = [row for row in ocr.served.audit_rows() if "set_auto_ocr" in repr(row)]
    assert len(audit) == 3
    for name in (DOC_TAG, PAGE_TAG, MISSING_TAG):
        assert all(name not in repr(row) for row in audit)
    assert '"tag_count":3' in repr(audit[0]).replace(" ", "")


@pytest.mark.parametrize(
    "args",
    [
        {"enabled": True},
        {"folders": [], "tags": [], "enabled": True},
        {"tags": ["   "], "enabled": True},
        {"tags": ["bell\x07"], "enabled": True},
        {"tags": [""], "enabled": True},
        {"tags": [f"t{n}" for n in range(101)], "enabled": True},
        {"tags": ["x" * 257], "enabled": True},
        {"tags": "bell", "enabled": True},
    ],
)
async def test_set_auto_ocr_refuses_bad_tags(ocr: Ocr, args: dict[str, object]) -> None:
    error = await ocr.served.error("set_auto_ocr", args)
    assert error["code"] == "invalid_argument"
    assert "bell" not in json.dumps(error)
    assert ocr.linked.index().get_setting("auto_ocr") is None


async def test_no_transcript_reaches_the_audit_or_the_jobs(ocr: Ocr) -> None:
    await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [1]})
    await ocr.drain()
    read = await ocr.served.call("read_page", {"document": NOTEBOOK, "page": 1})
    text = text_of(read, 1)
    assert text
    assert text not in repr(ocr.served.audit_rows())
    rows = ocr.linked.queue._conn.execute("SELECT payload, result, error FROM jobs").fetchall()
    assert text not in repr([tuple(r) for r in rows])
