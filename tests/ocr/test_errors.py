"""OCR errors: fixed reason codes, §11.0 codes, messages that carry nothing from outside."""

from __future__ import annotations

import pytest

from scriptorium.errors import ScriptoriumError
from scriptorium.ocr.errors import KEY_REASONS, OcrRefused, OcrUnavailable


def test_codes_and_fields() -> None:
    unavailable = OcrUnavailable("rate_limited", retry_after_s=3.0)
    assert isinstance(unavailable, ScriptoriumError)
    assert (unavailable.code, unavailable.reason, unavailable.retry_after_s) == (
        "internal",
        "rate_limited",
        3.0,
    )
    assert unavailable.hint == "try again later"
    refused = OcrRefused("refused", input_tokens=10, output_tokens=2)
    assert (refused.code, refused.reason, refused.input_tokens, refused.output_tokens) == (
        "refused",
        "refused",
        10,
        2,
    )
    assert refused.as_dict() == {
        "code": "refused",
        "message": "the model declined to transcribe this page",
    }


def test_about_key() -> None:
    assert {r for r in KEY_REASONS if OcrRefused(r).about_key} == KEY_REASONS
    assert not OcrRefused("image_too_large").about_key


@pytest.mark.parametrize("make", [OcrRefused, OcrUnavailable])
def test_reasons_are_a_closed_set(make: type[ScriptoriumError]) -> None:
    """A free-text reason could carry an API message (or worse) into a log line."""
    with pytest.raises(ValueError):
        make("invalid x-api-key sk-ant-123")
