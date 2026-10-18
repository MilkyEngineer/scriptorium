"""Render job kinds: keys name the version of the work; payloads hold checked identifiers."""

from __future__ import annotations

from typing import Any

import pytest

from scriptorium.errors import InvalidArgumentError
from scriptorium.jobs.kinds import (
    PAGE_IMAGE,
    RENDER,
    RenderSpec,
    parse_render_payload,
    render_key,
    rerun_key,
)

DOC = "00000000-0000-4000-8000-000000000001"
PAGE = "00000000-0000-4000-8000-000000000101"
HASH = "ab" * 32
ACCT = "01J0000000000000000000000A"


def test_keys_and_payloads_round_trip() -> None:
    archive = RenderSpec(DOC, PAGE, HASH, 3)
    assert archive.kind == RENDER
    assert archive.key(ACCT) == render_key(ACCT, PAGE, HASH) == f"render:{ACCT}:{PAGE}:{HASH}"
    assert parse_render_payload(RENDER, archive.payload()) == archive
    sized = RenderSpec(DOC, PAGE, HASH, 3, max_side=2048)
    assert sized.kind == PAGE_IMAGE
    assert sized.key(ACCT) == f"page_image:{ACCT}:{PAGE}:{HASH}:2048"
    assert parse_render_payload(PAGE_IMAGE, sized.payload()) == sized


def test_a_rerun_key_never_nests() -> None:
    key = render_key(ACCT, PAGE, HASH)
    once = rerun_key(key, 7)
    assert once == f"{key}:after:7"
    assert rerun_key(once, 9) == f"{key}:after:9"


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("sync", {"document": DOC, "page": PAGE, "hash": HASH, "generation": 1}),
        (RENDER, {"document": DOC, "page": PAGE, "hash": HASH}),
        (RENDER, {"document": DOC, "page": PAGE, "hash": HASH, "generation": 1, "x": 1}),
        (RENDER, {"document": DOC, "page": PAGE, "hash": HASH, "generation": 1, "max_side": 9}),
        (PAGE_IMAGE, {"document": DOC, "page": PAGE, "hash": HASH, "generation": 1}),
        (RENDER, {"document": "../etc", "page": PAGE, "hash": HASH, "generation": 1}),
        (RENDER, {"document": DOC, "page": f"{PAGE}/..", "hash": HASH, "generation": 1}),
        (RENDER, {"document": DOC, "page": PAGE, "hash": "AB" * 32, "generation": 1}),
        (RENDER, {"document": DOC, "page": PAGE, "hash": HASH, "generation": True}),
        (RENDER, {"document": DOC, "page": PAGE, "hash": HASH, "generation": -1}),
        (
            PAGE_IMAGE,
            {"document": DOC, "page": PAGE, "hash": HASH, "generation": 1, "max_side": 10**6},
        ),
    ],
)
def test_malformed_payloads_are_refused(kind: str, payload: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentError):
        parse_render_payload(kind, payload)
