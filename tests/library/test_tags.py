"""Tags from ``.content`` (library/tags.py): bounds, normalisation, page ids, skip counts.

Tag names here are synthetic; the S3 fixture notebook supplies the page ids and shape.
"""

from __future__ import annotations

import json
import random
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from scriptorium.library.tags import (
    MAX_TAG_LEN,
    MAX_TAGS_PER_DOCUMENT,
    TagSet,
    normalise_tag,
    parse_tags,
)

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures/move-test-notebook/notebook/xochitl/00000000-0000-4000-8000-000000000001.content"
)
P = [f"00000000-0000-4000-8000-00000000010{n}" for n in range(1, 8)]  # the fixture's pages


def fixture_content(**extra: Any) -> dict[str, Any]:
    content = json.loads(FIXTURE.read_text())
    assert isinstance(content, dict)
    content.update(extra)
    return content


def test_the_fixture_has_no_tags() -> None:
    tags = parse_tags(fixture_content())
    assert tags == TagSet()
    assert tags.skipped == 0 and len(tags) == 0


def test_document_and_page_tags() -> None:
    tags = parse_tags(
        fixture_content(
            tags=[{"name": "alpha", "timestamp": 1}, {"name": "beta"}],
            pageTags=[
                {"name": "gamma", "pageId": P[0]},
                {"name": "alpha", "pageId": P[0]},
                {"name": "delta", "pageId": P[6]},
            ],
        )
    )
    assert tags.document == {"alpha", "beta"}
    assert dict(tags.pages) == {P[0]: {"gamma", "alpha"}, P[6]: {"delta"}}
    assert tags.for_page(P[0]) == {"alpha", "beta", "gamma"}
    assert tags.for_page(P[3]) == {"alpha", "beta"}
    assert tags.names() == {"alpha", "beta", "gamma", "delta"}
    assert len(tags) == 5 and tags.skipped == 0


def test_page_tags_on_pages_not_in_cpages_are_dropped_and_counted() -> None:
    content = fixture_content(
        pageTags=[
            {"name": "kept", "pageId": P[1]},
            {"name": "x", "pageId": "00000000-0000-4000-8000-000000000999"},
            {"name": "x", "pageId": "../../etc/passwd"},
            {"name": "x"},
            {"name": "x", "pageId": 7},
            "not an entry",
        ]
    )
    # A page marked deleted in cPages is not a page of the document either.
    content["cPages"]["pages"][2]["deleted"] = {"timestamp": "1:1", "value": 1}
    content["pageTags"].append({"name": "x", "pageId": P[2]})
    tags = parse_tags(content)
    assert dict(tags.pages) == {P[1]: {"kept"}}
    assert tags.skipped == 6


def test_format_one_pages_list_gives_the_page_ids() -> None:
    content = {"pages": [P[0], "nonsense"], "pageTags": [{"name": "a", "pageId": P[0]}]}
    assert dict(parse_tags(content).pages) == {P[0]: {"a"}}


def test_names_are_normalised_stripped_and_deduplicated() -> None:
    decomposed = "cafe\u0301"  # e + combining acute: NFC folds it into one character
    tags = parse_tags(
        {"tags": [{"name": f"  {decomposed} "}, {"name": "caf\u00e9"}, {"name": "\tplain\n"}]}
    )
    assert tags.document == {"caf\u00e9", "plain"}
    assert all(unicodedata.is_normalized("NFC", name) for name in tags.document)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "a\x00b",
        "a\x1bb",
        "a\x7fb",
        "line\u2028break",
        "para\u2029graph",
        "rtl\u202eoverride",
        "iso\u2066late",
        "\ud800",
        None,
        7,
        ["list"],
        {"name": "nested"},
    ],
)
def test_control_characters_and_non_strings_are_rejected(bad: object) -> None:
    assert normalise_tag(bad) is None
    tags = parse_tags({"tags": [{"name": bad}, {"name": "ok"}]})
    assert tags.document == {"ok"}
    assert tags.skipped == 1


def test_emoji_sequences_and_other_scripts_are_kept() -> None:
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # joined with ZWJ (category Cf)
    tags = parse_tags({"tags": [{"name": family}, {"name": "\u4e2d\u6587"}, {"name": "x-y_z"}]})
    assert tags.document == {family, "\u4e2d\u6587", "x-y_z"}


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("work\u200b", "work"),  # zero-width space
        ("\ufeffwork", "work"),  # byte order mark
        ("wo\u200erk\u200f", "work"),  # left-to-right and right-to-left marks
        ("\u061cwork", "work"),  # Arabic letter mark
        ("wo\u00adrk", "work"),  # soft hyphen
        ("wo\u2060rk", "work"),  # word joiner
        ("work\U000e0041\U000e007f", "work"),  # tag characters
        ("\u200b work \u200b", "work"),  # stripped again once the invisibles are gone
        ("e\u00ad\u0301", "\u00e9"),  # recomposed once the soft hyphen is gone
        ("\U0001f3f3\ufe0f\u200d\U0001f308", "\U0001f3f3\ufe0f\u200d\U0001f308"),  # VS16, ZWJ
        ("\u0645\u200c\u06cc", "\u0645\u200c\u06cc"),  # ZWNJ in Persian
    ],
)
def test_invisible_format_characters_are_removed(raw: str, stored: str) -> None:
    assert normalise_tag(raw) == stored
    assert parse_tags({"tags": [{"name": raw}]}).document == {stored}


@pytest.mark.parametrize("raw", ["\u200b", "\ufeff\u2060", " \u200b\u00ad ", "\U000e0041"])
def test_a_name_of_invisible_characters_only_is_rejected(raw: str) -> None:
    assert normalise_tag(raw) is None
    assert parse_tags({"tags": [{"name": raw}]}).skipped == 1


def test_invisible_variants_do_not_push_real_tags_out() -> None:
    """A hundred zero-width variants of one name are one tag, not a hundred."""
    phantoms = [{"name": "work" + "\u200b" * n} for n in range(100)]
    page = [{"name": "harbour", "pageId": P[0]}]
    tags = parse_tags({"tags": phantoms, "pageTags": page, "cPages": {"pages": [{"id": P[0]}]}})
    assert tags.document == {"work"}
    assert tags.pages == {P[0]: {"harbour"}}


def test_normalise_tag_is_idempotent() -> None:
    """Over the characters that interact: combining marks, Hangul jamo, joiners, invisibles,
    spaces, and names cut at the length bound mid-sequence."""
    parts = [
        "a", "e", "\u0301", "\u0327", "\u0323", "\u0308", "\u1100", "\u1161", "\u11a8",
        "\u0e01", "\u0e31", "\u200d", "\u200c", "\ufe0f", "\U0001f468", " ", "\u00a0",
        "\u3000", "\u0f71", "\u0f72", "\u0344", "\u1e0a", "\u05d0", "\u05b8", "\u05bc",
        "\u0915", "\u093c", "\U0001d157", "\U0001d165", "\u212b", "\u2126", "\u0130",
        "\u200b", "\u00ad", "\ufeff", "\u2060", "\u200e", "\U000e0041",
    ]  # fmt: skip
    rng = random.Random(7)
    samples = ["x" * 63 + "a\u0301\u0327", "x" * 63 + "\u00ad\u0301", "x" * 62 + "\u200b" * 5]
    samples += ["".join(rng.choices(parts, k=rng.randint(1, 80))) for _ in range(5000)]
    for raw in samples:
        once = normalise_tag(raw)
        if once is not None:
            assert normalise_tag(once) == once, ascii(raw)
            assert unicodedata.normalize("NFC", once) == once


def test_long_names_are_truncated_not_dropped() -> None:
    long = "a" * 63 + " " + "b" * 100
    tags = parse_tags({"tags": [{"name": "c" * 500}, {"name": long}]})
    assert tags.document == {"c" * MAX_TAG_LEN, "a" * 63}  # the cut is stripped again
    assert tags.skipped == 0


def test_at_most_a_hundred_tags_document_tags_first() -> None:
    doc_tags = [{"name": f"doc-{n:03d}"} for n in range(70)]
    page_tags = [{"name": f"page-{n:03d}", "pageId": P[n % 7]} for n in range(70)]
    tags = parse_tags(fixture_content(tags=doc_tags, pageTags=page_tags))
    assert len(tags) == MAX_TAGS_PER_DOCUMENT
    assert len(tags.document) == 70
    assert sum(len(names) for names in tags.pages.values()) == 30
    assert tags.skipped == 40
    # More document tags than the bound: the first hundred by name.
    many = parse_tags({"tags": [{"name": f"t{n:04d}"} for n in range(250, 0, -1)]})
    assert sorted(many.document) == [f"t{n:04d}" for n in range(1, 101)]
    assert many.pages == {} and many.skipped == 150


def test_shapes_that_are_not_lists_give_no_tags() -> None:
    for content in ({"tags": "alpha"}, {"tags": {"name": "a"}}, {"pageTags": None}, {}):
        assert parse_tags(content) == TagSet()


def test_tag_sets_compare_by_value_not_by_skips() -> None:
    assert TagSet(frozenset({"a"}), skipped=3) == TagSet(frozenset({"a"}))
