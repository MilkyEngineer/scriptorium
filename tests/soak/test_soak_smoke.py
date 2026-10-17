from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("sqlite_soak.py")


def test_both_roles_for_five_seconds_have_no_busy_errors(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    completed = subprocess.run(  # noqa: S603 - fixed argv, our own script
        [
            sys.executable,
            str(SCRIPT),
            "--data",
            str(tmp_path / "soak"),
            "--role",
            "both",
            "--seconds",
            "5",
            "--integrity-every",
            "2",
            "--seed",
            "1",
            "--summary",
            str(summary_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(summary_path.read_text())
    assert summary == json.loads(completed.stdout)
    assert summary["busy_errors"] == 0
    assert summary["other_errors"] == 0
    assert summary["passed"] is True
    assert summary["integrity_checks"]
    assert all(check["result"] == "ok" for check in summary["integrity_checks"])
    writer = summary["children"]["writer"]
    reader = summary["children"]["reader"]
    assert writer["transactions"]["document_update"] >= 3
    assert reader["transactions"]["job_enqueue"] >= 10
    assert reader["settings"] == {
        "journal_mode": "wal",
        "synchronous": 1,
        "busy_timeout_ms": 5000,
        "foreign_keys": 1,
        "user_version": 1,
    }
