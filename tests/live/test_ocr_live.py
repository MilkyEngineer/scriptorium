"""OcrClient against the real Claude API (M2 breakdown §4). Skipped unless
SCRIPTORIUM_LIVE_OCR_TEST=1; the coordinator runs it with the operator's key::

    SCRIPTORIUM_LIVE_OCR_TEST=1 SCRIPTORIUM_ANTHROPIC_API_KEY_FILE=/run/secrets/claude_key \\
        uv run pytest tests/live -q -s

What it spends: two free calls (``models.retrieve`` with the key and with a made-up key) and
five pages of the S3 fixture notebook (its golden renders, written for the purpose): three as
single Messages calls and two in one Message Batch at half price, about three US cents in all
at the claude-sonnet-5-5 list price. SCRIPTORIUM_OCR_MODEL picks another model;
SCRIPTORIUM_LIVE_OCR_BATCH_WAIT_S bounds the wait for the batch (default 1200 s; the test
skips with the batch id if it has not ended by then).

Output (with ``-s``): token counts, stop reasons, costs and the batch id. Never a transcript:
the assertions read the text, nothing prints or logs it. The key is read from its file inside
the test and handed to OcrClient as a SecretStr only.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from pydantic import SecretStr

from scriptorium.config import API_KEY_RE, DEFAULT_OCR_MODEL, Settings
from scriptorium.ocr.client import BatchItem, OcrClient, OcrItemError, Transcription, check_png
from scriptorium.ocr.errors import OcrRefused
from scriptorium.ocr.ledger import cost_usd
from scriptorium.ocr.prompt import load_prompt
from tests.conftest import make_settings

# Read at import: tests/conftest.py clears SCRIPTORIUM_* from the environment of every test.
_ENABLED: Final = os.environ.get("SCRIPTORIUM_LIVE_OCR_TEST") == "1"
_KEY_FILE: Final = os.environ.get("SCRIPTORIUM_ANTHROPIC_API_KEY_FILE")
_KEY_ENV: Final = SecretStr(os.environ.get("SCRIPTORIUM_ANTHROPIC_API_KEY") or "")
_MODEL: Final = os.environ.get("SCRIPTORIUM_OCR_MODEL") or DEFAULT_OCR_MODEL
_BATCH_WAIT_S: Final = float(os.environ.get("SCRIPTORIUM_LIVE_OCR_BATCH_WAIT_S") or 1200)

GOLDEN: Final = Path(__file__).parents[1] / "fixtures" / "move-test-notebook" / "golden"
SINGLE_PAGES: Final = ("notebook-01.png", "notebook-02.png", "notebook-03.png")
BATCH_PAGES: Final = ("notebook-04.png", "notebook-05.png")
MAX_PAGES: Final = 5  # the breakdown's cap for this test
MADE_UP_KEY: Final = "sk-ant-api03-" + "0" * 40 + "-scriptorium-live-test"

pytestmark = [
    pytest.mark.live_ocr,
    pytest.mark.skipif(not _ENABLED, reason="set SCRIPTORIUM_LIVE_OCR_TEST=1 to call the API"),
]
assert len(SINGLE_PAGES) + len(BATCH_PAGES) <= MAX_PAGES


def _key() -> SecretStr:
    if _KEY_FILE:
        key = Path(_KEY_FILE).read_text(encoding="ascii").strip()
    else:
        key = _KEY_ENV.get_secret_value().strip()
    if not API_KEY_RE.fullmatch(key):
        pytest.fail("set SCRIPTORIUM_ANTHROPIC_API_KEY_FILE (or _KEY) to a Claude API key")
    return SecretStr(key)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return make_settings(ocr_model=_MODEL)


@pytest.fixture(scope="module")
def client(settings: Settings) -> Iterator[OcrClient]:
    with OcrClient(_key(), model=settings.ocr_model, max_tokens=settings.ocr_max_tokens) as live:
        yield live


def _page(name: str) -> bytes:
    png = (GOLDEN / name).read_bytes()
    check_png(png)
    return png


def _report(label: str, result: Transcription, settings: Settings) -> float:
    cost = cost_usd(settings, result.input_tokens, result.output_tokens, batch=result.batch)
    print(  # counts only: never the text
        f"{label}: {result.input_tokens} in / {result.output_tokens} out, "
        f"stop={result.stop_reason}, model={result.model}, {len(result.text)} chars, "
        f"${cost:.5f}"
    )
    return cost


def _looks_like_a_transcript(result: Transcription) -> None:
    assert result.text.strip(), "empty transcript"
    assert not result.text.lstrip().startswith("```"), "the prompt asks for no code fence"
    assert result.input_tokens > 500  # an image and the prompt
    assert result.output_tokens > 0
    assert result.stop_reason in ("end_turn", "stop_sequence"), result.stop_reason


def test_the_key_works_and_a_made_up_one_does_not(client: OcrClient, settings: Settings) -> None:
    client.validate_key()
    with (
        OcrClient(SecretStr(MADE_UP_KEY), model=settings.ocr_model, max_tokens=256) as bad,
        pytest.raises(OcrRefused) as caught,
    ):
        bad.validate_key()
    assert caught.value.reason == "bad_key"
    assert MADE_UP_KEY not in str(caught.value) and MADE_UP_KEY not in repr(caught.value)


def test_three_pages_one_call_each(client: OcrClient, settings: Settings) -> None:
    prompt = load_prompt()
    total = 0.0
    for name in SINGLE_PAGES:
        result = client.transcribe(_page(name), prompt)
        _looks_like_a_transcript(result)
        assert result.batch is False
        total += _report(name, result, settings)
    print(f"single calls: ${total:.5f} for {len(SINGLE_PAGES)} pages (prompt {prompt.version})")
    assert total < 0.25  # a sanity bound, far above the list price of three pages


def test_two_pages_in_one_batch(client: OcrClient, settings: Settings) -> None:
    prompt = load_prompt()
    items = [BatchItem(f"page-{n}", _page(name)) for n, name in enumerate(BATCH_PAGES, 1)]
    batch_id = client.submit_batch(items, prompt)
    print(f"batch {batch_id} submitted with {len(items)} pages")
    deadline = time.monotonic() + _BATCH_WAIT_S
    status = client.batch_status(batch_id)
    while status.state in ("in_progress", "canceling") and time.monotonic() < deadline:
        time.sleep(15)
        status = client.batch_status(batch_id)
    if status.state != "ended":
        pytest.skip(f"batch {batch_id} is {status.state} after {_BATCH_WAIT_S:.0f} s")
    assert status.counts.succeeded == len(items), status.counts
    results = dict(client.batch_results(batch_id))
    assert set(results) == {item.custom_id for item in items}
    total = 0.0
    for custom_id, result in sorted(results.items()):
        assert not isinstance(result, OcrItemError), (custom_id, result)
        _looks_like_a_transcript(result)
        assert result.batch is True
        total += _report(custom_id, result, settings)
    print(f"batch: ${total:.5f} for {len(items)} pages at the batch discount")
