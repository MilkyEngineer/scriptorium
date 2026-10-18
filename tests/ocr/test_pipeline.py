from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from scriptorium.config import Settings
from scriptorium.errors import NotFoundError, ScriptoriumError
from scriptorium.jobs.kinds import RERUN_CAP, OcrBatchSpec, OcrSpec
from scriptorium.jobs.queue import JobQueue
from scriptorium.library.index import Index
from scriptorium.ocr.ledger import Allowance, Degraded
from scriptorium.ocr.modes import Resolution
from scriptorium.ocr.pipeline import (
    AUTO_OCR_KEY,
    KEY_REFUSED_HOLD,
    KEY_REFUSED_KEY,
    KEY_UNREADABLE,
    MAX_PENDING_BATCHES,
    PENDING_BATCHES_KEY,
    AutoOcr,
    KeyRefusal,
    ModeChoice,
    add_pending_batch,
    affordable_pages,
    blocked_reason,
    clear_key_refusal,
    drop_pending_batch,
    dump_auto_ocr,
    enqueue_page,
    is_deferred,
    is_key_failure,
    key_error,
    key_mode,
    load_auto_ocr,
    load_key_refusal,
    load_pending_batches,
    note_key_refusal,
    remaining_usd,
    resolve_mode,
    safe_model,
)
from scriptorium.users.store import GlobalStore
from tests.conftest import make_settings
from tests.fakes.ocr import fake_ports

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
    def allowance(degraded: Degraded) -> Allowance:
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
    for bad in ("", "x" * 101, "a\nb", "two words", "-lead", None, 3):
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


def test_key_failures_are_rerun_without_being_asked(queue: JobQueue) -> None:
    first, _ = enqueue_page(queue, uid=UID, acct=ACCT, spec=spec(), retry_failed=False)
    assert first is not None
    job = queue.lease("w", kinds=("ocr",))
    assert job is not None
    queue.fail(job.id, "w", key_error("bad_key"), final=True)
    dead = queue.get(first.id, UID)
    assert dead is not None and dead.error == "key_bad_key" and is_key_failure(dead)
    rerun, how = enqueue_page(queue, uid=UID, acct=ACCT, spec=spec(), retry_failed=False)
    assert rerun is not None and how == "rerun" and rerun.id != first.id


# ---- the mode ---------------------------------------------------------------------------------


def _resolving(found: Resolution | Exception) -> Any:
    def resolve(*args: object) -> Resolution:
        if isinstance(found, Exception):
            raise found
        return found

    return replace(fake_ports(), resolve=resolve)


def test_resolve_mode_keeps_the_key_only_for_a_key_mode() -> None:
    settings = make_settings()
    store = cast(GlobalStore, None)
    server = Resolution("server", "server_key", SecretStr("sk-ant-server-key-for-tests"))
    choice = resolve_mode(_resolving(server), settings, store, UID, T0)
    assert (choice.mode, choice.reason) == ("server", "server_key")
    assert choice.api_key is not None
    assert "sk-ant" not in repr(choice)
    lazy = resolve_mode(_resolving(Resolution("lazy", "no_key")), settings, store, UID, T0)
    assert lazy == ModeChoice("lazy", "no_key")
    keyless = resolve_mode(_resolving(Resolution("user", "user_key")), settings, store, UID, T0)
    assert (keyless.mode, keyless.api_key) == ("lazy", None)


def test_an_unreadable_key_resolves_lazy_and_other_errors_propagate() -> None:
    settings: Settings = make_settings()
    store = cast(GlobalStore, None)
    broken = _resolving(ScriptoriumError("the stored Claude API key cannot be read"))
    assert resolve_mode(broken, settings, store, UID, T0) == ModeChoice("lazy", KEY_UNREADABLE)
    with pytest.raises(NotFoundError):
        resolve_mode(_resolving(NotFoundError("no such user")), settings, store, UID, T0)


def test_key_mode() -> None:
    assert [key_mode(m) for m in ("user", "server", "lazy", "")] == ["user", "server", None, None]


# ---- a refused key ----------------------------------------------------------------------------


def test_a_key_refusal_round_trips_and_holds_for_its_mode_only(index: Index) -> None:
    assert load_key_refusal(index) is None
    note_key_refusal(index, "server", "billing", T0)
    note = load_key_refusal(index)
    assert note == KeyRefusal("server", "billing", T0)
    assert json.loads(index.get_setting(KEY_REFUSED_KEY) or "")["reason"] == "billing"
    assert note.applies("server", T0 + KEY_REFUSED_HOLD - timedelta(seconds=1))
    assert not note.applies("server", T0 + KEY_REFUSED_HOLD)
    assert not note.applies("user", T0)
    clear_key_refusal(index, "user")  # about the other mode: kept
    assert load_key_refusal(index) is not None
    clear_key_refusal(index, "server")
    assert index.get_setting(KEY_REFUSED_KEY) is None


@pytest.mark.parametrize(
    ("mode", "reason"), [("lazy", "bad_key"), ("server", "refused"), ("server", "sk-ant-x")]
)
def test_only_key_reasons_for_key_modes_are_noted(index: Index, mode: str, reason: str) -> None:
    note_key_refusal(index, mode, reason, T0)
    assert index.get_setting(KEY_REFUSED_KEY) is None


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"mode": "server"}',
        '{"mode": "lazy", "reason": "bad_key", "at": "2026-10-01T12:00:00.000Z"}',
        '{"mode": "server", "reason": "anything", "at": "2026-10-01T12:00:00.000Z"}',
        '{"mode": "server", "reason": "bad_key", "at": 5}',
    ],
)
def test_a_malformed_key_refusal_reads_as_none_and_is_cleared(index: Index, raw: str) -> None:
    index.set_setting(KEY_REFUSED_KEY, raw)
    assert load_key_refusal(index) is None
    clear_key_refusal(index, "user")
    assert index.get_setting(KEY_REFUSED_KEY) is None


# ---- pending batches --------------------------------------------------------------------------


def _pending(batch_id: str) -> OcrBatchSpec:
    return OcrBatchSpec(batch_id, "server", {"p0": spec()}, prompt_version="v1")


def test_pending_batches_round_trip_and_drop(index: Index) -> None:
    assert load_pending_batches(index) == []
    add_pending_batch(index, _pending("msgbatch_a"))
    add_pending_batch(index, _pending("msgbatch_b"))
    add_pending_batch(index, _pending("msgbatch_a"))  # once per batch id
    assert [b.batch_id for b in load_pending_batches(index)] == ["msgbatch_b", "msgbatch_a"]
    assert load_pending_batches(index)[0] == _pending("msgbatch_b")
    drop_pending_batch(index, "msgbatch_b")
    drop_pending_batch(index, "msgbatch_unknown")
    assert [b.batch_id for b in load_pending_batches(index)] == ["msgbatch_a"]
    drop_pending_batch(index, "msgbatch_a")
    assert index.get_setting(PENDING_BATCHES_KEY) is None


def test_pending_batches_are_bounded_and_garbage_is_dropped(index: Index) -> None:
    for n in range(MAX_PENDING_BATCHES + 3):
        add_pending_batch(index, _pending(f"msgbatch_{n}"))
    kept = [b.batch_id for b in load_pending_batches(index)]
    assert kept == [f"msgbatch_{n}" for n in range(3, MAX_PENDING_BATCHES + 3)]
    good = _pending("msgbatch_ok").payload()
    index.set_setting(PENDING_BATCHES_KEY, json.dumps([{"batch_id": "../x"}, good, 7]))
    assert [b.batch_id for b in load_pending_batches(index)] == ["msgbatch_ok"]
    for raw in ("not json", "{}"):
        index.set_setting(PENDING_BATCHES_KEY, raw)
        assert load_pending_batches(index) == []
