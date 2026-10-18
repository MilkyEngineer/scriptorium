"""The transcription prompt (Architecture §8.2), versioned by the first line of ``prompt.md``.

``prompt.md`` starts with ``<!-- scriptorium-ocr-prompt v<N> -->``; the rest of the file is
the system prompt sent with every page. The version is stored with each transcript, so a
change to the rules that should re-transcribe pages bumps ``N`` in the same commit.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from importlib import resources
from typing import Final

__all__ = ["PROMPT_FILE", "Prompt", "load_prompt", "parse_prompt"]

PROMPT_FILE: Final = "prompt.md"
_HEADER_RE: Final = re.compile(r"<!--\s*scriptorium-ocr-prompt\s+(v[1-9][0-9]{0,5})\s*-->")
_MAX_PROMPT_CHARS: Final = 16_000


@dataclass(frozen=True)
class Prompt:
    """The system prompt and its version (``"v<N>"``), stored with every transcript."""

    text: str
    version: str


def parse_prompt(raw: str) -> Prompt:
    """Split ``prompt.md``'s content into its version line and its text.

    Raises ValueError when the header is missing or malformed, or the text is empty: a
    prompt without a version would store transcripts that cannot be told apart later.
    """
    first, _, rest = raw.lstrip("﻿").partition("\n")
    match = _HEADER_RE.fullmatch(first.strip())
    if match is None:
        raise ValueError(f"{PROMPT_FILE} must start with <!-- scriptorium-ocr-prompt v<N> -->")
    text = rest.strip()
    if not text:
        raise ValueError(f"{PROMPT_FILE} has no prompt text after its version line")
    if len(text) > _MAX_PROMPT_CHARS:
        raise ValueError(f"{PROMPT_FILE} is longer than {_MAX_PROMPT_CHARS} characters")
    return Prompt(text=text, version=match.group(1))


@functools.cache
def load_prompt() -> Prompt:
    """The packaged prompt, read once per process."""
    raw = resources.files("scriptorium.ocr").joinpath(PROMPT_FILE).read_text(encoding="utf-8")
    return parse_prompt(raw)
