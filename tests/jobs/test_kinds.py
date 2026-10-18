"""Job kinds: keys name the version of the work; payloads hold checked identifiers."""

from __future__ import annotations

from typing import Any

import pytest

from scriptorium.errors import InvalidArgumentError
from scriptorium.jobs.kinds import (
    CUSTOM_ID_RE,
    MAX_BATCH_ITEMS,
    OCR,
    OCR_BATCH,
    PAGE_IMAGE,
    RENDER,
    OcrBatchSpec,
    OcrSpec,
    RenderSpec,
    parse_ocr_batch_payload,
    parse_ocr_payload,
    parse_render_payload,
    render_key,
    rerun_key,
)

DOC = "00000000-0000-4000-8000-000000000001"
PAGE = "00000000-0000-4000-8000-000000000101"
HASH = "ab" * 32
ACCT = "01J0000000000000000000000A"


def test_keys_and_payloads_round_trip() -> None:
    archive = RenderSpec(DOC, PAGE, HASH, 3)
    assert archive.kind == RENDER
    assert archive.key(ACCT) == render_key(ACCT, PAGE, HASH) == f"render:{ACCT}:{PAGE}:{HASH}"
    assert parse_render_payload(RENDER, archive.payload()) == archive
    sized = RenderSpec(DOC, PAGE, HASH, 3, max_side=2048)
    assert sized.kind == PAGE_IMAGE
    assert sized.key(ACCT) == f"page_image:{ACCT}:{PAGE}:{HASH}:2048"
    assert parse_render_payload(PAGE_IMAGE, sized.payload()) == sized


def test_a_rerun_key_never_nests() -> None:
    key = render_key(ACCT, PAGE, HASH)
    once = rerun_key(key, 7)
    assert once == f"{key}:after:7"
    assert rerun_key(once, 9) == f"{key}:after:9"


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("sync", {"document": DOC, "page": PAGE, "hash": HASH, "generation": 1}),
        (RENDER, {"document": DOC, "page": PAGE, "hash": HASH}),
        (RENDER, {"document": DOC, "page": PAGE, "hash": HASH, "generation": 1, "x": 1}),
        (RENDER, {"document": DOC, "page": PAGE, "hash": HASH, "generation": 1, "max_side": 9}),
        (PAGE_IMAGE, {"document": DOC, "page": PAGE, "hash": HASH, "generation": 1}),
        (RENDER, {"document": "../etc", "page": PAGE, "hash": HASH, "generation": 1}),
        (RENDER, {"document": DOC, "page": f"{PAGE}/..", "hash": HASH, "generation": 1}),
        (RENDER, {"document": DOC, "page": PAGE, "hash": "AB" * 32, "generation": 1}),
        (RENDER, {"document": DOC, "page": PAGE, "hash": HASH, "generation": True}),
        (RENDER, {"document": DOC, "page": PAGE, "hash": HASH, "generation": -1}),
        (
            PAGE_IMAGE,
            {"document": DOC, "page": PAGE, "hash": HASH, "generation": 1, "max_side": 10**6},
        ),
    ],
)
def test_malformed_payloads_are_refused(kind: str, payload: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentError):
        parse_render_payload(kind, payload)


# ---- OCR ------------------------------------------------------------------------------------


def test_ocr_keys_and_payloads_round_trip() -> None:
    spec = OcrSpec(DOC, PAGE, HASH, 3)
    assert spec.kind == OCR
    assert spec.key(ACCT) == f"ocr:{ACCT}:{DOC}:{PAGE}:{HASH}"
    assert parse_ocr_payload(spec.payload()) == spec
    forced = OcrSpec(DOC, PAGE, HASH, 3, force=True)
    assert forced.key(ACCT) == spec.key(ACCT)  # force re-runs under :after:, same family
    assert parse_ocr_payload(forced.payload()) == forced
    # force may be left out (defaults to False)
    bare = {"document": DOC, "page": PAGE, "hash": HASH, "generation": 3}
    assert parse_ocr_payload(bare) == spec
    assert spec.render_spec() == RenderSpec(DOC, PAGE, HASH, 3)


@pytest.mark.parametrize(
    "payload",
    [
        {"document": DOC, "page": PAGE, "hash": HASH},
        {"document": DOC, "page": PAGE, "hash": HASH, "generation": 1, "text": "x"},
        {"document": "../x", "page": PAGE, "hash": HASH, "generation": 1},
        {"document": DOC, "page": PAGE, "hash": "AB" * 32, "generation": 1},
        {"document": DOC, "page": PAGE, "hash": HASH, "generation": True},
        {"document": DOC, "page": PAGE, "hash": HASH, "generation": 1, "force": 1},
        ["not", "a", "mapping"],
    ],
)
def test_malformed_ocr_payloads_are_refused(payload: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        parse_ocr_payload(payload)


def _batch(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "batch_id": "msgbatch_01ABCdef",
        "mode": "server",
        "items": {"p0": OcrSpec(DOC, PAGE, HASH, 1).payload()},
    }
    payload.update(over)
    return payload


def test_ocr_batch_round_trip() -> None:
    spec = parse_ocr_batch_payload(_batch())
    assert spec.kind == OCR_BATCH
    assert spec.key(ACCT) == f"ocr_batch:{ACCT}:msgbatch_01ABCdef"
    assert spec.items == {"p0": OcrSpec(DOC, PAGE, HASH, 1)}
    assert parse_ocr_batch_payload(spec.payload()) == spec


@pytest.mark.parametrize(
    "payload",
    [
        _batch(batch_id="../../x"),
        _batch(batch_id=""),
        _batch(mode="lazy"),
        _batch(items={}),
        _batch(items={"P0": OcrSpec(DOC, PAGE, HASH, 1).payload()}),
        _batch(items={"p0": {**OcrSpec(DOC, PAGE, HASH, 1).payload(), "force": True}}),
        _batch(items={f"p{n}": OcrSpec(DOC, PAGE, HASH, 1).payload() for n in range(101)}),
        _batch(items=[1]),
        {**_batch(), "extra": 1},
        _batch(prompt_version="1"),
        _batch(prompt_version="v0"),
        _batch(prompt_version="v1\n"),
        _batch(prompt_version=1),
    ],
)
def test_malformed_ocr_batch_payloads_are_refused(payload: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentError):
        parse_ocr_batch_payload(payload)


def test_ocr_batch_carries_the_prompt_version_it_was_sent_with() -> None:
    spec = parse_ocr_batch_payload(_batch(prompt_version="v12"))
    assert spec.prompt_version == "v12"
    assert spec.payload()["prompt_version"] == "v12"
    assert parse_ocr_batch_payload(spec.payload()) == spec
    # A job queued before the field existed has none (and its payload stays as it was).
    older = parse_ocr_batch_payload(_batch())
    assert older.prompt_version is None and "prompt_version" not in older.payload()
    with pytest.raises(InvalidArgumentError):
        OcrBatchSpec("b1", "user", {"p0": OcrSpec(DOC, PAGE, HASH, 1)}, prompt_version="x")


@pytest.mark.parametrize(
    ("cid", "ok"),
    [
        ("p0", True),  # a payload written before the nonce
        ("p9999", True),
        ("0123456789abcdef-p0", True),
        ("0123456789abcdef-p12", True),
        ("0123456789ABCDEF-p0", False),
        ("0123456789abcde-p0", False),
        ("0123456789abcdef-", False),
        ("0123456789abcdef-q0", False),
        ("x-p0", False),
        ("p10000", False),
    ],
)
def test_custom_ids_carry_the_submissions_nonce(cid: str, ok: bool) -> None:
    assert bool(CUSTOM_ID_RE.fullmatch(cid)) is ok
    payload = _batch(items={cid: OcrSpec(DOC, PAGE, HASH, 1).payload()})
    if ok:
        assert list(parse_ocr_batch_payload(payload).items) == [cid]
    else:
        with pytest.raises(InvalidArgumentError):
            parse_ocr_batch_payload(payload)


def test_batch_size_bound() -> None:
    items = {f"p{n}": OcrSpec(DOC, PAGE, HASH, 1) for n in range(MAX_BATCH_ITEMS)}
    assert len(OcrBatchSpec("b1", "user", items).payload()["items"]) == MAX_BATCH_ITEMS
