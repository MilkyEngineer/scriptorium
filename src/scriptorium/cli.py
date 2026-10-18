"""Command-line entry point: ``scriptorium serve | worker | gen-master-key | licence-check``.

The two container entrypoints (``serve``, ``worker``) share one image, one
import graph and one settings object; the rest are local operator helpers.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from scriptorium.config import Settings

LOG_LEVELS: Final = ("debug", "info", "warning", "error")


# ---- serve / worker -------------------------------------------------------------------------


def _start(args: argparse.Namespace) -> Settings | None:
    """Settings, logging and the ``/data`` check shared by ``serve`` and ``worker``."""
    from scriptorium.config import ConfigError, load_settings
    from scriptorium.logging import configure_logging
    from scriptorium.users.fs import UnsafeDataDir, require_local_data_dir

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"scriptorium: {exc}", file=sys.stderr)
        return None
    configure_logging(args.log_level or settings.log_level)
    try:
        require_local_data_dir(settings.data_dir)
    except UnsafeDataDir as exc:  # FUSE, network, overlay or unwritable: never risk SQLite
        print(f"scriptorium: {exc}", file=sys.stderr)
        return None
    return settings


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from scriptorium.server import create_app
    from scriptorium.tools._services import Services

    settings = _start(args)
    if settings is None:
        return 2
    # The server owns migrations (§6.1): the global database and every account index are
    # brought up to date before the first request; the worker waits for them.
    services = Services.open(settings)
    try:
        uvicorn.run(
            create_app(settings, services=services),
            host=settings.bind_host,
            port=settings.bind_port,
            log_config=None,  # keep the JSON logging configured above
            log_level=None,
            access_log=False,  # the app logs requests itself, with /dl tokens redacted
            proxy_headers=False,  # X-Forwarded-* is handled by TRUSTED_PROXIES in the app
            server_header=False,
            timeout_graceful_shutdown=10,
        )
    finally:
        services.close()
    return 0


WORKER_ALREADY_RUNNING: Final = 3
"""``scriptorium worker``'s exit code when another worker holds ``<data>/worker.lock``."""


async def _run_worker(settings: Settings) -> int:
    import logging

    from scriptorium.logging import log_event
    from scriptorium.worker import Worker, acquire_worker_lock

    lock = acquire_worker_lock(settings.data_dir)
    if lock is None:  # a second worker on this /data would duplicate every poll and render
        log_event(logging.getLogger("scriptorium.worker"), "worker_already_running")
        return WORKER_ALREADY_RUNNING
    try:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):  # no signal handlers on Windows
                loop.add_signal_handler(sig, stop.set)
        worker = await Worker.open(settings, stop)
        if worker is None:  # stopped while waiting for the server's migrations
            return 0
        try:
            await worker.run(stop)
        finally:
            worker.close()
        return 0
    finally:
        os.close(lock)  # releases the flock


def cmd_worker(args: argparse.Namespace) -> int:
    settings = _start(args)
    if settings is None:
        return 2
    return asyncio.run(_run_worker(settings))


def cmd_healthcheck(args: argparse.Namespace) -> int:
    """Container HEALTHCHECK: GET /healthz on the local listener, or with ``--worker`` the
    age of the worker's heartbeat file under ``SCRIPTORIUM_DATA_DIR``."""
    if args.worker:
        from scriptorium.users.store import worker_heartbeat_path
        from scriptorium.worker import HEARTBEAT_STALE_SECONDS, heartbeat_age

        data_dir = Path(os.environ.get("SCRIPTORIUM_DATA_DIR") or "/data")
        age = heartbeat_age(worker_heartbeat_path(data_dir))
        return 0 if age is not None and age < HEARTBEAT_STALE_SECONDS else 1

    import httpx

    bind = os.environ.get("SCRIPTORIUM_BIND", "0.0.0.0:8000")
    port = bind.rpartition(":")[2] or "8000"
    try:
        response = httpx.get(f"http://127.0.0.1:{int(port)}/healthz", timeout=3.0)
    except (httpx.HTTPError, ValueError):
        return 1
    return 0 if response.status_code == 200 else 1


# ---- gen-master-key ----------------------------------------------------------------------------


def cmd_gen_master_key(args: argparse.Namespace) -> int:
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode("ascii"))
    return 0


# ---- licence-check ---------------------------------------------------------------------------

COPYLEFT_RE: Final = re.compile(r"\b(?:A|L)?GPL|General Public Licen[cs]e|Affero", re.IGNORECASE)
FORBIDDEN_MODULES: Final = ("fitz", "pymupdf", "ebooklib", "rmink")
FORBIDDEN_DISTRIBUTIONS: Final = frozenset({"pymupdf", "pymupdfb", "ebooklib", "rmink"})
LICENCE_ALLOWLIST: Final = {
    "resvg-py": (
        "MIT project bundling the MPL-2.0 resvg crate; its wheel has no licence metadata "
        "(Architecture §4.4)"
    ),
}


@dataclass(frozen=True, slots=True)
class PackageLicence:
    name: str
    version: str
    licence: str


def normalise_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def licence_acceptable(licence: str) -> bool:
    """True unless every alternative of ``licence`` is copyleft or unknown.

    SPDX ``OR`` offers a choice; ``AND`` and pip-licenses' ``;``-joined classifiers
    must all be acceptable.
    """
    for alternative in re.split(r"\s+OR\s+", licence.replace("(", " ").replace(")", " ")):
        parts = [p.strip() for p in re.split(r"\s+AND\s+|;", alternative) if p.strip()]
        if parts and all(p.upper() != "UNKNOWN" and not COPYLEFT_RE.search(p) for p in parts):
            return True
    return False


def _from_pip_licenses() -> list[PackageLicence] | None:
    """Run pip-licenses over this interpreter's environment, if it is installed."""
    if importlib.util.find_spec("piplicenses") is None:
        return None
    proc = subprocess.run(  # fixed argument list, no shell, no user input
        [sys.executable, "-m", "piplicenses", "--format=json", "--from=mixed", "--with-system"],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pip-licenses failed with exit code {proc.returncode}")
    rows = json.loads(proc.stdout)
    return [PackageLicence(r["Name"], r["Version"], r["License"]) for r in rows]


def _from_metadata() -> list[PackageLicence]:
    """The same "mixed" rule as pip-licenses, read with importlib.metadata.

    Used where pip-licenses (a dev dependency) is not installed, e.g. in the image.
    """
    found = []
    for dist in importlib.metadata.distributions():
        md = dist.metadata
        expression = md.get("License-Expression")
        classifiers = [
            c.split("::")[-1].strip()
            for c in md.get_all("Classifier") or []
            if c.startswith("License ::") and c.count("::") >= 2
        ]
        free_text = (md.get("License") or "").strip().splitlines()
        if expression:
            licence = expression
        elif classifiers:
            licence = "; ".join(classifiers)
        elif free_text and free_text[0] != "UNKNOWN":
            licence = free_text[0][:200]
        else:
            licence = "UNKNOWN"
        found.append(PackageLicence(md["Name"], dist.version, licence))
    return found


def licence_problems(packages: Sequence[PackageLicence]) -> tuple[list[str], list[str]]:
    """(problems, notes) for the given packages plus the forbidden-module check."""
    problems: list[str] = []
    notes: list[str] = []
    for pkg in sorted(packages, key=lambda p: normalise_name(p.name)):
        name = normalise_name(pkg.name)
        if name in FORBIDDEN_DISTRIBUTIONS:
            problems.append(f"{pkg.name} {pkg.version}: excluded dependency (AGPL), see §4.4")
        elif name in LICENCE_ALLOWLIST:
            notes.append(
                f"allowlisted {pkg.name} {pkg.version} ({pkg.licence}): {LICENCE_ALLOWLIST[name]}"
            )
        elif not licence_acceptable(pkg.licence):
            problems.append(f"{pkg.name} {pkg.version}: licence {pkg.licence!r} is not allowed")
    for module in FORBIDDEN_MODULES:
        if importlib.util.find_spec(module) is not None:
            problems.append(f"module {module!r} is importable; it must never be installed")
    return problems, notes


def cmd_licence_check(args: argparse.Namespace) -> int:
    packages = _from_pip_licenses()
    source = "pip-licenses"
    if packages is None:
        packages = _from_metadata()
        source = "distribution metadata (pip-licenses not installed)"
    problems, notes = licence_problems(packages)
    for note in notes:
        print(f"note: {note}")
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    verdict = "failed" if problems else "passed"
    print(f"licence-check {verdict}: {len(packages)} distributions checked via {source}")
    return 1 if problems else 0


# ---- dispatch ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scriptorium")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--log-level", choices=LOG_LEVELS, help="override SCRIPTORIUM_LOG_LEVEL")
    serve.set_defaults(func=cmd_serve)

    worker = sub.add_parser("worker", help="run the sync worker")
    worker.add_argument("--log-level", choices=LOG_LEVELS, help="override SCRIPTORIUM_LOG_LEVEL")
    worker.set_defaults(func=cmd_worker)

    gen = sub.add_parser("gen-master-key", help="print a new SCRIPTORIUM_MASTER_KEYS key")
    gen.set_defaults(func=cmd_gen_master_key)

    lic = sub.add_parser("licence-check", help="fail on GPL/AGPL/LGPL or excluded packages")
    lic.set_defaults(func=cmd_licence_check)

    health = sub.add_parser("healthcheck", help="container health probe against /healthz")
    health.add_argument(
        "--worker", action="store_true", help="check the worker's heartbeat file instead"
    )
    health.set_defaults(func=cmd_healthcheck)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
