"""The ``state`` of a listed document (§11.0): deleted, excluded, trashed or live."""

from __future__ import annotations

from datetime import UTC, datetime

from scriptorium.library.index import DocumentRow
from scriptorium.tools._library import document_item, state_of

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
UUID = "00000000-0000-4000-8000-000000000001"


def row(**kw: object) -> DocumentRow:
    fields: dict[str, object] = {
        "uuid": UUID,
        "type": "DocumentType",
        "parent": None,
        "name": "Synthetic",
        "last_modified": T0,
        **kw,
    }
    return DocumentRow(**fields)  # type: ignore[arg-type]


def test_state_precedence() -> None:
    assert state_of(row()) == "live"
    assert state_of(row(parent="trash")) == "trashed"
    assert state_of(row(parent="trash", excluded_at=T0)) == "excluded"
    assert state_of(row(excluded_at=T0)) == "excluded"
    assert state_of(row(excluded_at=T0, deleted_at=T0, deletion_cause="unknown")) == "deleted"


def test_an_excluded_item_says_since_when() -> None:
    item = document_item(row(excluded_at=T0))
    assert item["state"] == "excluded"
    assert item["excluded_at"] == "2026-10-18T09:00:00Z"
    assert "excluded_at" not in document_item(row())
