"""transcribe, ocr_status, set_auto_ocr and read_page with a user or server key (M2, §8.1).

F's resolver, ledger and client are the fakes in ``tests/fakes/ocr.py``; nothing here calls
an API. The worker that drains the jobs shares the server's ports, so spend recorded by one is
reported by the other.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import pytest
from mcp_types import ImageContent

from scriptorium.auth.oidc import OIDCValidator
from scriptorium.jobs.kinds import OCR_KINDS
from scriptorium.ocr._ports import OcrPorts
from scriptorium.tools.ocr import LAZY_INSTRUCTION
from tests.cloud.fake_cloud import NOTEBOOK, PDF, fixture_documents
from tests.conftest import FakeIssuer
from tests.fakes.ocr import (
    FakeOcrClient,
    FakeResolver,
    FakeSpendLedger,
    OcrRefused,
    fake_ports,
)
from tests.tools.helpers import Served, meta_of, serve, text_of
from tests.worker.helpers import Linked

FOLDER = "00000000-0000-4000-8000-0000000000f0"
NOTEBOOK_PAGES = 7


@dataclass
class Ocr:
    linked: Linked
    served: Served
    client: FakeOcrClient
    ledger: FakeSpendLedger
    resolver: FakeResolver
    ports: OcrPorts

    async def drain(self) -> None:
        await self.linked.worker.drain_jobs(kinds=OCR_KINDS)


@pytest.fixture
async def ocr(
    linked: Linked, issuer: FakeIssuer, make_validator: Callable[..., OIDCValidator]
) -> AsyncIterator[Ocr]:
    client, ledger, resolver = FakeOcrClient(), FakeSpendLedger(), FakeResolver("server")
    ports = fake_ports(client=client, ledger=ledger, resolver=resolver)
    linked.worker.ocr = ports
    settings = linked.settings.model_copy(update={"pending_wait_seconds": 0.2})
    async with serve(settings, issuer, make_validator, clock=linked.clock, ocr=ports) as served:
        yield Ocr(linked, served, client, ledger, resolver, ports)


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
    assert len(ocr.client.transcribed) == 2
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
    assert len(ocr.client.transcribed) == 3
    assert len(ocr.ledger.rows) == 3


async def test_transcribe_with_no_pages_means_every_page(ocr: Ocr) -> None:
    queued = await ocr.served.ok("transcribe", {"document": NOTEBOOK})
    assert [j["page"] for j in queued["jobs"]] == list(range(1, NOTEBOOK_PAGES + 1))
    beyond = await ocr.served.error("transcribe", {"document": NOTEBOOK, "pages": [99]})
    assert beyond["code"] == "invalid_argument"


async def test_lazy_transcribe_returns_at_most_three_images(ocr: Ocr) -> None:
    ocr.resolver.mode = "lazy"
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
    assert ocr.client.calls() == 0
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
    assert ocr.client.calls() == 1


async def test_read_page_falls_back_to_lazy_when_the_job_fails(ocr: Ocr) -> None:
    ocr.client.errors.append(OcrRefused("refused"))
    pending = await ocr.served.error("read_page", {"document": NOTEBOOK, "page": 5})
    assert pending["code"] == "pending"
    await ocr.drain()
    read = await ocr.served.call("read_page", {"document": NOTEBOOK, "page": 5})
    assert not read.is_error
    assert LAZY_INSTRUCTION in text_of(read)
    status = await ocr.served.ok("ocr_status")
    assert status["dead"] == 1
    assert status["recent_errors"][0]["reason"] == "ocr_refused"
    assert "refused" not in json.dumps(status["recent_errors"]).replace("ocr_refused", "")


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
    assert before["mode"] == "server"
    assert before["queue"]["ocr"]["queued"] == 3
    assert before["spend_this_month"]["pages"] == 0
    await ocr.drain()
    after = await ocr.served.ok("ocr_status")
    assert after["queue"]["ocr"]["done"] == 3
    assert after["spend_this_month"]["pages"] == 3
    assert after["spend_this_month"]["cost_usd"] > 0
    remaining = after["remaining_this_month"]
    assert remaining["server_key_global_usd"] < 20
    assert remaining["blocked"] is None
    status = await ocr.served.ok("account_status")
    assert status["ocr"]["mode"] == "server"
    assert status["ocr"]["queue"]["ocr"]["done"] == 3
    assert status["ocr"]["queue"]["ocr_batch"]["queued"] == 0


async def test_ocr_status_without_ports_is_lazy(library: Served) -> None:
    status = await library.ok("ocr_status")
    assert status["mode"] == "lazy" and status["reason"] == "not_configured"
    assert status["spend_this_month"] is None
    account = await library.ok("account_status")
    assert account["ocr"]["mode"] == "lazy"
    assert "queue" in account["ocr"]


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
    status = await ocr.served.ok("ocr_status")
    assert status["auto_ocr"] == {"enabled": True, "folders": [FOLDER]}
    stored = ocr.linked.index().get_setting("auto_ocr")
    assert stored is not None and json.loads(stored) == {"enabled": True, "folders": [FOLDER]}
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


async def test_no_transcript_reaches_the_audit_or_the_jobs(ocr: Ocr) -> None:
    await ocr.served.ok("transcribe", {"document": NOTEBOOK, "pages": [1]})
    await ocr.drain()
    read = await ocr.served.call("read_page", {"document": NOTEBOOK, "page": 1})
    text = text_of(read, 1)
    assert text
    assert text not in repr(ocr.served.audit_rows())
    rows = ocr.linked.queue._conn.execute("SELECT payload, result, error FROM jobs").fetchall()
    assert text not in repr([tuple(r) for r in rows])
