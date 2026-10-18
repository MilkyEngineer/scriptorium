"""``cloud/scope.py``: the scope value, its stored form and the in/out decision."""

from __future__ import annotations

import json

import pytest

from scriptorium.cloud.scope import (
    ALL,
    MAX_ANCESTOR_DEPTH,
    MAX_TAG_LEN,
    MAX_TAGS_PER_DOCUMENT,
    ScopeError,
    SyncScope,
    ancestor_tags,
    document_tags,
    dump_scope,
    in_scope,
    load_scope,
    make_scope,
    normalise_tag,
    scope_fingerprint,
)

E = frozenset[str]()


class Settings:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def get_setting(self, key: str) -> str | None:
        assert key == "sync_scope"
        return self.value


def test_normalise_tag() -> None:
    assert normalise_tag("  Work ") == "Work"
    assert normalise_tag("Café") == "Café"  # NFC
    assert normalise_tag("x" * 70) == "x" * MAX_TAG_LEN
    assert normalise_tag("a" * 63 + " b") == "a" * 63
    for bad in ("", "   ", "a\nb", "a\x00", "‮abc", "\ud800", 3, None):
        assert normalise_tag(bad) is None


def test_document_tags_reads_document_tags_only() -> None:
    content = {
        "tags": [{"name": " Work "}, {"name": "work"}, {"name": ""}, "bare", {"no": 1}],
        "pageTags": [{"name": "Private", "pageId": "p"}],
    }
    assert document_tags(content) == {"Work", "work"}
    assert document_tags({}) == E
    assert document_tags({"tags": "Work"}) == E
    many = {"tags": [{"name": f"t{i:03}"} for i in range(150)]}
    kept = document_tags(many)
    assert len(kept) == MAX_TAGS_PER_DOCUMENT and min(kept) == "t000"


def test_scope_value_is_checked() -> None:
    assert make_scope("all", []) == ALL
    assert make_scope("deny", [" b", "a", "a"]) == SyncScope("deny", frozenset({"a", "b"}))
    for mode, tags in (
        ("all", ["a"]),
        ("allow", []),
        ("deny", ["ok", "\n"]),
        ("some", ["a"]),
        ("allow", [f"t{i}" for i in range(101)]),
    ):
        with pytest.raises(ScopeError):
            make_scope(mode, tags)
    with pytest.raises(ScopeError):
        SyncScope("allow", frozenset({" padded"}))


def test_stored_scope_round_trips_and_malformed_is_an_error() -> None:
    assert load_scope(Settings(None)) == ALL
    scope = make_scope("allow", ["Été", "b"])
    stored = dump_scope(scope)
    assert json.loads(stored) == {"mode": "allow", "tags": ["b", "Été"]}
    assert load_scope(Settings(stored)) == scope
    assert scope_fingerprint(scope) != scope_fingerprint(ALL)
    assert scope_fingerprint(make_scope("allow", ["b", "Été"])) == scope_fingerprint(scope)
    for bad in (
        "nope",
        "[]",
        '{"mode": "allow"}',
        '{"mode": "allow", "tags": "a"}',
        '{"mode": "allow", "tags": [], "x": 1}',
        '{"mode": 1, "tags": []}',
        '{"mode": "deny", "tags": []}',
        "[" * 100_000,
    ):
        with pytest.raises(ScopeError):
            load_scope(Settings(bad))


def test_in_scope() -> None:
    allow = make_scope("allow", ["work"])
    deny = make_scope("deny", ["private"])
    assert in_scope(ALL, E, E)
    assert in_scope(allow, frozenset({"work"}), E)
    assert in_scope(allow, E, frozenset({"work"}))  # a folder's tag reaches its children
    assert not in_scope(allow, frozenset({"Work"}), E)  # exact match after normalisation
    assert not in_scope(allow, E, E)
    assert in_scope(deny, frozenset({"work"}), E)
    assert not in_scope(deny, frozenset({"private"}), E)
    assert not in_scope(deny, E, frozenset({"private", "x"}))


def test_ancestor_tags_walks_up_and_stops() -> None:
    parents = {"c": "b", "b": "a", "a": None, "t": "trash", "x": "y", "y": "x"}
    tags = {"a": frozenset({"top"}), "b": frozenset({"mid"}), "trash": frozenset({"no"})}

    def walk(start: str | None) -> frozenset[str]:
        return ancestor_tags(start, parents.__getitem__, lambda u: tags.get(u, E))

    assert walk("c") == {"top", "mid"}
    assert walk(None) == E
    assert walk("t") == E  # the trash is not a folder
    assert walk("x") == E  # a cycle ends
    chain = {str(i): str(i + 1) for i in range(1000)}
    seen: list[str] = []

    def tags_of(u: str) -> frozenset[str]:
        seen.append(u)
        return E

    ancestor_tags("0", chain.__getitem__, tags_of)
    assert len(seen) == MAX_ANCESTOR_DEPTH
