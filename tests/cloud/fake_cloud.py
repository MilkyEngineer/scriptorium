"""An in-memory reMarkable sync 1.5 service, as an ``httpx.MockTransport`` handler.

Built the way the fork's own tests build theirs: documents are xochitl file maps, each
document index is serialised with the fork's ``_serialize_doc_index`` and stored under
its ``_hash_entries`` address, and the root with ``_serialize_root_index`` (schema 4,
addressed by the SHA-256 of its body) or as schema 3 (``HashEntries`` address). Every
mutation publishes a new root under a new generation.

Faults are injected per request target: an HTTP status, extra response headers, a body
served in place of the real one (corruption), a transport error or an endless body.
Content comes only from the S3 fixture notebook, so nothing here is a real note.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from remarkable_mcp import sync as fork

from scriptorium.cloud.client import FILES_URL, ROOT_URL, USER_TOKEN_URL

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "move-test-notebook"
DEVICE_TOKEN = "test-device-token"
USER_TOKEN = "test-user-token"

NOTEBOOK = "00000000-0000-4000-8000-000000000001"
PDF = "00000000-0000-4000-8000-000000000002"
PAGES = tuple(f"00000000-0000-4000-8000-000000000{n}" for n in range(101, 108))
"""The fixture's page UUIDs; synthetic scenarios also use them as document UUIDs."""

ROOT_KEY = "root"
TOKEN_KEY = "token"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fixture_documents() -> dict[str, dict[str, bytes]]:
    """``{doc uuid: {xochitl-relative name: bytes}}`` for the fixture notebook and PDF."""
    docs: dict[str, dict[str, bytes]] = {}
    for kind in ("notebook", "pdf"):
        base = FIXTURE_ROOT / kind / "xochitl"
        files = {
            p.relative_to(base).as_posix(): p.read_bytes()
            for p in sorted(base.rglob("*"))
            if p.is_file()
        }
        uuid = next(name.split(".")[0] for name in files if name.endswith(".metadata"))
        docs[uuid] = files
    return docs


def renamed(files: Mapping[str, bytes], old: str, new: str) -> dict[str, bytes]:
    """The same fixture bytes filed under another document UUID."""
    return {name.replace(old, new, 1): data for name, data in files.items()}


@dataclass
class FakeCloud:
    root_schema: int = 4
    device_token: str = DEVICE_TOKEN
    user_token: str = USER_TOKEN
    generation: int = 0
    docs: dict[str, dict[str, bytes]] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    root_hash: str = ""
    status: dict[str, int] = field(default_factory=dict)
    """Target (a hash, ``ROOT_KEY`` or ``TOKEN_KEY``) → HTTP status to answer with."""
    headers: dict[str, dict[str, str]] = field(default_factory=dict)
    corrupt: dict[str, bytes] = field(default_factory=dict)
    """Hash → body served instead of the real one."""
    errors: dict[str, str] = field(default_factory=dict)
    """Target → ``"connect"`` or ``"read_timeout"``."""
    endless: set[str] = field(default_factory=set)
    requests: list[tuple[str, str, str | None]] = field(default_factory=list)
    """``(method, target, rm-filename)`` for every request received."""

    def __post_init__(self) -> None:
        self.publish()

    # ---- the library ------------------------------------------------------------------

    def put_document(self, uuid: str, files: Mapping[str, bytes]) -> None:
        self.docs[uuid] = dict(files)
        self.publish()

    def put_documents(self, docs: Mapping[str, Mapping[str, bytes]]) -> None:
        """Several documents in one new generation."""
        for uuid, files in docs.items():
            self.docs[uuid] = dict(files)
        self.publish()

    def remove_document(self, *uuids: str) -> None:
        for uuid in uuids:
            del self.docs[uuid]
        self.publish()

    def set_file(self, uuid: str, name: str, data: bytes) -> None:
        self.docs[uuid][name] = data
        self.publish()

    def doc_hash(self, uuid: str) -> str:
        return fork._hash_entries(self._entries(uuid))

    def publish(self, *, generation: int | None = None) -> None:
        """Store every blob and index and point the root at the result."""
        roots: list[dict[str, Any]] = []
        for uuid in sorted(self.docs):
            entries = self._entries(uuid)
            for data in self.docs[uuid].values():
                self.blobs[sha256(data)] = data
            index = fork._serialize_doc_index(entries)
            doc_hash = fork._hash_entries(entries)
            self.blobs[doc_hash] = index
            roots.append(
                {
                    "hash": doc_hash,
                    "id": uuid,
                    "subfiles": len(entries),
                    "size": sum(e["size"] for e in entries),
                }
            )
        if self.root_schema == 4:
            body = fork._serialize_root_index(roots)
            self.root_hash = sha256(body)
        else:
            body = (
                "3\n"
                + "".join(
                    f"{r['hash']}:80000000:{r['id']}:{r['subfiles']}:{r['size']}\n" for r in roots
                )
            ).encode()
            self.root_hash = fork._hash_entries(roots)
        self.blobs[self.root_hash] = body
        self.generation = self.generation + 1 if generation is None else generation

    def _entries(self, uuid: str) -> list[dict[str, Any]]:
        return [
            {"hash": sha256(data), "id": name, "size": len(data)}
            for name, data in sorted(self.docs[uuid].items())
        ]

    # ---- HTTP -------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        auth = request.headers.get("authorization", "")
        filename = request.headers.get("rm-filename")
        if request.method == "POST" and url == USER_TOKEN_URL:
            self.requests.append(("POST", TOKEN_KEY, None))
            self._raise_error(TOKEN_KEY, request)
            if auth != f"Bearer {self.device_token}":
                return httpx.Response(401)
            return self._answer(TOKEN_KEY, self.user_token.encode(), "text/plain")
        if request.method != "GET":
            return httpx.Response(405)
        if auth != f"Bearer {self.user_token}":
            return httpx.Response(401)
        if url == ROOT_URL:
            self.requests.append(("GET", ROOT_KEY, filename))
            self._raise_error(ROOT_KEY, request)
            pointer = {
                "hash": self.root_hash,
                "generation": self.generation,
                "schemaVersion": self.root_schema,
            }
            return self._answer(ROOT_KEY, json.dumps(pointer).encode(), "application/json")
        if url.startswith(f"{FILES_URL}/"):
            target = url.removeprefix(f"{FILES_URL}/")
            self.requests.append(("GET", target, filename))
            self._raise_error(target, request)
            body = self.corrupt.get(target, self.blobs.get(target))
            if body is None:
                return httpx.Response(404)
            return self._answer(target, body, "application/octet-stream")
        return httpx.Response(404)

    def _answer(self, target: str, body: bytes, content_type: str) -> httpx.Response:
        status = self.status.get(target, 200)
        headers = {"content-type": content_type, **self.headers.get(target, {})}
        if status != 200:
            return httpx.Response(status, headers=headers)
        if target in self.endless:
            return httpx.Response(200, headers=headers, content=_endless(body))
        return httpx.Response(200, headers=headers, content=body)

    def _raise_error(self, target: str, request: httpx.Request) -> None:
        kind = self.errors.get(target)
        if kind == "connect":
            raise httpx.ConnectError("connection refused", request=request)
        if kind == "read_timeout":
            raise httpx.ReadTimeout("timed out", request=request)

    def targets(self) -> list[str]:
        return [target for _method, target, _name in self.requests]


def _endless(seed: bytes) -> Iterator[bytes]:
    chunk = (seed or b"x") * (65536 // max(len(seed), 1) + 1)
    while True:
        yield chunk
