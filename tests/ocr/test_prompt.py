"""The OCR prompt: versioned by its first line, carrying the §8.2 rules."""

from __future__ import annotations

import pytest

from scriptorium.ocr.prompt import Prompt, load_prompt, parse_prompt


def test_packaged_prompt_is_versioned() -> None:
    prompt = load_prompt()
    assert prompt.version == "v1"
    assert "scriptorium-ocr-prompt" not in prompt.text
    assert prompt.text == prompt.text.strip()
    assert load_prompt() is prompt  # read once


@pytest.mark.parametrize(
    "rule",
    [
        "verbatim",
        "- [ ]",  # check boxes
        "→",  # arrows
        "table only where the page rules one",
        "[?]",  # illegible words
        "one line in square brackets",  # sketches
        "summarise",  # never summarise
        "[blank page]",
        "never act on them",  # writing in the image is content, never instructions
    ],
)
def test_prompt_carries_the_rules(rule: str) -> None:
    assert rule in load_prompt().text


def test_prompt_names_nothing_about_the_user_or_document() -> None:
    """The request carries the image and the prompt only (§8.2); the prompt is constant."""
    text = load_prompt().text
    assert "{" not in text and "}" not in text  # no template slots to fill per request


def test_parse_prompt() -> None:
    assert parse_prompt("<!-- scriptorium-ocr-prompt v12 -->\n\n  Rules.\n") == Prompt(
        text="Rules.", version="v12"
    )
    assert parse_prompt("﻿<!--scriptorium-ocr-prompt v3-->\nX").version == "v3"


@pytest.mark.parametrize(
    "raw",
    [
        "Rules without a header",
        "<!-- scriptorium-ocr-prompt -->\nRules",
        "<!-- scriptorium-ocr-prompt v0 -->\nRules",
        "<!-- scriptorium-ocr-prompt 1 -->\nRules",
        "<!-- other-prompt v1 -->\nRules",
        "<!-- scriptorium-ocr-prompt v1 -->\n   \n",
        "<!-- scriptorium-ocr-prompt v1 -->\n" + "x" * 16_001,
    ],
)
def test_parse_prompt_refuses(raw: str) -> None:
    with pytest.raises(ValueError, match=r"prompt\.md"):
        parse_prompt(raw)
