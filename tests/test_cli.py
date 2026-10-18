"""The ``serve`` and ``worker`` entrypoints and the worker healthcheck (scriptorium/cli.py)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from scriptorium import cli, config
from scriptorium import logging as slog
from scriptorium.users import fs
from scriptorium.users.store import global_db_path, worker_heartbeat_path
from scriptorium.worker import HEARTBEAT_STALE_SECONDS
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def _keep_test_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """``serve`` and ``worker`` reconfigure the root logger; pytest's capture must survive."""
    monkeypatch.setattr(slog, "configure_logging", lambda level="info": None)


def _data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    return data


def test_worker_healthcheck_reads_the_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _data(tmp_path)
    monkeypatch.setenv("SCRIPTORIUM_DATA_DIR", str(data))
    assert cli.main(["healthcheck", "--worker"]) == 1  # no heartbeat yet
    beat = worker_heartbeat_path(data)
    beat.write_text("")
    assert cli.main(["healthcheck", "--worker"]) == 0
    old = time.time() - HEARTBEAT_STALE_SECONDS - 5
    os.utime(beat, (old, old))
    assert cli.main(["healthcheck", "--worker"]) == 1


def test_serve_and_worker_refuse_an_unsafe_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(data_dir=_data(tmp_path))
    monkeypatch.setattr(config, "load_settings", lambda: settings)

    def refuse(path: Path) -> None:
        report = fs.FilesystemReport(
            path=path,
            fstype="nfs4",
            mount_point=path,
            is_local=False,
            writable=True,
            wal_ok=True,
            locks_ok=True,
            problems=["a network filesystem"],
        )
        raise fs.UnsafeDataDir(report)

    monkeypatch.setattr(fs, "require_local_data_dir", refuse)
    assert cli.main(["worker"]) == 2
    assert cli.main(["serve"]) == 2
    assert "network filesystem" in capsys.readouterr().err
    assert not global_db_path(settings.data_dir).exists()  # nothing was opened


def test_serve_migrates_before_the_first_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    settings = make_settings(data_dir=_data(tmp_path))
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    seen: dict[str, Any] = {}

    def run(app: Any, **kwargs: Any) -> None:
        seen["app"] = app
        seen["db"] = global_db_path(settings.data_dir).is_file()
        seen.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", run)
    assert cli.main(["serve"]) == 0
    assert seen["db"] is True  # migrated before uvicorn started
    assert seen["access_log"] is False
    assert seen["proxy_headers"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="no asyncio signal handlers on Windows")
async def test_the_worker_runs_until_sigterm(tmp_path: Path) -> None:
    from scriptorium.tools._services import Services

    settings = make_settings(data_dir=_data(tmp_path))
    Services.open(settings).close()  # the server's migrations
    task = asyncio.create_task(cli._run_worker(settings))
    beat = worker_heartbeat_path(settings.data_dir)
    for _ in range(200):
        if beat.exists() or task.done():
            break
        await asyncio.sleep(0.05)
    assert beat.exists(), "the worker never wrote its heartbeat"
    os.kill(os.getpid(), signal.SIGTERM)  # handled by the worker's loop, not the default
    assert await asyncio.wait_for(task, timeout=30) == 0
