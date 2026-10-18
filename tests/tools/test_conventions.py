"""§11.0 conventions: errors, caps, pages, cursors, timestamps."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from scriptorium.tools._conventions import (
    MAX_LIMIT,
    RESULT_CAP_BYTES,
    TEXT_CAP_BYTES,
    ErrorCode,
    NoArgs,
    PageArgs,
    ToolError,
    cap_text,
    decode_cursor,
    encode_cursor,
    error_result,
    index_to_page,
    is_uuid,
    iso_utc,
    json_result,
    page_to_index,
    parse_iso_utc,
    text_result,
)


def test_error_codes_are_the_documented_set() -> None:
    assert {code.value for code in ErrorCode} == {
        "not_linked",
        "needs_relink",
        "not_found",
        "ambiguous",
        "stale_hash",
        "quota_exceeded",
        "ocr_cap_reached",
        "pending",
        "refused",
        "confirm_required",
        "sync_anomaly",
        "invalid_argument",
    }


def test_error_result_shape() -> None:
    result = error_result(ErrorCode.NOT_FOUND, "No such document.", "Use search_documents.")
    assert result.is_error is True
    expected = {
        "code": "not_found",
        "message": "No such document.",
        "hint": "Use search_documents.",
    }
    assert result.structured_content == expected
    text = result.content[0]
    assert text.type == "text"
    assert json.loads(text.text) == expected


def test_error_result_extra_keys_never_override_the_error() -> None:
    result = error_result(
        ErrorCode.AMBIGUOUS,
        "Two documents match.",
        None,
        extra={"candidates": ["a", "b"], "code": "ok", "message": "x"},
    )
    assert result.structured_content == {
        "code": "ambiguous",
        "message": "Two documents match.",
        "hint": None,
        "candidates": ["a", "b"],
    }
    assert ToolError(ErrorCode.PENDING, "Wait.", extra={"job": 3}).extra == {"job": 3}


def test_library_result_cap() -> None:
    assert text_result("a" * RESULT_CAP_BYTES, cap=RESULT_CAP_BYTES).content
    with pytest.raises(ToolError) as info:
        text_result("a" * (RESULT_CAP_BYTES + 1), cap=RESULT_CAP_BYTES)
    assert "32 KB" in info.value.message
    with pytest.raises(ToolError):
        json_result({"x": "a" * RESULT_CAP_BYTES}, cap=RESULT_CAP_BYTES)


def test_text_cap() -> None:
    assert text_result("a" * TEXT_CAP_BYTES).content
    with pytest.raises(ToolError) as info:
        text_result("a" * (TEXT_CAP_BYTES + 1))
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_json_result_cap() -> None:
    with pytest.raises(ToolError):
        json_result({"x": "a" * TEXT_CAP_BYTES})


def test_cap_text_respects_utf8_boundaries() -> None:
    text = "é" * 10  # 2 bytes each
    capped, truncated = cap_text(text, cap=5)
    assert truncated is True
    assert capped == "éé"
    assert cap_text("abc", cap=5) == ("abc", False)


def test_args_reject_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        NoArgs.model_validate({"user_id": "x"})


def test_page_args_limits() -> None:
    assert PageArgs().limit == 50
    with pytest.raises(ValidationError):
        PageArgs(limit=MAX_LIMIT + 1)
    with pytest.raises(ValidationError):
        PageArgs(limit=0)


def test_pages_are_one_based() -> None:
    assert page_to_index(1) == 0
    assert page_to_index(3, page_count=3) == 2
    assert index_to_page(0) == 1
    with pytest.raises(ToolError):
        page_to_index(0)
    with pytest.raises(ToolError):
        page_to_index(4, page_count=3)


def test_cursor_round_trip() -> None:
    state: dict[str, str | int] = {"after": "2026-10-17T00:00:00Z", "offset": 50}
    assert decode_cursor(encode_cursor(state)) == state


@pytest.mark.parametrize("bad", ["!!!", "bm90IGpzb24", encode_cursor({"a": 1})[:-2] + "@@", "WzFd"])
def test_bad_cursor_is_invalid_argument(bad: str) -> None:
    with pytest.raises(ToolError) as info:
        decode_cursor(bad)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_uuid() -> None:
    assert is_uuid("0f3c2a1e-1234-4abc-8def-0123456789ab")
    assert not is_uuid("0F3C2A1E-1234-4ABC-8DEF-0123456789AB")
    assert not is_uuid("../../etc/passwd")


def test_timestamps() -> None:
    moment = datetime(2026, 10, 17, 9, 0, tzinfo=timezone(timedelta(hours=10)))
    assert iso_utc(moment) == "2026-10-16T23:00:00Z"
    assert parse_iso_utc("2026-10-16T23:00:00Z") == moment.astimezone(UTC)
    assert parse_iso_utc("2026-10-16T23:00:00").tzinfo is UTC
    with pytest.raises(ValueError, match="naive"):
        iso_utc(datetime(2026, 1, 1))
    with pytest.raises(ToolError):
        parse_iso_utc("yesterday")
