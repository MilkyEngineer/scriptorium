"""In-memory fake of ``library/archive.py:Archive`` (plan §3.2) that records every operation.

Blobs are real files under ``<root>/blobs`` (callers read them through ``blob_path``); the
library, tombstones and the per-generation holding area are dictionaries of
``name -> sha256``, so a test can assert exactly what moved where. Like the real archive
it has no method that deletes: superseded files go to ``holding``, removed documents to
``tombstones``. ``inject`` raises an exception (usually :class:`SimulatedCrash`, standing in
for SIGKILL) at a named point.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

# Until A's layout module is merged, the mirror in cloud/ports.py is the allowlist.
from scriptorium.cloud.ports import is_safe_name
from scriptorium.errors import RefusedError

_UUID_LEN = 36


class SimulatedCrash(BaseException):
    """Raised by a fake at an injected point; not an ``Exception``, so nothing catches it."""


@dataclass
class Staging:
    uuid: str
    job: str
    open: bool = True


@dataclass(frozen=True)
class Committed:
    uuid: str
    generation: int
    hash_root: str


@dataclass
class FakeArchive:
    root: Path
    library: dict[str, dict[str, str]] = field(default_factory=dict)
    tombstones: dict[str, dict[str, str]] = field(default_factory=dict)
    holding: list[tuple[str, int, dict[str, str]]] = field(default_factory=list)
    """``(uuid, generation the files were superseded at, files)``."""
    generations: dict[str, int] = field(default_factory=dict)
    staging: dict[str, Staging] = field(default_factory=dict)
    ops: list[tuple[str, ...]] = field(default_factory=list)
    faults: dict[str, tuple[int, BaseException]] = field(default_factory=dict)
    blobs_written: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)

    # ---- plan §3.2 --------------------------------------------------------------------

    @contextmanager
    def stage(self, uuid: str, job: str) -> Iterator[Staging]:
        _check_uuid(uuid)
        key = f"{uuid}.{job}"
        staged = Staging(uuid, job)
        self.staging[key] = staged
        self.ops.append(("stage", uuid))
        try:
            yield staged
        except Exception:
            # The real archive sweeps its staging directory on an error. A BaseException
            # (a simulated SIGKILL) leaves it for sweep_staging(), as a dead process would.
            self.staging.pop(key, None)
            raise
        else:
            self.staging.pop(key, None)
        staged.open = False

    def put_blob(self, sha256: str, stream: BinaryIO) -> Path:
        self._maybe_crash("put_blob")
        target = self.blob_path(sha256)
        hasher = hashlib.sha256()
        tmp = self.root / "tmp" / f"{sha256}.{os.getpid()}.part"
        try:
            with tmp.open("wb") as out:
                while chunk := stream.read(64 * 1024):
                    hasher.update(chunk)
                    out.write(chunk)
            if hasher.hexdigest() != sha256:
                raise RefusedError("blob does not match its hash")
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp.replace(target)
                self.blobs_written.append(sha256)
        finally:
            tmp.unlink(missing_ok=True)  # the temporary file only, never under blobs/
        self.ops.append(("put_blob", sha256))
        return target

    def commit(
        self, staged: Staging, files: Mapping[str, str], generation: int, hash_root: str
    ) -> Committed:
        self._maybe_crash("commit")
        if not staged.open or self.staging.get(f"{staged.uuid}.{staged.job}") is not staged:
            raise RefusedError("commit of a staging area that is not open")
        uuid = staged.uuid
        for name, sha in files.items():
            if not is_safe_name(name) or not (
                name.startswith(f"{uuid}.") or name.startswith(f"{uuid}/")
            ):
                raise RefusedError("commit of a file name outside the allowlist")
            if not self.blob_path(sha).is_file():
                raise RefusedError("commit of a file whose blob is missing")
        previous = self.library.get(uuid)
        if previous is not None:
            self.holding.append((uuid, generation, previous))
        self.library[uuid] = dict(files)
        self.generations[uuid] = generation
        self.ops.append(("commit", uuid, str(generation), hash_root))
        return Committed(uuid, generation, hash_root)

    def tombstone(self, uuid: str) -> None:
        self._maybe_crash("tombstone")
        _check_uuid(uuid)
        files = self.library.pop(uuid, {})
        self.tombstones[uuid] = files
        self.ops.append(("tombstone", uuid))

    def untombstone(self, uuid: str) -> None:
        self._maybe_crash("untombstone")
        _check_uuid(uuid)
        files = self.tombstones.pop(uuid, {})
        self.library[uuid] = files
        self.ops.append(("untombstone", uuid))

    def sweep_staging(self) -> int:
        count = len(self.staging)
        self.staging.clear()
        self.ops.append(("sweep_staging", str(count)))
        return count

    def current_files(self, uuid: str) -> Mapping[str, Path]:
        return {name: self.blob_path(sha) for name, sha in self.library.get(uuid, {}).items()}

    def blob_path(self, sha256: str) -> Path:
        if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
            raise RefusedError("not a SHA-256")
        return self.root / "blobs" / sha256[:2] / sha256

    # ---- test helpers -----------------------------------------------------------------

    def op_names(self) -> list[str]:
        return [op[0] for op in self.ops]

    def assert_nothing_lost(self) -> None:
        """Every blob ever written is still on disk; every file set ever committed is held."""
        for sha in self.blobs_written:
            assert self.blob_path(sha).is_file(), f"blob {sha[:12]} disappeared"
        for uuid, files in [*self.library.items(), *self.tombstones.items()]:
            for sha in files.values():
                assert self.blob_path(sha).is_file(), f"{uuid} references a missing blob"
        for _uuid, _gen, files in self.holding:
            for sha in files.values():
                assert self.blob_path(sha).is_file()

    def inject(self, point: str, exc: BaseException, *, after: int = 0) -> None:
        """Raise ``exc`` at the next call of ``point`` that follows ``after`` clean calls.

        Points: ``put_blob``, ``commit``, ``tombstone``, ``untombstone``. Fires once.
        """
        self.faults[point] = (after, exc)

    def _maybe_crash(self, point: str) -> None:
        fault = self.faults.get(point)
        if fault is None:
            return
        remaining, exc = fault
        if remaining > 0:
            self.faults[point] = (remaining - 1, exc)
            return
        del self.faults[point]
        raise exc


def _check_uuid(uuid: str) -> None:
    if len(uuid) != _UUID_LEN or not is_safe_name(uuid):
        raise RefusedError("not a document UUID")
