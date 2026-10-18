"""M1 acceptance through the MCP endpoint: the tool list, latency (#9) and the audit (#11)."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any

import pytest

from scriptorium.logging import JsonFormatter
from tests.cloud.fake_cloud import DEVICE_TOKEN, NOTEBOOK, PDF, fixture_documents, renamed
from tests.test_server import USER_TOOLS, mcp_session
from tests.tools.helpers import Served, meta_of, text_of
from tests.worker.helpers import Linked

M1_TOOLS = {
    "connect_remarkable",
    "disconnect_remarkable",
    "account_status",
    "list",
    "recent",
    "read_page",
    "read_document",
    "page_image",
    "versions",
    "trash",
    "save_transcript",
}
M2_TOOLS = {
    "search",
    "set_claude_api_key",
    "clear_claude_api_key",
    "transcribe",
    "ocr_status",
    "set_auto_ocr",
    "set_sync_scope",
}
ADMIN_TOOLS = {"admin_allow_server_key", "admin_clear_anomaly"}
NOTEBOOK_NAME = "Scriptorium Test Notebook"
TRANSCRIPT_MARKER = "transcript-marker-7f3a"
PATH_MARKER = "path-marker-91c2"
CODE_MARKER = "codemarker77"


async def test_tools_list_for_a_user_and_an_admin(library: Served) -> None:
    async with mcp_session(library.app, library.issuer.token()) as (client, _):
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == M1_TOOLS | M2_TOOLS | USER_TOOLS
    for name in M1_TOOLS | M2_TOOLS:
        tool = tools[name]
        assert tool.description and len(tool.description) > 80, name
        properties = tool.input_schema.get("properties", {})
        # Tenancy: no tool takes a user id or a filesystem path.
        assert not {"user", "uid", "user_id", "path", "file", "filename"} & set(properties), name
    assert tools["list"].annotations is not None and tools["list"].annotations.read_only_hint
    assert tools["search"].annotations is not None and tools["search"].annotations.read_only_hint
    assert tools["save_transcript"].annotations is not None
    assert not tools["save_transcript"].annotations.read_only_hint
    async with mcp_session(library.app, library.issuer.admin_token()) as (client, _):
        admin = {tool.name for tool in (await client.list_tools()).tools}
    assert admin == M1_TOOLS | M2_TOOLS | USER_TOOLS | ADMIN_TOOLS | {"admin_spike_ping"}


async def test_list_and_read_page_p95_under_two_seconds(library: Served) -> None:
    """Acceptance #9: 100 calls on cached data in one session."""
    timings: list[float] = []
    async with mcp_session(library.app, library.issuer.token()) as (client, _):
        for n in range(100):
            if n % 2:
                args: dict[str, Any] = {"document": NOTEBOOK, "page": n % 7 + 1}
                name = "read_page"
            else:
                args, name = {}, "list"
            started = time.perf_counter()
            result = await client.call_tool(name, args)
            timings.append(time.perf_counter() - started)
            assert not result.is_error, text_of(result)
    timings.sort()
    p95 = timings[94]
    assert p95 < 2.0, f"p95 {p95:.3f}s"


def _day_of_usage() -> list[tuple[str, dict[str, Any]]]:
    """Calls a user makes in a day, successful and failing, with marked arguments."""
    return [
        ("account_status", {}),
        ("list", {}),
        ("list", {"folder": f"/{PATH_MARKER}"}),
        ("recent", {"since": "2026-01-01T00:00:00Z"}),
        ("read_page", {"document": NOTEBOOK, "page": 2}),
        ("read_page", {"document": f"/{NOTEBOOK_NAME}", "page": 3}),
        ("read_page", {"document": f"{PATH_MARKER}/{NOTEBOOK_NAME}", "page": 1}),
        ("read_page", {"document": NOTEBOOK, "page": 99}),
        ("read_document", {"document": NOTEBOOK}),
        ("read_document", {"document": NOTEBOOK, "pages": "1-2"}),
        ("page_image", {"document": PDF, "page": 1}),
        ("versions", {"document": NOTEBOOK}),
        ("versions", {"document": NOTEBOOK, "cursor": PATH_MARKER}),
        ("trash", {}),
        ("connect_remarkable", {"code": CODE_MARKER}),
        ("disconnect_remarkable", {"keep_archive": False}),
    ]


async def test_audit_rows_hold_no_arguments_or_content(
    linked: Linked, library: Served, caplog: pytest.LogCaptureFixture
) -> None:
    """Acceptance #11: a grep over every audit column and every log line of a day's calls."""
    caplog.set_level(logging.DEBUG)
    read = await library.call("read_page", {"document": NOTEBOOK, "page": 2})
    meta = meta_of(read)
    saved = await library.ok(
        "save_transcript",
        {
            "document": NOTEBOOK,
            "page": 2,
            "content_hash": meta["content_hash"],
            "text": f"Synthetic line with {TRANSCRIPT_MARKER}",
            "model": "claude-test-model",
        },
    )
    assert saved["saved"] is True
    for name, args in _day_of_usage():
        await library.call(name, args)
    with contextlib.suppress(Exception):  # a protocol error, raised by the client
        await library.call("no_such_tool", {"document": PATH_MARKER})
    # The transcript is returned to its owner...
    read = await library.call("read_page", {"document": NOTEBOOK, "page": 2})
    assert any(TRANSCRIPT_MARKER in text_of(read, i) for i in range(len(read.content)))

    rows = library.audit_rows()
    assert len(rows) >= len(_day_of_usage()) + 2
    stored = json.dumps([list(row) for row in rows], default=str)
    formatter = JsonFormatter()
    # What reaches stderr in production at log level debug: every scriptorium line, and the
    # libraries' warnings (configure_logging holds mcp and httpx at WARNING; the MCP *client*
    # used by this test logs its own requests at DEBUG, which is not the server's log).
    logged = "\n".join(
        formatter.format(record)
        for record in caplog.records
        if record.name.startswith("scriptorium") or record.levelno >= logging.WARNING
    )
    assert "tool_call" in logged
    tokens = (library.issuer.token(), library.issuer.admin_token())
    for marker in (
        TRANSCRIPT_MARKER,
        PATH_MARKER,
        CODE_MARKER,
        NOTEBOOK_NAME,
        "Scriptorium Test PDF",
        "claude-test-model",
        DEVICE_TOKEN,
        library.settings.oidc_issuer,
        *tokens,
    ):
        # ...but never stored in the audit or logged.
        assert marker not in stored, marker
        assert marker not in logged, marker
    user = library.issuer.claims()
    assert user["sub"] not in stored and user["iss"] not in stored  # the hashed id only

    by_tool: dict[str, list[dict[str, Any]]] = {}
    columns = _audit_columns(library)
    for row in rows:
        record = dict(zip(columns, row, strict=True))
        by_tool.setdefault(record["tool"], []).append(record)
    assert "no_such_tool" not in by_tool  # unknown tools are not audited as tools
    first_read = json.loads(by_tool["read_page"][0]["identifiers"])
    assert first_read["acct"] == linked.acct
    assert first_read["uuid"] == NOTEBOOK and first_read["page"] == 2
    assert set(first_read) <= {"acct", "uuid", "page", "generation", "bytes", "job"}
    save = json.loads(by_tool["save_transcript"][0]["identifiers"])
    assert save["bytes"] == len(f"Synthetic line with {TRANSCRIPT_MARKER}")
    assert {r["outcome"] for r in by_tool["read_page"]} >= {"ok", "invalid_argument"}
    assert {r["outcome"] for r in by_tool["connect_remarkable"]} == {"refused"}


def _audit_columns(library: Served) -> list[str]:
    import sqlite3

    from scriptorium.users.store import global_db_path

    conn = sqlite3.connect(global_db_path(library.settings.data_dir))
    try:
        return [row[1] for row in conn.execute("PRAGMA table_info(audit)")]
    finally:
        conn.close()


async def test_an_interrupted_download_is_never_visible(linked: Linked, library: Served) -> None:
    """Acceptance #7 (in-process): what a killed worker left in staging/ is never listed,
    is swept when the worker starts again, and the next sync completes the document."""
    new = "00000000-0000-4000-8000-000000000301"
    linked.cloud.put_documents({new: renamed(fixture_documents()[PDF], PDF, new)})
    half = linked.paths.staging / f"{new}.01J0000000000000000000000C"
    half.mkdir(parents=True)
    (half / f"{new}.metadata").write_bytes(b'{"visibleName": "half')
    incoming = linked.paths.staging / ".incoming"
    incoming.mkdir(exist_ok=True)
    (incoming / "0123abcd.part").write_bytes(b"half a blob")
    listed = {item["uuid"] for item in (await library.ok("list"))["items"]}
    assert listed == {NOTEBOOK, PDF}
    assert (await library.error("read_page", {"document": new, "page": 1}))["code"] == "not_found"
    await linked.sync()  # a new loop: the restarted worker sweeps staging, then polls
    assert not half.exists()
    assert not (incoming / "0123abcd.part").exists()
    listed = {item["uuid"] for item in (await library.ok("list"))["items"]}
    assert listed == {NOTEBOOK, PDF, new}
    assert not (await library.call("read_page", {"document": new, "page": 1})).is_error
