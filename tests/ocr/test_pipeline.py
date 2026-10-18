from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scriptorium.jobs.kinds import RERUN_CAP, OcrSpec
from scriptorium.jobs.queue import JobQueue
from scriptorium.library.index import Index
from scriptorium.ocr.pipeline import (
    AUTO_OCR_KEY,
    AutoOcr,
    affordable_pages,
    blocked_reason,
    dump_auto_ocr,
    enqueue_page,
    is_deferred,
    load_auto_ocr,
    remaining_usd,
    safe_model,
)
from tests.fakes.ocr import Allowance

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
UID = "0123456789abcdef"
ACCT = "01J0000000000000000000000A"
DOC = "00000000-0000-4000-8000-000000000001"
PAGE = "00000000-0000-4000-8000-000000000002"
F1 = "00000000-0000-4000-8000-0000000000f1"
F2 = "00000000-0000-4000-8000-0000000000f2"


@pytest.fixture
def queue(tmp_path: Path) -> Iterator[JobQueue]:
    with JobQueue.open(tmp_path / "s.sqlite", migrate=True, clock=lambda: T0) as q:
        yield q


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    with Index.open(tmp_path / "index.sqlite", migrate=True) as ix:
        yield ix


def spec(force: bool = False) -> OcrSpec:
    return OcrSpec(DOC, PAGE, "a" * 64, 3, force)


# ---- the auto-OCR setting ---------------------------------------------------------------------


def test_auto_ocr_round_trips_and_reads_garbage_as_disabled(index: Index) -> None:
    assert load_auto_ocr(index) == AutoOcr()
    index.set_setting(AUTO_OCR_KEY, dump_auto_ocr(AutoOcr(True, (F1, F2))))
    assert json.loads(index.get_setting(AUTO_OCR_KEY) or "") == {
        "enabled": True,
        "folders": [F1, F2],
    }
    assert load_auto_ocr(index) == AutoOcr(True, (F1, F2))
    for raw in (
        "not json",
        "[]",
        '{"enabled": "yes", "folders": []}',
        '{"enabled": true, "folders": "x"}',
        '{"enabled": true}',
    ):
        index.set_setting(AUTO_OCR_KEY, raw)
        assert load_auto_ocr(index) == AutoOcr(), raw
    # Entries that are not folder UUIDs are dropped, duplicates collapse, order is sorted.
    index.set_setting(
        AUTO_OCR_KEY, json.dumps({"enabled": True, "folders": [F2, "../etc", 7, F1, F2]})
    )
    assert load_auto_ocr(index) == AutoOcr(True, (F1, F2))


# ---- caps --------------------------------------------------------------------------------------


def test_blocked_reason_reads_the_cap_of_the_mode() -> None:
    def allowance(degraded: bool | str) -> Allowance:
        return Allowance(1.0, 1.0, None, degraded)

    assert blocked_reason(allowance(False), "server") is None
    assert blocked_reason(allowance("global"), "server") == "cap_reached_global"
    assert blocked_reason(allowance("user"), "server") == "cap_reached_user"
    assert blocked_reason(allowance("own"), "server") is None
    # A user's own key is limited only by the user's own cap.
    assert blocked_reason(allowance("global"), "user") is None
    assert blocked_reason(allowance("own"), "user") == "user_cap_reached"


def test_remaining_and_affordable_pages() -> None:
    a = Allowance(5.0, 2.0, None, False)
    assert remaining_usd(a, "server") == 2.0
    assert remaining_usd(a, "user") is None
    assert remaining_usd(Allowance(5.0, 2.0, 0.5, False), "user") == 0.5
    assert remaining_usd(Allowance(-1.0, 2.0, None, "global"), "server") == 0.0
    assert affordable_pages(None, 0.01, 7) == 7
    assert affordable_pages(0.05, 0.01, 100) == 5
    assert affordable_pages(0.0, 0.01, 100) == 0
    assert affordable_pages(1.0, 0.0, 3) == 3


def test_safe_model() -> None:
    assert safe_model("claude-sonnet-5-5") == "claude-sonnet-5-5"
    for bad in ("", "x" * 101, "a\nb", None, 3):
        assert safe_model(bad) == "unknown"


# ---- queueing ---------------------------------------------------------------------------------


def finish(queue: JobQueue, job_id: int, result: dict[str, object]) -> None:
    job = queue.lease("w", kinds=("ocr",))
    assert job is not None and job.id == job_id
    queue.complete(job.id, "w", result)


def test_enqueue_page_dedupes_one_page_version(queue: JobQueue) -> None:
    first, how = enqueue_page(queue, uid=UID, acct=ACCT, spec=spec(), retry_failed=False)
    assert first is not None and how == "queued"
    assert first.dedupe_key == f"ocr:{ACCT}:{DOC}:{PAGE}:{'a' * 64}"
    again, how = enqueue_page(queue, uid=UID, acct=ACCT, spec=spec(), retry_failed=False)
    assert again is not None and (again.id, how) == (first.id, "active")
    finish(queue, first.id, {"state": "stored"})
    done, how = enqueue_page(queue, uid=UID, acct=ACCT, spec=spec(), retry_failed=True)
    assert done is not None and (done.id, how) == (first.id, "finished")


def test_force_and_deferred_rerun_under_after_keys_up_to_the_cap(queue: JobQueue) -> None:
    first, _ = enqueue_page(queue, uid=UID, acct=ACCT, spec=spec(), retry_failed=False)
    assert first is not None
    finish(queue, first.id, {"state": "deferred", "reason": "no_key"})
    assert is_deferred(queue.get(first.id, UID))  # type: ignore[arg-type]
    rerun, how = enqueue_page(queue, uid=UID, acct=ACCT, spec=spec(), retry_failed=False)
    assert rerun is not None and how == "rerun"
    assert rerun.dedupe_key == f"{first.dedupe_key}:after:{first.id}"
    assert rerun.payload["force"] is False
    finish(queue, rerun.id, {"state": "stored"})
    forced, how = enqueue_page(queue, uid=UID, acct=ACCT, spec=spec(True), retry_failed=False)
    assert forced is not None and how == "rerun" and forced.payload["force"] is True
    for _ in range(RERUN_CAP - 2):
        finish(queue, forced.id, {"state": "stored"})
        forced, how = enqueue_page(queue, uid=UID, acct=ACCT, spec=spec(True), retry_failed=False)
        assert forced is not None and how == "rerun"
    finish(queue, forced.id, {"state": "stored"})
    capped, how = enqueue_page(queue, uid=UID, acct=ACCT, spec=spec(True), retry_failed=False)
    assert (capped, how) == (None, "capped")


def test_dead_jobs_are_retried_only_on_request(queue: JobQueue) -> None:
    first, _ = enqueue_page(queue, uid=UID, acct=ACCT, spec=spec(), retry_failed=False)
    assert first is not None
    job = queue.lease("w", kinds=("ocr",))
    assert job is not None
    queue.fail(job.id, "w", "ocr_refused", final=True)
    same, how = enqueue_page(queue, uid=UID, acct=ACCT, spec=spec(), retry_failed=False)
    assert same is not None and (same.id, how) == (first.id, "finished")
    rerun, how = enqueue_page(queue, uid=UID, acct=ACCT, spec=spec(), retry_failed=True)
    assert rerun is not None and how == "rerun" and rerun.id != first.id
