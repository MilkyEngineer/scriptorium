"""``cloud/scope.py``: the scope value, its stored form and the fail-closed in/out decision."""

from __future__ import annotations

import json

import pytest

from scriptorium.cloud.scope import (
    ALL,
    MAX_ANCESTOR_DEPTH,
    MAX_TAG_LEN,
    MAX_TAG_SCAN,
    MAX_TAGS_PER_DOCUMENT,
    NOT_A_FOLDER,
    Known,
    ScopeError,
    SyncScope,
    ancestor_tags,
    decide,
    dump_scope,
    load_scope,
    make_scope,
    normalise_tag,
    scope_fingerprint,
    scope_tags,
)
from scriptorium.library import tags as index_tags

E = frozenset[str]()


class Settings:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def get_setting(self, key: str) -> str | None:
        assert key == "sync_scope"
        return self.value


def test_normalise_tag_is_the_tag_index_normalisation() -> None:
    assert normalise_tag is index_tags.normalise_tag
    assert normalise_tag("  Work ") == "Work"
    assert normalise_tag("Cafe\u0301") == "Caf\u00e9"  # NFC
    assert normalise_tag("x" * 70) == "x" * MAX_TAG_LEN
    assert normalise_tag("a" * 63 + " b") == "a" * 63
    for bad in ("", "   ", "a\nb", "a\x00", "\u202eabc", "\ud800", "a\u2028b", 3, None):
        assert normalise_tag(bad) is None


def test_scope_tags_reads_the_whole_list_or_nothing() -> None:
    content = {
        "tags": [{"name": " Work "}, {"name": "work"}, {"name": "  "}, {"name": "a\x00"}],
        "pageTags": [{"name": "Private", "pageId": "p"}],
    }
    # Names that normalise to nothing can equal no scope tag; page tags do not count.
    assert scope_tags(content) == {"Work", "work"}
    assert scope_tags({}) == E  # no tags key: no tags
    assert scope_tags({"tags": []}) == E
    many = {"tags": [{"name": f"t{i:03}"} for i in range(150)]}
    tags = scope_tags(many)
    assert tags is not None and len(tags) == 150 > MAX_TAGS_PER_DOCUMENT  # no display cut
    for unknown in (
        None,  # listed but unreadable
        {"tags": "Work"},
        {"tags": None},
        {"tags": [{"name": "ok"}, "bare"]},
        {"tags": [{"name": "ok"}, {"no": 1}]},
        {"tags": [{"name": 3}]},
        {"tags": [{"name": "x"}] * (MAX_TAG_SCAN + 1)},
    ):
        assert scope_tags(unknown) is None


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


def test_decide() -> None:
    allow = make_scope("allow", ["work"])
    deny = make_scope("deny", ["private"])
    work, private = frozenset({"work"}), frozenset({"private"})
    assert decide(ALL, None, E, complete=False) is True
    assert decide(allow, work, E) is True
    assert decide(allow, E, work) is True  # a folder's tag reaches its children
    assert decide(allow, frozenset({"Work"}), E) is False  # exact match after normalisation
    assert decide(allow, E, E) is False
    assert decide(deny, work, E) is True
    assert decide(deny, private, E) is False
    assert decide(deny, E, private | work) is False
    # Unknown own tags or an unknown folder above: only a known match settles it.
    for scope in (allow, deny):
        assert decide(scope, None, E) is None
        assert decide(scope, E, E, complete=False) is None
    assert decide(allow, None, work, complete=False) is True
    assert decide(allow, work, E, complete=False) is True
    assert decide(deny, None, private) is False
    assert decide(deny, private, E, complete=False) is False


def test_ancestor_tags_walks_up_and_reports_unknowns() -> None:
    nodes = {
        "c": Known(frozenset({"mid"}), "b"),
        "b": Known(frozenset({"top"}), None),
        "t": Known(frozenset({"in-trash"}), "trash"),
        "x": Known(frozenset({"cx"}), "y"),
        "y": Known(frozenset({"cy"}), "x"),
        "u": Known(None, "b"),  # tags unknown, parent known
        "m": Known(frozenset({"m"}), None, parent_known=False),
        "o": Known(E, "gone"),
    }

    def lookup(uuid: str) -> Known | None:
        if uuid == "gone":
            return NOT_A_FOLDER
        return nodes.get(uuid)

    assert ancestor_tags("c", lookup) == ({"mid", "top"}, True)
    assert ancestor_tags(None, lookup) == (E, True)
    assert ancestor_tags("trash", lookup) == (E, True)  # the trash is not a folder
    assert ancestor_tags("t", lookup) == ({"in-trash"}, True)
    assert ancestor_tags("x", lookup) == ({"cx", "cy"}, True)  # a cycle ends
    assert ancestor_tags("o", lookup) == (E, True)
    assert ancestor_tags("u", lookup) == ({"top"}, False)  # keeps walking for a known match
    assert ancestor_tags("m", lookup) == ({"m"}, False)
    assert ancestor_tags("nobody", lookup) == (E, False)

    chain = {str(i): Known(E, str(i + 1)) for i in range(1000)}
    looked: list[str] = []

    def walk(uuid: str) -> Known | None:
        looked.append(uuid)
        return chain[uuid]

    assert ancestor_tags("0", walk) == (E, False)  # too deep: incomplete
    assert len(looked) == MAX_ANCESTOR_DEPTH
