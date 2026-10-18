"""CloudClient: bounds, failure classes, verification, identity and registration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from remarkable_mcp import sync as fork

from scriptorium.cloud import client as client_module
from scriptorium.cloud.client import FILES_URL, CloudClient
from scriptorium.cloud.errors import (
    BlobVerificationError,
    CloudNotFoundError,
    CloudProtocolError,
    CloudRequestError,
    RateLimitedError,
    ResponseTooLargeError,
    TokenRejectedError,
    TransientCloudError,
)
from scriptorium.errors import InvalidArgumentError, RefusedError
from tests.cloud.fake_cloud import (
    DEVICE_TOKEN,
    NOTEBOOK,
    PAGES,
    PDF,
    ROOT_KEY,
    TOKEN_KEY,
    FakeCloud,
    fixture_documents,
    sha256,
)

PAGE_101 = f"{NOTEBOOK}/{PAGES[0]}.rm"


def make(cloud: FakeCloud, tmp_path: Path, **kwargs: Any) -> CloudClient:
    return CloudClient(
        DEVICE_TOKEN,
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(cloud.handler),
        **kwargs,
    )


@pytest.fixture
def cloud() -> FakeCloud:
    c = FakeCloud()
    c.put_documents(fixture_documents())
    return c


# ---- construction ------------------------------------------------------------------------


def test_fork_config_is_explicit_and_ignores_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cloud: FakeCloud
) -> None:
    monkeypatch.setenv("REMARKABLE_CACHE_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("REMARKABLE_RETRY_ATTEMPTS", "9")
    monkeypatch.setenv("REMARKABLE_SYNC_WORKERS", "32")
    client = make(cloud, tmp_path)

    config = client.fork_config
    assert config.cache_dir == tmp_path / "cache"
    assert config.resolved_cache_dir == tmp_path / "cache"
    assert config.cache_enabled is False
    assert config.retry_attempts == 1
    assert config.retry_delay == 0
    assert config.sync_workers == 1


def test_timeouts_and_environment_isolation(tmp_path: Path, cloud: FakeCloud) -> None:
    client = make(cloud, tmp_path, http_timeout_s=12)
    http = client._http
    assert http.timeout == httpx.Timeout(12)
    assert http.trust_env is False
    with pytest.raises(InvalidArgumentError):
        make(cloud, tmp_path, http_timeout_s=0)
    with pytest.raises(InvalidArgumentError):
        make(cloud, tmp_path, http_timeout_s=601)


@pytest.mark.parametrize("token", ["", "a b", "tok\nen", "x" * 20_000, "tök"])
def test_device_token_must_look_like_a_token(token: str, tmp_path: Path) -> None:
    with pytest.raises(InvalidArgumentError):
        CloudClient(token, cache_dir=tmp_path)


def test_repr_hides_the_token(tmp_path: Path, cloud: FakeCloud) -> None:
    assert DEVICE_TOKEN not in repr(make(cloud, tmp_path))


def test_from_device_token(tmp_path: Path, cloud: FakeCloud) -> None:
    client = CloudClient.from_device_token(
        DEVICE_TOKEN,
        cache_dir=tmp_path,
        http_timeout_s=5,
        transport=httpx.MockTransport(cloud.handler),
    )
    with client:
        assert client.root().generation == cloud.generation


# ---- identity and registration -----------------------------------------------------------


def _jwt(claims: dict[str, Any]) -> str:
    return jwt.encode(claims, "k" * 32, algorithm="HS256")


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (_jwt({"auth0-userid": "auth0|abc123", "device-id": "x"}), "auth0|abc123"),
        (_jwt({"device-id": "x"}), None),
        (_jwt({"auth0-userid": 42}), None),
        (_jwt({"auth0-userid": "bad\nvalue"}), None),
        (_jwt({"auth0-userid": "x" * 300}), None),
        ("not-a-jwt", None),
    ],
)
def test_account_identity(token: str, expected: str | None, tmp_path: Path) -> None:
    assert CloudClient(token, cache_dir=tmp_path).account_identity() == expected


def test_register_device_wraps_the_fork(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_register(code: str, device_desc: str = "") -> dict[str, str]:
        calls.append((code, device_desc))
        return {"devicetoken": "new.device.token"}

    monkeypatch.setattr(fork, "register_device", fake_register)
    assert CloudClient.register_device("abcd1234", "scriptorium-01") == "new.device.token"
    assert calls == [("abcd1234", "scriptorium-01")]


@pytest.mark.parametrize(
    ("code", "description"),
    [("ab", "desc"), ("abcd 123", "desc"), ("abcd1234", "two words"), ("abcd1234", "")],
)
def test_register_device_validates_its_arguments(
    code: str, description: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fork, "register_device", pytest.fail)
    with pytest.raises(InvalidArgumentError):
        CloudClient.register_device(code, description)


@pytest.mark.parametrize("result", [RuntimeError("HTTP 400: body"), {"devicetoken": ""}, {}])
def test_register_device_failures_are_refused(result: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_register(code: str, device_desc: str = "") -> Any:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(fork, "register_device", fake_register)
    with pytest.raises(RefusedError) as info:
        CloudClient.register_device("abcd1234", "scriptorium")
    assert "body" not in str(info.value)


# ---- the token endpoint ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, TokenRejectedError),
        (403, TokenRejectedError),
        (429, RateLimitedError),
        (500, TransientCloudError),
        (404, TransientCloudError),
    ],
)
def test_token_endpoint_statuses(
    status: int, error: type[Exception], tmp_path: Path, cloud: FakeCloud
) -> None:
    cloud.status[TOKEN_KEY] = status
    with pytest.raises(error):
        make(cloud, tmp_path).root()
    assert cloud.targets() == [TOKEN_KEY]


def test_token_endpoint_garbage_is_transient(tmp_path: Path, cloud: FakeCloud) -> None:
    cloud.user_token = "<html>maintenance</html>"
    with pytest.raises(TransientCloudError):
        make(cloud, tmp_path).root()


def test_token_endpoint_unreachable_is_transient(tmp_path: Path, cloud: FakeCloud) -> None:
    cloud.errors[TOKEN_KEY] = "connect"
    with pytest.raises(TransientCloudError):
        make(cloud, tmp_path).root()


def test_an_expired_user_token_is_renewed_once(tmp_path: Path, cloud: FakeCloud) -> None:
    client = make(cloud, tmp_path)
    client.root()
    cloud.user_token = "rotated-user-token"

    assert client.root().hash == cloud.root_hash
    assert cloud.targets().count(TOKEN_KEY) == 2


# ---- statuses and bounds -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (404, CloudNotFoundError),
        (410, CloudNotFoundError),
        (418, CloudRequestError),
        (403, TransientCloudError),
        (502, TransientCloudError),
        (429, RateLimitedError),
    ],
)
def test_document_index_statuses(
    status: int, error: type[Exception], tmp_path: Path, cloud: FakeCloud
) -> None:
    cloud.status[cloud.doc_hash(NOTEBOOK)] = status
    with pytest.raises(error):
        make(cloud, tmp_path).document_index(NOTEBOOK, cloud.doc_hash(NOTEBOOK))


def test_retry_after_as_an_http_date(tmp_path: Path, cloud: FakeCloud) -> None:
    cloud.status[ROOT_KEY] = 429
    cloud.headers[ROOT_KEY] = {"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"}
    with pytest.raises(RateLimitedError) as info:
        make(cloud, tmp_path).root()
    assert info.value.retry_after_s == client_module.MAX_RETRY_AFTER_S


def test_missing_root_index_is_transient(tmp_path: Path, cloud: FakeCloud) -> None:
    cloud.status[cloud.root_hash] = 404
    with pytest.raises(TransientCloudError):
        make(cloud, tmp_path).root()


def test_an_unchanged_root_is_not_fetched_twice(tmp_path: Path, cloud: FakeCloud) -> None:
    client = make(cloud, tmp_path)
    first = client.root()
    second = client.root()
    assert first.entries == second.entries
    assert cloud.targets().count(cloud.root_hash) == 1


def test_document_index_must_match_its_hash(tmp_path: Path, cloud: FakeCloud) -> None:
    # The notebook's address answers with the PDF's (valid) index.
    cloud.corrupt[cloud.doc_hash(NOTEBOOK)] = cloud.blobs[cloud.doc_hash(PDF)]
    with pytest.raises(CloudProtocolError):
        make(cloud, tmp_path).document_index(NOTEBOOK, cloud.doc_hash(NOTEBOOK))


def test_document_index_arguments_are_validated(tmp_path: Path, cloud: FakeCloud) -> None:
    client = make(cloud, tmp_path)
    with pytest.raises(InvalidArgumentError):
        client.document_index("../x", "a" * 64)
    with pytest.raises(InvalidArgumentError):
        client.document_index(NOTEBOOK, "../" + "a" * 61)
    with pytest.raises(InvalidArgumentError):
        with client.open_blob("../etc/passwd"):
            pass


def test_an_endless_index_is_refused_without_reading_it_all(
    tmp_path: Path, cloud: FakeCloud
) -> None:
    cloud.endless.add(cloud.doc_hash(NOTEBOOK))
    client = make(cloud, tmp_path, max_index_bytes=100_000)
    with pytest.raises(ResponseTooLargeError):
        client.document_index(NOTEBOOK, cloud.doc_hash(NOTEBOOK))


def test_an_endless_root_pointer_is_refused(tmp_path: Path, cloud: FakeCloud) -> None:
    cloud.endless.add(ROOT_KEY)
    with pytest.raises(ResponseTooLargeError):
        make(cloud, tmp_path).root()


def test_a_declared_content_length_over_the_cap_is_refused_up_front(
    tmp_path: Path, cloud: FakeCloud
) -> None:
    cloud.headers[cloud.root_hash] = {"content-length": str(10**9)}
    with pytest.raises(ResponseTooLargeError):
        make(cloud, tmp_path).root()


def test_too_many_root_entries_are_refused(tmp_path: Path, cloud: FakeCloud) -> None:
    with pytest.raises(ResponseTooLargeError):
        make(cloud, tmp_path, max_root_entries=1).root()


# ---- blobs -------------------------------------------------------------------------------


def _page(cloud: FakeCloud) -> tuple[str, bytes]:
    data = cloud.docs[NOTEBOOK][PAGE_101]
    return sha256(data), data


def test_open_blob_streams_verified_bytes(tmp_path: Path, cloud: FakeCloud) -> None:
    sha, data = _page(cloud)
    with make(cloud, tmp_path).open_blob(sha, max_bytes=len(data)) as stream:
        assert stream.read() == data


def test_a_blob_that_does_not_match_its_hash_raises_at_the_end(
    tmp_path: Path, cloud: FakeCloud
) -> None:
    sha, data = _page(cloud)
    cloud.corrupt[sha] = b"y" * len(data)
    with pytest.raises(BlobVerificationError):
        with make(cloud, tmp_path).open_blob(sha, max_bytes=len(data)) as stream:
            stream.read()


def test_a_consumer_that_stops_early_still_gets_the_verdict(
    tmp_path: Path, cloud: FakeCloud
) -> None:
    sha, data = _page(cloud)
    cloud.corrupt[sha] = b"y" * len(data)
    with pytest.raises(BlobVerificationError):
        with make(cloud, tmp_path).open_blob(sha, max_bytes=len(data)) as stream:
            stream.read(10)


def test_a_blob_larger_than_declared_is_refused(tmp_path: Path, cloud: FakeCloud) -> None:
    sha, data = _page(cloud)
    with pytest.raises(ResponseTooLargeError):
        with make(cloud, tmp_path).open_blob(sha, max_bytes=len(data) - 1) as stream:
            stream.read()


def test_an_endless_blob_is_cut_off_at_its_declared_size(tmp_path: Path, cloud: FakeCloud) -> None:
    sha, data = _page(cloud)
    cloud.endless.add(sha)
    with pytest.raises(ResponseTooLargeError):
        with make(cloud, tmp_path).open_blob(sha, max_bytes=len(data)) as stream:
            while stream.read(65536):
                pass


def test_a_blob_declared_over_the_hard_cap_is_refused(tmp_path: Path, cloud: FakeCloud) -> None:
    sha, _data = _page(cloud)
    with pytest.raises(ResponseTooLargeError):
        with make(cloud, tmp_path).open_blob(sha, max_bytes=client_module.MAX_BLOB_BYTES + 1):
            pass
    assert sha not in cloud.targets()


def test_a_stalled_transfer_is_transient(
    tmp_path: Path, cloud: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha, data = _page(cloud)
    client = make(cloud, tmp_path)
    monkeypatch.setattr(client, "_deadline", lambda size: 0.0)
    with pytest.raises(TransientCloudError):
        with client.open_blob(sha, max_bytes=len(data)) as stream:
            stream.read()


# ---- transport ---------------------------------------------------------------------------


def test_network_failures_are_transient(tmp_path: Path, cloud: FakeCloud) -> None:
    cloud.errors[ROOT_KEY] = "read_timeout"
    with pytest.raises(TransientCloudError):
        make(cloud, tmp_path).root()


def test_a_redirect_to_plain_http_is_refused(tmp_path: Path, cloud: FakeCloud) -> None:
    target = f"{FILES_URL}/{cloud.root_hash}"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == target:
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
        return cloud.handler(request)

    client = CloudClient(DEVICE_TOKEN, cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    with pytest.raises(CloudRequestError):
        client.root()


def test_endless_redirects_are_a_protocol_error(tmp_path: Path, cloud: FakeCloud) -> None:
    target = f"{FILES_URL}/{cloud.root_hash}"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(target):
            return httpx.Response(302, headers={"location": target + "?again"})
        return cloud.handler(request)

    client = CloudClient(DEVICE_TOKEN, cache_dir=tmp_path, transport=httpx.MockTransport(handler))
    with pytest.raises(CloudProtocolError):
        client.root()


def test_errors_carry_no_token_or_body(tmp_path: Path, cloud: FakeCloud) -> None:
    cloud.status[cloud.doc_hash(NOTEBOOK)] = 500
    with pytest.raises(TransientCloudError) as info:
        make(cloud, tmp_path).document_index(NOTEBOOK, cloud.doc_hash(NOTEBOOK))
    text = str(info.value) + repr(info.value.__dict__)
    assert DEVICE_TOKEN not in text and cloud.user_token not in text
    assert info.value.__cause__ is None
