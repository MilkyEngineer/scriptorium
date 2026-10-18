"""Regenerate the synthetic cassettes from the in-memory cloud.

    uv run python -m tests.cloud.build_cassettes

Runs every scenario in ``scenarios.py`` against ``FakeCloud`` through a synthetic
``Recorder`` and writes ``tests/fixtures/cassettes/<name>.json``. No network: the
recorder refuses any transport but an in-memory one unless it is built with
``Recorder.live`` (operator only). ``test_cassettes.py`` checks that the committed files
are exactly what this produces.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from tests.cloud.cassettes import CASSETTE_DIR, Recorder, save_cassette
from tests.cloud.fake_cloud import FakeCloud
from tests.cloud.scenarios import SCENARIOS, Scenario, harness


def record(scenario: Scenario, out_dir: Path) -> Path:
    cloud = FakeCloud(root_schema=scenario.root_schema)
    recorder = Recorder.synthetic(cloud.handler)
    with tempfile.TemporaryDirectory(prefix="cassette-") as tmp:
        h = harness(recorder, Path(tmp), client=scenario.client)
        try:
            for step in scenario.steps:
                step(cloud)
                h.poll()
        finally:
            h.client.close()
    path = out_dir / f"{scenario.name}.json"
    save_cassette(path, recorder.cassette(description=f"Synthetic. {scenario.description}"))
    return path


def main(argv: list[str]) -> int:
    out_dir = Path(argv[0]) if argv else CASSETTE_DIR
    for scenario in SCENARIOS.values():
        print(record(scenario, out_dir).name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
