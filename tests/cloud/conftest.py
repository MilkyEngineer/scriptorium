"""Cloud tests: no network, and nothing under an archive's protected trees is removed."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

_PROTECTED = ("blobs", "library", "tombstones")


def _guard(real: Callable[..., Any], name: str) -> Callable[..., Any]:
    """Refuse a delete by path under a protected tree.

    Calls relative to a directory descriptor are shutil.rmtree's own inner steps (pytest
    and ``TemporaryDirectory`` cleaning up); the top-level path of those is still checked.
    """

    def wrapper(path: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("dir_fd") is None and any(
            part in _PROTECTED for part in Path(os.fsdecode(path)).parts
        ):
            raise AssertionError(f"{name} under a protected directory")
        return real(path, *args, **kwargs)

    return wrapper


@pytest.fixture(autouse=True)
def _never_delete_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    # os.rmdir is not guarded: it only ever removes an empty directory, which the real
    # archive does to tidy tombstones/<uuid>/ after an un-tombstone.
    for name in ("unlink", "remove"):
        monkeypatch.setattr(os, name, _guard(getattr(os, name), f"os.{name}"))
    monkeypatch.setattr(shutil, "rmtree", _guard(shutil.rmtree, "shutil.rmtree"))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a cloud test tried to use the network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
