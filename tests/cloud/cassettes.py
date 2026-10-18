"""Cassettes: scrubbed request/response pairs for the cloud client (plan §3.7).

**Player.** ``Player`` is an ``httpx.MockTransport`` that answers each request with the
next recorded response for the same method and (host-scrubbed) URL; the last response
for a URL is sticky, so a re-read gets the same answer. ``CloudClient`` takes it as its
``transport``; the fork's ``_issue_request`` is not involved because the client never
reads through the fork (see ``cloud/client.py``). A request with no recorded answer is a
test failure, not a silent 404.

**Recorder.** ``Recorder`` is a transport that passes requests to an inner transport and
keeps the exchanges. ``save`` scrubs them and refuses to write the cassette at all if
what remains contains an ``Authorization`` value, anything shaped like a bearer token or
JWT, a UUID that is not in the fixture's ``fixture.json``, or a file blob that is not
byte-identical to one of the S3 fixture's files. Hosts become ``cloud.example.com``; the
user token body becomes a placeholder; error bodies are dropped; file blobs are stored
as references to fixture files, never inline.

``Recorder.synthetic`` wraps an in-memory handler (``fake_cloud.FakeCloud``): that is how
every committed cassette was made. ``Recorder.live`` wraps the network and works only
with ``SCRIPTORIUM_LIVE_TEST=1`` and a device token file outside the repository. Agents
never run it; it exists for the operator's scratch account.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from scriptorium.cloud.client import FILES_URL, ROOT_URL, USER_TOKEN_URL
from scriptorium.cloud.ports import is_safe_name
from scriptorium.cloud.protocol import parse_index, parse_root_pointer

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "move-test-notebook"
CASSETTE_DIR = REPO_ROOT / "tests" / "fixtures" / "cassettes"

FORMAT = 1
SCRUB_HOST = "cloud.example.com"
SCRUBBED = "[scrubbed]"
SCRUBBED_USER_TOKEN = "scrubbed-user-token"
"""Stands in for the user token body; token-shaped so the client accepts it on replay."""

_KEEP_REQUEST_HEADERS = ("rm-filename",)
_KEEP_RESPONSE_HEADERS = ("content-type", "retry-after")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")
_BEARER_RE = re.compile(r"bearer\s*[:=]?\s*(?!\[scrubbed\])\S{6,}", re.I)
_ERROR_KINDS = {"connect": httpx.ConnectError, "read_timeout": httpx.ReadTimeout}


class CassetteRefusedError(Exception):
    """The cassette would carry something that must not be committed."""


def _scrub_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.query or parts.fragment:
        raise CassetteRefusedError("only plain https URLs are recorded")
    return f"https://{SCRUB_HOST}{parts.path}"


_TOKEN_PATH = urlsplit(USER_TOKEN_URL).path
_ROOT_PATH = urlsplit(ROOT_URL).path
_FILES_PATH = urlsplit(FILES_URL).path + "/"


@cache
def fixture_uuids() -> frozenset[str]:
    """Every UUID ``fixture.json`` names: the only ones a cassette may contain."""
    data = json.loads((FIXTURE_ROOT / "fixture.json").read_text())
    out: set[str] = set()
    for doc in data.values():
        out.add(doc["doc"])
        out.update(doc["pages"])
    return frozenset(out)


@cache
def fixture_blobs() -> Mapping[str, str]:
    """SHA-256 → fixture-relative path, for every file of the fixture's xochitl trees."""
    out: dict[str, str] = {}
    for path in sorted(FIXTURE_ROOT.glob("*/xochitl/**/*")):
        if path.is_file():
            out.setdefault(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.relative_to(FIXTURE_ROOT).as_posix(),
            )
    return out


def _fixture_bytes(ref: str) -> bytes:
    if ref not in set(fixture_blobs().values()):
        raise CassetteRefusedError("a fixture reference that is not a fixture file")
    return (FIXTURE_ROOT / ref).read_bytes()


# ---- recording -------------------------------------------------------------------------


@dataclass
class _Exchange:
    method: str
    url: str
    request_headers: dict[str, str]
    status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    error: str | None = None


class Recorder(httpx.BaseTransport):
    """A transport that keeps every exchange, for ``save`` to scrub and check."""

    def __init__(self, inner: httpx.BaseTransport, *, _live: bool = False) -> None:
        if not isinstance(inner, httpx.MockTransport) and not _live:
            raise CassetteRefusedError("recording from the network needs Recorder.live()")
        self._inner = inner
        self._live = _live
        self.exchanges: list[_Exchange] = []

    @classmethod
    def synthetic(cls, handler: Any) -> Recorder:
        return cls(httpx.MockTransport(handler))

    @classmethod
    def live(cls, token_file: Path) -> tuple[Recorder, str]:
        """A network recorder and the scratch device token. Operator use only."""
        if os.environ.get("SCRIPTORIUM_LIVE_TEST") != "1":
            raise CassetteRefusedError("live recording needs SCRIPTORIUM_LIVE_TEST=1")
        resolved = token_file.resolve(strict=True)
        if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
            raise CassetteRefusedError("the device token file must live outside the repository")
        token = resolved.read_text().strip()
        return cls(httpx.HTTPTransport(), _live=True), token

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        exchange = _Exchange(
            method=request.method,
            url=str(request.url),
            request_headers={k.lower(): v for k, v in request.headers.items()},
        )
        self.exchanges.append(exchange)
        try:
            response = self._inner.handle_request(request)
        except httpx.ConnectError:
            exchange.error = "connect"
            raise
        except httpx.ReadTimeout:
            exchange.error = "read_timeout"
            raise
        body = response.read()
        exchange.status = response.status_code
        exchange.response_headers = {k.lower(): v for k, v in response.headers.items()}
        exchange.body = body
        return httpx.Response(
            response.status_code, headers=response.headers, content=body, request=request
        )

    def cassette(self, *, description: str) -> dict[str, Any]:
        return build_cassette(self.exchanges, description=description, synthetic=not self._live)

    def save(self, path: Path, *, description: str) -> None:
        save_cassette(path, self.cassette(description=description))


def build_cassette(
    exchanges: Iterable[_Exchange], *, description: str, synthetic: bool
) -> dict[str, Any]:
    """Scrub recorded exchanges into a cassette, then check it; raise rather than leak."""
    blobs = fixture_blobs()
    interactions: list[dict[str, Any]] = []
    for ex in exchanges:
        path = urlsplit(ex.url).path
        request: dict[str, Any] = {"method": ex.method, "url": _scrub_url(ex.url)}
        headers = {
            k: ex.request_headers[k] for k in _KEEP_REQUEST_HEADERS if k in ex.request_headers
        }
        if "authorization" in ex.request_headers:
            headers["authorization"] = SCRUBBED
        request["headers"] = headers
        if ex.error is not None:
            interactions.append({"request": request, "error": ex.error})
            continue
        assert ex.status is not None
        response: dict[str, Any] = {
            "status": ex.status,
            "headers": {
                k: ex.response_headers[k]
                for k in _KEEP_RESPONSE_HEADERS
                if k in ex.response_headers
            },
        }
        if ex.status == 200:
            digest = hashlib.sha256(ex.body).hexdigest()
            if path == _TOKEN_PATH:
                response["body"] = SCRUBBED_USER_TOKEN
            elif path == _ROOT_PATH:
                response["body"] = _text(ex.body)
            elif digest in blobs:
                response["fixture"] = blobs[digest]
            elif path.startswith(_FILES_PATH) and headers.get("rm-filename", "").endswith(
                ".docSchema"
            ):
                response["body"] = _text(ex.body)
            else:
                raise CassetteRefusedError("a response body that is not a fixture file")
        interactions.append({"request": request, "response": response})
    cassette = {
        "format": FORMAT,
        "synthetic": synthetic,
        "description": description,
        "interactions": interactions,
    }
    check_cassette(cassette)
    return cassette


def _text(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise CassetteRefusedError("a text response that is not UTF-8") from None


def check_cassette(cassette: Mapping[str, Any]) -> None:
    """Everything a committed cassette must satisfy; raises ``CassetteRefusedError``."""
    if cassette.get("format") != FORMAT or not isinstance(cassette.get("synthetic"), bool):
        raise CassetteRefusedError("not a cassette of this format")
    allowed = fixture_uuids()
    for interaction in cassette.get("interactions", []):
        request = interaction["request"]
        parts = urlsplit(request["url"])
        if parts.scheme != "https" or parts.netloc != SCRUB_HOST:
            raise CassetteRefusedError("a URL whose host was not scrubbed")
        headers = request.get("headers", {})
        if set(headers) - {*_KEEP_REQUEST_HEADERS, "authorization"}:
            raise CassetteRefusedError("a request header that is not on the keep list")
        if headers.get("authorization", SCRUBBED) != SCRUBBED:
            raise CassetteRefusedError("an Authorization header value")
        name = headers.get("rm-filename")
        if name is not None and not (
            name == "root.docSchema"
            or (name.endswith(".docSchema") and is_safe_name(name.removesuffix(".docSchema")))
            or is_safe_name(name)
        ):
            raise CassetteRefusedError("an rm-filename outside the allowlist")
        if "error" in interaction:
            if interaction["error"] not in _ERROR_KINDS:
                raise CassetteRefusedError("an unknown transport error")
            continue
        response = interaction["response"]
        if set(response) - {"status", "headers", "body", "fixture"}:
            raise CassetteRefusedError("a response field that is not on the keep list")
        if set(response.get("headers", {})) - set(_KEEP_RESPONSE_HEADERS):
            raise CassetteRefusedError("a response header that is not on the keep list")
        if "fixture" in response:
            _fixture_bytes(response["fixture"])
        if "body" in response:
            _check_body(parts.path, name, response["body"])
    text = json.dumps(cassette)
    if _JWT_RE.search(text) or _BEARER_RE.search(text):
        raise CassetteRefusedError("something shaped like a bearer token")
    for found in _UUID_RE.findall(text):
        if found not in allowed:
            raise CassetteRefusedError("a UUID that is not in fixture.json")


def _check_body(path: str, name: str | None, body: str) -> None:
    raw = body.encode("utf-8")
    try:
        if path == _TOKEN_PATH:
            if body != SCRUBBED_USER_TOKEN:
                raise CassetteRefusedError("a user token body")
        elif path == _ROOT_PATH:
            parse_root_pointer(raw)
        elif path.startswith(_FILES_PATH) and name is not None and name.endswith(".docSchema"):
            parse_index(raw, max_entries=100_000, what="index")
        else:
            raise CassetteRefusedError("an inline body that is not a token, pointer or index")
    except CassetteRefusedError:
        raise
    except Exception as exc:  # a protocol error from the parsers
        raise CassetteRefusedError(f"an inline body that does not parse ({exc})") from None


def save_cassette(path: Path, cassette: Mapping[str, Any]) -> None:
    check_cassette(cassette)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cassette, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def load_cassette(path: Path) -> dict[str, Any]:
    cassette: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    check_cassette(cassette)
    return cassette


# ---- playback --------------------------------------------------------------------------


class Player(httpx.MockTransport):
    """Answers requests from a cassette, in recorded order per (method, URL)."""

    def __init__(self, cassette: Mapping[str, Any]) -> None:
        self._queues: dict[tuple[str, str], deque[Mapping[str, Any]]] = {}
        for interaction in cassette["interactions"]:
            request = interaction["request"]
            key = (request["method"], request["url"])
            self._queues.setdefault(key, deque()).append(interaction)
        self._recorded = {key: len(q) for key, q in self._queues.items()}
        self.played: list[tuple[str, str]] = []
        super().__init__(self._answer)

    @classmethod
    def named(cls, name: str) -> Player:
        return cls(load_cassette(CASSETTE_DIR / f"{name}.json"))

    def _answer(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, _scrub_url(str(request.url)))
        queue = self._queues.get(key)
        if not queue:
            raise AssertionError(f"no recorded answer for {key[0]} {urlsplit(key[1]).path}")
        if "authorization" not in request.headers:
            raise AssertionError("an unauthenticated request")
        interaction = queue.popleft() if len(queue) > 1 else queue[0]
        self.played.append(key)
        if "error" in interaction:
            raise _ERROR_KINDS[interaction["error"]]("recorded transport error", request=request)
        response = interaction["response"]
        if "fixture" in response:
            content = _fixture_bytes(response["fixture"])
        elif "body" in response:
            content = response["body"].encode("utf-8")
        else:
            content = b""
        return httpx.Response(response["status"], headers=response["headers"], content=content)

    def unplayed(self) -> int:
        """Recorded interactions not yet answered (a sticky answer counts once)."""
        used: dict[tuple[str, str], int] = {}
        for key in self.played:
            used[key] = used.get(key, 0) + 1
        return sum(max(0, n - used.get(key, 0)) for key, n in self._recorded.items())
