"""The OCR fake keeps the real client's interface, so code tested against it runs for real."""

from __future__ import annotations

import inspect

import pytest
from pydantic import SecretStr

from scriptorium.ocr.client import BatchItem, OcrClient, OcrItemError
from scriptorium.ocr.errors import OcrRefused, OcrUnavailable
from scriptorium.ocr.prompt import load_prompt
from tests.conftest import make_settings
from tests.fakes.ocr import FakeOcrClient, FakeOcrFactory, canned
from tests.ocr.test_client import PNG

PUBLIC = ["transcribe", "validate_key", "submit_batch", "batch_status", "batch_results", "close"]


@pytest.mark.parametrize("name", PUBLIC)
def test_fake_has_the_real_signature(name: str) -> None:
    real = inspect.signature(getattr(OcrClient, name))
    fake = inspect.signature(getattr(FakeOcrClient, name))
    assert list(real.parameters) == list(fake.parameters)


def test_fake_records_and_answers() -> None:
    fake = FakeOcrClient(transcriptions=[canned("first"), OcrRefused("refused")])
    prompt = load_prompt()
    assert fake.transcribe(PNG, prompt).text == "first"
    with pytest.raises(OcrRefused):
        fake.transcribe(PNG, prompt)
    assert fake.transcribe(PNG, prompt).text == "Transcribed page"
    assert fake.calls[0] == ("transcribe", str(len(PNG)), "v1")
    with pytest.raises(OcrRefused):
        fake.transcribe(b"not a png", prompt)


def test_fake_batches() -> None:
    fake = FakeOcrClient()
    batch = fake.submit_batch([BatchItem("a", PNG), BatchItem("b", PNG)], load_prompt())
    assert fake.batch_status(batch).state == "in_progress"
    with pytest.raises(OcrUnavailable):
        fake.batch_results(batch)
    fake.finish_batch(batch, {"a": canned("A", batch=True), "b": OcrItemError("expired", True)})
    status = fake.batch_status(batch)
    assert (status.state, status.counts.succeeded, status.counts.errored) == ("ended", 1, 1)
    assert dict(fake.batch_results(batch))["a"].text == "A"  # type: ignore[union-attr]
    assert fake.batch_status("msgbatch_unknown").state == "expired"


def test_factory_remembers_keys() -> None:
    factory = FakeOcrFactory(FakeOcrClient(key_error=OcrRefused("bad_key")))
    client = factory(make_settings(), SecretStr("sk-ant-test-" + "u" * 30))
    with pytest.raises(OcrRefused):
        client.validate_key()
    assert factory.keys == ["sk-ant-test-" + "u" * 30]
