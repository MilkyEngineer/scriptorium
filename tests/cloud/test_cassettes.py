"""The cassette harness: committed cassettes are clean, reproducible, and the recorder refuses."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.cloud import build_cassettes
from tests.cloud.cassettes import (
    CASSETTE_DIR,
    REPO_ROOT,
    SCRUBBED,
    CassetteRefusedError,
    Player,
    Recorder,
    check_cassette,
    load_cassette,
)
from tests.cloud.fake_cloud import DEVICE_TOKEN, NOTEBOOK, FakeCloud, fixture_documents
from tests.cloud.scenarios import SCENARIOS, both, harness

COMMITTED = sorted(CASSETTE_DIR.glob("*.json"))
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"


def test_every_scenario_has_a_committed_cassette() -> None:
    assert {p.stem for p in COMMITTED} == set(SCENARIOS)


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: p.stem)
def test_committed_cassettes_are_synthetic_and_clean(path: Path) -> None:
    cassette = load_cassette(path)  # runs every check
    assert cassette["synthetic"] is True
    assert cassette["description"].startswith("Synthetic.")
    text = path.read_text()
    assert DEVICE_TOKEN not in text
    assert "remarkable" not in text.lower()


def test_the_readme_says_they_are_synthetic() -> None:
    readme = (CASSETTE_DIR / "README.md").read_text()
    assert "synthetic" in readme.lower()
    assert "build_cassettes" in readme


def test_committed_cassettes_are_exactly_what_the_builder_makes(tmp_path: Path) -> None:
    build_cassettes.main([str(tmp_path)])
    for path in COMMITTED:
        assert (tmp_path / path.name).read_bytes() == path.read_bytes(), path.name


# ---- the recorder refuses ----------------------------------------------------------------


def _record(cloud: FakeCloud, tmp_path: Path) -> Recorder:
    recorder = Recorder.synthetic(cloud.handler)
    h = harness(recorder, tmp_path)
    h.poll()
    return recorder


def test_the_recorder_refuses_a_blob_that_is_not_a_fixture_file(tmp_path: Path) -> None:
    cloud = FakeCloud()
    docs = fixture_documents()
    docs[NOTEBOOK][f"{NOTEBOOK}.pagedata"] = b"a real note would be here\n"
    cloud.put_documents(docs)
    recorder = _record(cloud, tmp_path)

    with pytest.raises(CassetteRefusedError, match="not a fixture file"):
        recorder.save(tmp_path / "x.json", description="x")
    assert not (tmp_path / "x.json").exists()


def test_the_recorder_refuses_a_uuid_outside_the_fixture(tmp_path: Path) -> None:
    cloud = FakeCloud()
    stranger = "12345678-1234-4234-8234-123456789abc"
    docs = fixture_documents()
    cloud.put_documents(
        {
            stranger: {
                name.replace(NOTEBOOK, stranger): data for name, data in docs[NOTEBOOK].items()
            }
        }
    )
    recorder = _record(cloud, tmp_path)

    with pytest.raises(CassetteRefusedError, match="UUID"):
        recorder.save(tmp_path / "x.json", description="x")


def test_the_recorder_scrubs_tokens_and_hosts(tmp_path: Path) -> None:
    cloud = FakeCloud()
    both(cloud)
    cassette = _record(cloud, tmp_path).cassette(description="Synthetic. x")
    text = json.dumps(cassette)

    assert DEVICE_TOKEN not in text and cloud.user_token not in text
    assert "remarkable" not in text
    for interaction in cassette["interactions"]:
        assert interaction["request"]["headers"]["authorization"] == SCRUBBED


def test_the_recorder_will_not_wrap_the_network() -> None:
    with pytest.raises(CassetteRefusedError):
        Recorder(httpx.HTTPTransport())


def test_live_recording_needs_the_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCRIPTORIUM_LIVE_TEST", raising=False)
    token = tmp_path / "token"
    token.write_text("x")
    with pytest.raises(CassetteRefusedError, match="SCRIPTORIUM_LIVE_TEST"):
        Recorder.live(token)


def test_live_recording_refuses_a_token_inside_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCRIPTORIUM_LIVE_TEST", "1")
    with pytest.raises(CassetteRefusedError, match="outside the repository"):
        Recorder.live(REPO_ROOT / "pyproject.toml")


# ---- the checker refuses -----------------------------------------------------------------


def _base() -> dict[str, Any]:
    return load_cassette(CASSETTE_DIR / "full_listing.json")


def _mutate(path: list[Any], value: Any) -> dict[str, Any]:
    cassette = copy.deepcopy(_base())
    target: Any = cassette
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return cassette


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["interactions", 0, "request", "headers", "authorization"], "Bearer abc.def.ghi"),
        (["interactions", 0, "request", "headers", "cookie"], "session=1"),
        (["interactions", 0, "request", "url"], "https://internal.example.net/x"),
        (["interactions", 0, "response", "body"], "a-real-user-token"),
        (["description"], f"Synthetic. {_JWT}"),
        (["interactions", 2, "response", "body"], "3\nnot an index\n"),
        (["interactions", 5, "response", "fixture"], "../../../pyproject.toml"),
        (["interactions", 5, "response", "headers", "set-cookie"], "a=b"),
        (["interactions", 3, "request", "headers", "rm-filename"], "../../etc/passwd"),
        (["description"], "token: Bearer abcdefghijkl"),
        (["description"], "about 12345678-1234-4234-8234-123456789abc"),
        (["synthetic"], "yes"),
    ],
)
def test_the_checker_refuses(path: list[Any], value: Any) -> None:
    with pytest.raises(CassetteRefusedError):
        check_cassette(_mutate(path, value))


# ---- the player --------------------------------------------------------------------------


def test_the_player_refuses_an_unrecorded_request(tmp_path: Path) -> None:
    player = Player.named("full_listing")
    client = harness(player, tmp_path).client
    with pytest.raises(AssertionError, match="no recorded answer"):
        client.document_index(NOTEBOOK, "a" * 64)
