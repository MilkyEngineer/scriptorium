"""OcrClient against a mock transport: what is sent, retries, error mapping, batches.

Nothing here reaches the network: the SDK runs for real on an ``httpx2.MockTransport``.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import struct
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from PIL import Image
from pydantic import SecretStr

from scriptorium.errors import InvalidArgumentError
from scriptorium.ocr.client import (
    MAX_IMAGE_SIDE,
    BatchCounts,
    BatchItem,
    BatchStatus,
    OcrClient,
    OcrItemError,
    Transcription,
    check_png,
    client_for,
)
from scriptorium.ocr.errors import OcrRefused, OcrUnavailable
from scriptorium.ocr.prompt import load_prompt
from tests.conftest import make_settings

KEY = "sk-ant-test-" + "k" * 40  # the shape of a key, never a real one
MODEL = "claude-sonnet-5-5"
RESULTS_URL = "https://api.anthropic.com/v1/messages/batches/msgbatch_01/results"


def png_of(width: int = 40, height: int = 60) -> bytes:
    out = io.BytesIO()
    Image.new("L", (width, height), 255).save(out, format="PNG")
    return out.getvalue()


def png_header(width: int, height: int) -> bytes:
    """A PNG signature and IHDR claiming ``width`` by ``height`` (enough for check_png)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )


PNG = png_of()


def message_json(
    text: str = "# Notes\n- [ ] call back",
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 2300,
    output_tokens: int = 120,
    extra_blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = list(extra_blocks or [])
    if text:
        content.append({"type": "text", "text": text})
    return {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def batch_json(
    status: str = "in_progress",
    *,
    results_url: str | None = None,
    archived: bool = False,
    counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msgbatch_01",
        "type": "message_batch",
        "processing_status": status,
        "request_counts": counts
        or {"processing": 2, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0},
        "created_at": "2026-10-18T10:00:00Z",
        "expires_at": "2026-10-19T10:00:00Z",
        "ended_at": None if status != "ended" else "2026-10-18T11:00:00Z",
        "archived_at": "2026-11-17T10:00:00Z" if archived else None,
        "cancel_initiated_at": None,
        "results_url": results_url,
    }


Reply = httpx2.Response | Exception | Callable[[httpx2.Request], httpx2.Response]


class FakeApi:
    """A scripted Claude API: answers requests in order and records them."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError(f"unexpected request {request.method} {request.url.path}")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(request)
        return reply

    def body(self, index: int = 0) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.requests[index].content)
        return data


@pytest.fixture(autouse=True)
def _no_sdk_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith(("ANTHROPIC_", "OTEL_")):
            monkeypatch.delenv(name, raising=False)


class Sleeps(list[float]):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def make_client(sleeps: Sleeps) -> Iterator[Callable[[FakeApi], OcrClient]]:
    clients: list[OcrClient] = []

    def make(api: FakeApi) -> OcrClient:
        http = httpx2.Client(transport=httpx2.MockTransport(api))
        client = OcrClient(
            SecretStr(KEY), model=MODEL, max_tokens=4096, http_client=http, sleep=sleeps
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


def ok(payload: dict[str, Any], **headers: str) -> httpx2.Response:
    return httpx2.Response(200, json=payload, headers=headers)


def status(code: int, error_type: str = "api_error", **headers: str) -> httpx2.Response:
    body = {"type": "error", "error": {"type": error_type, "message": "scripted"}}
    return httpx2.Response(code, json=body, headers=headers)


# ---- what is sent ---------------------------------------------------------------------------


def test_request_carries_the_image_and_the_prompt_only(make_client: Any) -> None:
    api = FakeApi(ok(message_json()))
    prompt = load_prompt()
    result = make_client(api).transcribe(PNG, prompt)
    assert result == Transcription(
        text="# Notes\n- [ ] call back",
        model=MODEL,
        input_tokens=2300,
        output_tokens=120,
        stop_reason="end_turn",
        batch=False,
    )
    (request,) = api.requests
    assert (request.method, request.url.scheme, request.url.host, request.url.path) == (
        "POST",
        "https",
        "api.anthropic.com",
        "/v1/messages",
    )
    body = api.body()
    assert set(body) == {"model", "max_tokens", "system", "messages", "output_config"}
    assert body["model"] == MODEL and body["max_tokens"] == 4096
    assert body["system"] == prompt.text
    assert body["output_config"] == {"effort": "low"}
    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(PNG).decode(),
                    },
                }
            ],
        }
    ]
    # The key travels in x-api-key and nowhere else; no Authorization header at all.
    assert request.headers["x-api-key"] == KEY
    assert "authorization" not in request.headers
    others = [v for k, v in request.headers.items() if k.lower() != "x-api-key"]
    assert not any(KEY[12:] in v for v in others)


def test_sdk_environment_cannot_redirect_or_rekey(
    make_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://collector.example.org")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-" + "e" * 40)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "env-bearer-token")
    api = FakeApi(ok(message_json()))
    make_client(api).transcribe(PNG, load_prompt())
    (request,) = api.requests
    assert request.url.host == "api.anthropic.com"
    assert request.headers["x-api-key"] == KEY
    assert "authorization" not in request.headers


def test_sdk_debug_logging_stays_quiet(
    make_client: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Below WARNING the SDK logs request options: the page image and the prompt."""
    caplog.set_level(logging.DEBUG)
    api = FakeApi(ok(message_json()))
    make_client(api).transcribe(PNG, load_prompt())
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("anthropic"))
    assert base64.b64encode(PNG).decode()[:40] not in text
    assert load_prompt().text[:40] not in text
    assert KEY not in caplog.text
    assert logging.getLogger("anthropic").level >= logging.WARNING


def test_thinking_blocks_are_not_transcript_and_bill_as_output(make_client: Any) -> None:
    thinking = {"type": "thinking", "thinking": "reading the page", "signature": "sig"}
    api = FakeApi(ok(message_json("Page text", output_tokens=900, extra_blocks=[thinking])))
    result = make_client(api).transcribe(PNG, load_prompt())
    assert result.text == "Page text"
    assert result.output_tokens == 900  # output_tokens includes thinking (§8.1)


def test_cache_tokens_count_as_input(make_client: Any) -> None:
    payload = message_json()
    payload["usage"].update(cache_creation_input_tokens=10, cache_read_input_tokens=5)
    api = FakeApi(ok(payload))
    assert make_client(api).transcribe(PNG, load_prompt()).input_tokens == 2315


def test_max_tokens_stop_is_returned_for_the_caller_to_judge(make_client: Any) -> None:
    api = FakeApi(ok(message_json("cut off", stop_reason="max_tokens")))
    assert make_client(api).transcribe(PNG, load_prompt()).stop_reason == "max_tokens"


def test_model_refusal_raises_with_its_tokens(make_client: Any) -> None:
    api = FakeApi(ok(message_json("", stop_reason="refusal", input_tokens=2000, output_tokens=7)))
    with pytest.raises(OcrRefused) as info:
        make_client(api).transcribe(PNG, load_prompt())
    assert (info.value.reason, info.value.input_tokens, info.value.output_tokens) == (
        "refused",
        2000,
        7,
    )
    assert info.value.code == "refused" and not info.value.about_key


# ---- images ---------------------------------------------------------------------------------


def test_check_png() -> None:
    assert check_png(PNG) == (40, 60)
    assert check_png(png_header(MAX_IMAGE_SIDE, 10)) == (MAX_IMAGE_SIDE, 10)


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", "image_invalid"),
        (b"GIF89a" + b"\0" * 40, "image_invalid"),
        (png_header(0, 10), "image_invalid"),
        (png_header(MAX_IMAGE_SIDE + 1, 10), "image_too_large"),
        (png_header(10, 4000), "image_too_large"),
        (png_header(100, 100) + b"\0" * 3_932_160, "image_too_large"),
    ],
)
def test_bad_images_are_refused_before_anything_is_sent(
    make_client: Any, data: bytes, reason: str
) -> None:
    api = FakeApi()
    with pytest.raises(OcrRefused) as info:
        make_client(api).transcribe(data, load_prompt())
    assert info.value.reason == reason
    assert api.requests == []


# ---- retries and error mapping --------------------------------------------------------------


def test_retries_with_backoff_then_succeeds(make_client: Any, sleeps: Sleeps) -> None:
    api = FakeApi(
        status(529, "overloaded_error"),
        status(429, "rate_limit_error", **{"retry-after": "3"}),
        status(500),
        ok(message_json()),
    )
    assert make_client(api).transcribe(PNG, load_prompt()).text.startswith("# Notes")
    assert len(api.requests) == 4
    assert len(sleeps) == 3
    assert 2 <= sleeps[0] < 3
    assert sleeps[1] == 3.0  # retry-after honoured
    assert 8 <= sleeps[2] < 9


def test_retry_after_beyond_a_minute_is_ignored(make_client: Any, sleeps: Sleeps) -> None:
    api = FakeApi(status(429, "rate_limit_error", **{"retry-after": "3600"}), ok(message_json()))
    make_client(api).transcribe(PNG, load_prompt())
    assert 2 <= sleeps[0] < 3


def test_gives_up_after_three_retries(make_client: Any, sleeps: Sleeps) -> None:
    api = FakeApi(*[status(529, "overloaded_error") for _ in range(4)])
    with pytest.raises(OcrUnavailable) as info:
        make_client(api).transcribe(PNG, load_prompt())
    assert info.value.reason == "overloaded" and info.value.code == "internal"
    assert len(api.requests) == 4 and len(sleeps) == 3
    # Never chained onto the SDK's exception, which holds the request and its headers.
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert KEY not in repr(info.value) and KEY[12:] not in str(info.value)


def test_connection_errors_are_retried(make_client: Any) -> None:
    api = FakeApi(httpx2.ConnectError("refused"), ok(message_json()))
    make_client(api).transcribe(PNG, load_prompt())
    assert len(api.requests) == 2


def test_timeouts_are_not_retried_inline(make_client: Any, sleeps: Sleeps) -> None:
    api = FakeApi(httpx2.ReadTimeout("slow"))
    with pytest.raises(OcrUnavailable) as info:
        make_client(api).transcribe(PNG, load_prompt())
    assert info.value.reason == "timeout"
    assert len(api.requests) == 1 and sleeps == []
    assert info.value.__context__ is None


@pytest.mark.parametrize(
    ("code", "error_type", "reason", "about_key"),
    [
        (400, "invalid_request_error", "invalid_request", False),
        (401, "authentication_error", "bad_key", True),
        (402, "billing_error", "billing", True),
        (403, "permission_error", "forbidden", True),
        (404, "not_found_error", "model_unavailable", True),
        (413, "request_too_large", "request_too_large", False),
    ],
)
def test_verdicts_are_not_retried(
    make_client: Any, sleeps: Sleeps, code: int, error_type: str, reason: str, about_key: bool
) -> None:
    api = FakeApi(status(code, error_type))
    with pytest.raises(OcrRefused) as info:
        make_client(api).transcribe(PNG, load_prompt())
    assert (info.value.reason, info.value.about_key) == (reason, about_key)
    assert len(api.requests) == 1 and sleeps == []
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert "scripted" not in str(info.value)  # nothing of the API's text is passed on


def test_validate_key(make_client: Any) -> None:
    model = {
        "type": "model",
        "id": MODEL,
        "display_name": "Sonnet",
        "created_at": "2026-01-01T00:00:00Z",
    }
    api = FakeApi(ok(model))
    make_client(api).validate_key()
    (request,) = api.requests
    assert (request.method, request.url.path) == ("GET", f"/v1/models/{MODEL}")
    assert request.content == b""  # nothing but the key and the model id
    api = FakeApi(status(401, "authentication_error"))
    with pytest.raises(OcrRefused) as info:
        make_client(api).validate_key()
    assert info.value.reason == "bad_key"


def test_repr_never_shows_the_key(make_client: Any) -> None:
    client = make_client(FakeApi())
    assert KEY not in repr(client) and KEY[:12] not in repr(client)
    assert repr(client) == "OcrClient(model='claude-sonnet-5-5')"


def test_client_for_uses_the_settings() -> None:
    settings = make_settings(ocr_model="claude-opus-5-5", ocr_max_tokens=2048)
    with client_for(settings, SecretStr(KEY)) as client:
        assert client.model == "claude-opus-5-5"


def test_constructor_refuses_a_plain_string_key() -> None:
    with pytest.raises(TypeError):
        OcrClient(KEY, model=MODEL, max_tokens=4096)  # type: ignore[arg-type]


# ---- batches --------------------------------------------------------------------------------


def test_submit_batch_sends_one_request_per_page(make_client: Any) -> None:
    api = FakeApi(ok(batch_json()))
    items = [BatchItem("p-1", PNG), BatchItem("p_2", png_of(10, 10))]
    assert make_client(api).submit_batch(items, load_prompt()) == "msgbatch_01"
    (request,) = api.requests
    assert request.url.path == "/v1/messages/batches"
    requests = api.body()["requests"]
    assert [r["custom_id"] for r in requests] == ["p-1", "p_2"]
    assert set(requests[0]["params"]) == {
        "model",
        "max_tokens",
        "system",
        "messages",
        "output_config",
    }
    assert set(api.body()) == {"requests"}


@pytest.mark.parametrize(
    "items",
    [
        [],
        [BatchItem("a b", PNG)],
        [BatchItem("x" * 65, PNG)],
        [BatchItem("dup", PNG), BatchItem("dup", PNG)],
    ],
)
def test_submit_batch_validates_items(make_client: Any, items: list[BatchItem]) -> None:
    api = FakeApi()
    with pytest.raises(InvalidArgumentError):
        make_client(api).submit_batch(items, load_prompt())
    assert api.requests == []


def test_submit_batch_checks_every_image_first(make_client: Any) -> None:
    api = FakeApi()
    with pytest.raises(OcrRefused):
        make_client(api).submit_batch([BatchItem("a", PNG), BatchItem("b", b"nope")], load_prompt())
    assert api.requests == []


def test_submit_batch_never_retries_after_a_server_error(make_client: Any) -> None:
    """A 5xx may come after the batch was created: a retry would pay for every page twice."""
    api = FakeApi(status(500))
    with pytest.raises(OcrUnavailable):
        make_client(api).submit_batch([BatchItem("a", PNG)], load_prompt())
    assert len(api.requests) == 1
    api = FakeApi(httpx2.ConnectError("reset"))
    with pytest.raises(OcrUnavailable):
        make_client(api).submit_batch([BatchItem("a", PNG)], load_prompt())
    assert len(api.requests) == 1
    api = FakeApi(status(529, "overloaded_error"), ok(batch_json()))
    assert make_client(api).submit_batch([BatchItem("a", PNG)], load_prompt()) == "msgbatch_01"


def test_batch_status(make_client: Any) -> None:
    api = FakeApi(
        ok(batch_json()),
        ok(
            batch_json(
                "ended",
                results_url=RESULTS_URL,
                counts={"processing": 0, "succeeded": 1, "errored": 1, "canceled": 0, "expired": 0},
            )
        ),
        ok(batch_json("ended", results_url=RESULTS_URL, archived=True)),
        status(404, "not_found_error"),
    )
    client = make_client(api)
    assert client.batch_status("msgbatch_01") == BatchStatus(
        "in_progress", BatchCounts(processing=2)
    )
    assert client.batch_status("msgbatch_01") == BatchStatus(
        "ended", BatchCounts(succeeded=1, errored=1)
    )
    assert client.batch_status("msgbatch_01").state == "expired"  # archived: results gone
    assert client.batch_status("msgbatch_01") == BatchStatus("expired", BatchCounts())
    assert api.requests[0].url.path == "/v1/messages/batches/msgbatch_01"
    with pytest.raises(InvalidArgumentError):
        client.batch_status("../models")


def _jsonl(*lines: dict[str, Any] | str) -> bytes:
    return b"".join(
        (line if isinstance(line, str) else json.dumps(line)).encode() + b"\n" for line in lines
    )


def test_batch_results_stream(make_client: Any) -> None:
    ended = ok(batch_json("ended", results_url=RESULTS_URL))
    body = _jsonl(
        {"custom_id": "p-1", "result": {"type": "succeeded", "message": message_json("one")}},
        {
            "custom_id": "p-2",
            "result": {
                "type": "errored",
                "error": {
                    "type": "error",
                    "error": {"type": "authentication_error", "message": "x"},
                },
            },
        },
        {
            "custom_id": "p-3",
            "result": {
                "type": "errored",
                "error": {"type": "error", "error": {"type": "overloaded_error", "message": "x"}},
            },
        },
        {"custom_id": "p-4", "result": {"type": "canceled"}},
        {"custom_id": "p-5", "result": {"type": "expired"}},
        {
            "custom_id": "p-6",
            "result": {
                "type": "succeeded",
                "message": message_json("", stop_reason="refusal", output_tokens=3),
            },
        },
        {"custom_id": "../../etc", "result": {"type": "canceled"}},  # untrusted: skipped
    )
    api = FakeApi(ended, ended, httpx2.Response(200, content=body))
    results = dict(make_client(api).batch_results("msgbatch_01"))
    assert results == {
        "p-1": Transcription("one", MODEL, 2300, 120, "end_turn", batch=True),
        "p-2": OcrItemError("bad_key", False),
        "p-3": OcrItemError("overloaded", True),
        "p-4": OcrItemError("canceled", True),
        "p-5": OcrItemError("expired", True),
        "p-6": OcrItemError("refused", False, 2300, 3),
    }
    assert api.requests[-1].url.host == "api.anthropic.com"
    assert api.requests[-1].headers["x-api-key"] == KEY


def test_batch_results_before_the_end(make_client: Any) -> None:
    api = FakeApi(ok(batch_json()))
    with pytest.raises(OcrUnavailable) as info:
        make_client(api).batch_results("msgbatch_01")
    assert info.value.reason == "batch_in_progress"


def test_batch_results_never_sends_the_key_elsewhere(make_client: Any) -> None:
    api = FakeApi(ok(batch_json("ended", results_url="https://files.example.org/results")))
    with pytest.raises(OcrRefused) as info:
        make_client(api).batch_results("msgbatch_01")
    assert info.value.reason == "unexpected_response"
    assert len(api.requests) == 1


def test_batch_results_broken_stream(make_client: Any) -> None:
    ended = ok(batch_json("ended", results_url=RESULTS_URL))
    body = _jsonl(
        {"custom_id": "p-1", "result": {"type": "canceled"}},
        "{not json",
    )
    api = FakeApi(ended, ended, httpx2.Response(200, content=body))
    stream = make_client(api).batch_results("msgbatch_01")
    assert next(stream) == ("p-1", OcrItemError("canceled", True))
    with pytest.raises(OcrUnavailable) as info:
        next(stream)
    assert info.value.reason == "stream_failed"
    assert info.value.__context__ is None


# ---- the SDK stays in one module ------------------------------------------------------------


def test_only_the_client_imports_the_sdk() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "scriptorium"
    importers = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if any(
            line.lstrip().startswith(("import anthropic", "from anthropic"))
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    )
    assert importers == ["ocr/client.py"]


# ---- the test suite's own guard -------------------------------------------------------------


def test_no_test_can_reach_the_claude_api() -> None:
    """tests/conftest.py refuses httpx2's network transport outside tests/live (§4)."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    with pytest.raises(AssertionError, match="network"):
        httpx2.HTTPTransport().handle_request(request)
    # A real client without a mocked transport fails before anything leaves the machine.
    client = OcrClient(SecretStr(KEY), model=MODEL, max_tokens=1024, sleep=lambda _s: None)
    with client, pytest.raises(OcrUnavailable) as caught:
        client.validate_key()
    assert caught.value.reason == "connection"
