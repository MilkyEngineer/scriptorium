"""Tool tests run under the cloud tests' guards, against a library the real worker synced."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from scriptorium.auth.oidc import OIDCValidator
from tests.cloud.conftest import _never_delete_archive, _no_network
from tests.conftest import FakeIssuer
from tests.tools.helpers import Served, serve
from tests.worker.helpers import Linked, link

__all__ = ["_never_delete_archive", "_no_network"]


@pytest.fixture
async def linked(tmp_path: Path) -> AsyncIterator[Linked]:
    """The fixture notebook and PDF, synced and rendered by one worker poll."""
    account = link(tmp_path)
    try:
        await account.sync()
        yield account
    finally:
        account.close()


@pytest.fixture
async def library(
    linked: Linked, issuer: FakeIssuer, make_validator: Callable[..., OIDCValidator]
) -> AsyncIterator[Served]:
    async with serve(linked.settings, issuer, make_validator, clock=linked.clock) as served:
        yield served
