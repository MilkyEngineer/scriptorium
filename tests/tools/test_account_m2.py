"""The M2 account tools: set_sync_scope and disconnect_remarkable(keep_archive=false)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from scriptorium.auth.oidc import OIDCValidator
from scriptorium.cloud.scope import SETTING_SYNC_SCOPE
from tests.cloud.fake_cloud import NOTEBOOK, PDF
from tests.conftest import FakeIssuer, make_settings
from tests.tools.helpers import Served, serve
from tests.worker.helpers import Linked

WORK = "Synthetic Work"
"""A tag the tests put on the fixture notebook (no real tag names)."""
PERSONAL = "Synthetic Personal"


@pytest.fixture
async def fresh(
    tmp_path: Path, issuer: FakeIssuer, make_validator: Callable[..., OIDCValidator]
) -> AsyncIterator[Served]:
    data = tmp_path / "data"
    data.mkdir()
    async with serve(make_settings(data_dir=data), issuer, make_validator) as served:
        yield served


def _tag(linked: Linked, uuid: str, *tags: str) -> None:
    files = linked.cloud.docs[uuid]
    content = json.loads(files[f"{uuid}.content"])
    content["tags"] = [{"name": tag, "timestamp": 1} for tag in tags]
    linked.cloud.set_file(uuid, f"{uuid}.content", json.dumps(content).encode())


def _excluded(linked: Linked, uuid: str) -> bool:
    row = linked.index().document(uuid)
    assert row is not None
    return row.excluded_at is not None


# ---- set_sync_scope ------------------------------------------------------------------------------


async def test_set_sync_scope_before_linking_is_not_linked(fresh: Served) -> None:
    assert (await fresh.error("set_sync_scope", {"mode": "all"}))["code"] == "not_linked"


@pytest.mark.parametrize(
    "args",
    [
        {"mode": "all", "tags": [WORK]},
        {"mode": "allow"},
        {"mode": "deny", "tags": []},
        {"mode": "allow", "tags": ["   "]},
        {"mode": "allow", "tags": ["bell\x07"]},
        {"mode": "allow", "tags": [""]},
        {"mode": "everything"},
        {"mode": "allow", "tags": [f"t{n}" for n in range(101)]},
        {"mode": "allow", "tags": ["x" * 257]},
        {"mode": "allow", "tags": [WORK], "user": "0123456789abcdef"},
    ],
)
async def test_set_sync_scope_refuses_a_bad_scope(fresh: Served, args: dict[str, Any]) -> None:
    # Refused before any account is looked at (nobody is linked here).
    error = await fresh.error("set_sync_scope", args)
    assert error["code"] == "invalid_argument"
    assert "bell" not in json.dumps(error)


async def test_set_sync_scope_stores_the_scope_and_the_next_poll_applies_it(
    library: Served, linked: Linked, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _tag(linked, NOTEBOOK, WORK)
    await linked.sync()  # the tag is indexed

    allowed = await library.ok("set_sync_scope", {"mode": "allow", "tags": [f"  {WORK} "]})

    assert allowed["account"] == linked.acct
    assert allowed["scope"] == {"mode": "allow", "tags": [WORK]}
    assert allowed["previous"] == {"mode": "all", "tags": []}
    assert allowed["changed"] is True
    assert allowed["unmatched_tags"] == []
    assert "Nothing archived here is deleted" in allowed["message"]
    stored = linked.index().get_setting(SETTING_SYNC_SCOPE)
    assert stored is not None and json.loads(stored) == {"mode": "allow", "tags": [WORK]}
    before = sorted(p for p in linked.paths.root.rglob("*") if p.is_file())

    await linked.sync()

    assert not _excluded(linked, NOTEBOOK) and _excluded(linked, PDF)
    after = {p for p in linked.paths.root.rglob("*") if p.is_file()}
    assert set(before) - after <= {p for p in before if p.name.endswith(("-wal", "-shm"))}

    again = await library.ok("set_sync_scope", {"mode": "allow", "tags": [WORK]})
    assert again["changed"] is False

    denied = await library.ok("set_sync_scope", {"mode": "deny", "tags": [WORK, PERSONAL]})
    assert denied["scope"] == {"mode": "deny", "tags": sorted([WORK, PERSONAL])}
    assert denied["unmatched_tags"] == [PERSONAL]
    assert "spelling" in denied["message"]
    await linked.sync()
    assert _excluded(linked, NOTEBOOK) and not _excluded(linked, PDF)

    everything = await library.ok("set_sync_scope", {"mode": "all"})
    assert everything["scope"] == {"mode": "all", "tags": []}
    assert linked.index().get_setting(SETTING_SYNC_SCOPE) is None  # the default: no row
    await linked.sync()
    assert not _excluded(linked, NOTEBOOK) and not _excluded(linked, PDF)

    # Tag names are the user's: returned to them, never logged or audited. (The test's own
    # MCP client logs what it sends; only the server side counts.)
    server_side = "\n".join(
        f"{r.getMessage()} {getattr(r, 'scriptorium_fields', '')}"
        for r in caplog.records
        if not r.name.startswith("mcp.client")
    )
    assert WORK not in server_side and PERSONAL not in server_side
    audit = [row for row in library.audit_rows() if "set_sync_scope" in repr(row)]
    assert audit and all(WORK not in repr(row) and PERSONAL not in repr(row) for row in audit)
    assert any(
        '"count":2' in repr(row).replace(" ", "") and linked.acct in repr(row) for row in audit
    )


async def test_a_damaged_stored_scope_is_replaced(library: Served, linked: Linked) -> None:
    linked.index().set_setting(SETTING_SYNC_SCOPE, "{not json")
    result = await library.ok("set_sync_scope", {"mode": "all"})
    assert result["previous"] is None and result["changed"] is True
    assert linked.index().get_setting(SETTING_SYNC_SCOPE) is None
