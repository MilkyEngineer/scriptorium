"""Archive tests. Every test that uses the ``archive`` fixture is also checked for the hard rule:
nothing under library/, blobs/ or tombstones/ is ever unlinked, removed or replaced, and no
content leaves those trees (checked at the facade and, independently, at the ``os`` level)."""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scriptorium.errors import NotFoundError, PathEscapeError, RefusedError
from scriptorium.library import archive as archive_mod
from scriptorium.library.archive import HOLDING_DIR, Archive, Committed
from scriptorium.users.store import AccountPaths, account_paths

if sys.platform != "win32":
    import fcntl

UID = "0123456789abcdef"
ACCT = "01J9Z8Y7X6W5V4T3S2R1Q0P0N0"
DOC = "00000000-0000-4000-8000-0000000000d1"
DOC2 = "00000000-0000-4000-8000-0000000000d2"
P1 = "00000000-0000-4000-8000-0000000001a1"
P2 = "00000000-0000-4000-8000-0000000001a2"
HASH_ROOT = "f" * 64

# Synthetic bytes standing in for xochitl files; no real note content.
V1 = {
    f"{DOC}.metadata": b'{"visibleName":"v1"}',
    f"{DOC}.content": b'{"v":1}',
    f"{DOC}/{P1}.rm": b"page one, version one",
    f"{DOC}/{P2}.rm": b"page two, version one",
}
V2 = {
    f"{DOC}.metadata": b'{"visibleName":"v2"}',
    f"{DOC}.content": b'{"v":2}',
    f"{DOC}/{P1}.rm": b"page one, version two",
}
OTHER = {f"{DOC2}.metadata": b'{"visibleName":"other"}', f"{DOC2}.pdf": b"%PDF-synthetic"}
# Every per-page shape: layer names, highlights, thumbnails (both formats), text conversion.
RICH = {
    f"{DOC}.metadata": b'{"visibleName":"rich"}',
    f"{DOC}.content": b'{"v":3}',
    f"{DOC}.pdf": b"%PDF-synthetic rich",
    f"{DOC}/{P1}.rm": b"page one, rich",
    f"{DOC}/{P1}-metadata.json": b'{"layers":[{"name":"synthetic"}]}',
    f"{DOC}.highlights/{P1}.json": b'{"highlights":[]}',
    f"{DOC}.thumbnails/{P1}.png": b"synthetic png",
    f"{DOC}.thumbnails/{P2}.jpg": b"synthetic jpg",
    f"{DOC}.textconversion/{P1}.json": b'{"synthetic":true}',
}
# The next version: no highlights or text conversion any more, a changed thumbnail.
RICH2 = {
    f"{DOC}.metadata": b'{"visibleName":"rich2"}',
    f"{DOC}.content": b'{"v":4}',
    f"{DOC}/{P1}.rm": b"page one, rich2",
    f"{DOC}.thumbnails/{P1}.png": b"synthetic png, version two",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SimulatedCrash(BaseException):
    """Raised from the filesystem facade; BaseException so no cleanup code catches it."""


class RecordingFs(archive_mod._Fs):
    """Records every mutation (parent resolved, final component kept); can crash or refuse links."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, bool]] = []  # op, path, other, existed
        self.crash_when: Callable[[str, Path, Path | None], bool] | None = None
        self.refuse_links = False

    @staticmethod
    def real(p: Path) -> str:
        return os.path.join(os.path.realpath(p.parent), p.name)

    def _note(self, op: str, path: Path, other: Path | None = None) -> None:
        existed = os.path.lexists(other) if other is not None else False
        self.calls.append(
            (op, self.real(path), None if other is None else self.real(other), existed)
        )
        if self.crash_when is not None and self.crash_when(op, path, other):
            raise SimulatedCrash(op)

    def mkdir(self, path: Path, *, exclusive: bool = False) -> None:
        self._note("mkdir", path)
        super().mkdir(path, exclusive=exclusive)

    def rename(self, src: Path, dst: Path) -> None:
        self._note("rename", src, dst)
        super().rename(src, dst)

    def link(self, src: Path, dst: Path) -> None:
        self._note("link", src, dst)
        if self.refuse_links:
            raise OSError(errno.EXDEV, "simulated cross-device link")
        super().link(src, dst)

    def create(self, path: Path, mode: int = 0o600) -> int:
        self._note("create", path)
        return super().create(path, mode)

    def chmod(self, path: Path, mode: int) -> None:
        self._note("chmod", path)
        super().chmod(path, mode)

    def unlink(self, path: Path) -> None:
        self._note("unlink", path)
        super().unlink(path)

    def rmtree(self, path: Path) -> None:
        self._note("rmtree", path)
        super().rmtree(path)

    def rmdir(self, path: Path) -> None:
        self._note("rmdir", path)
        super().rmdir(path)

    def fsync_dir(self, path: Path) -> None:
        self._note("fsync_dir", path)
        super().fsync_dir(path)


def _under(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r + os.sep) for r in roots)


def _protected(paths: AccountPaths) -> list[str]:
    return [os.path.realpath(p) for p in (paths.blobs, paths.library, paths.tombstones)]


def assert_never_deleted(rec: RecordingFs, paths: AccountPaths) -> None:
    roots = _protected(paths)
    staging = [os.path.realpath(paths.staging)]
    tombstones = [os.path.realpath(paths.tombstones)]
    for op, path, other, existed in rec.calls:
        if op in ("unlink", "rmtree"):
            assert not _under(path, roots), (op, path)
            assert _under(path, staging), (op, path)
        elif op == "rmdir":  # only ever an empty tombstone directory, or staging
            assert _under(path, tombstones + staging), (op, path)
        elif op == "rename":
            assert other is not None
            if _under(path, roots):  # content never leaves the protected trees
                assert _under(other, roots), (op, path, other)
            if _under(other, roots):  # and nothing there is ever replaced
                assert not existed, (op, other)
        # link(2) cannot replace: onto an existing name it fails with EEXIST.


def census(paths: AccountPaths) -> set[tuple[int, int]]:
    """Every regular file (by inode) under the protected trees."""
    out = set()
    for root in (paths.blobs, paths.library, paths.tombstones):
        for directory, _dirs, files in os.walk(root):
            for name in files:
                st = os.lstat(os.path.join(directory, name))
                if stat.S_ISREG(st.st_mode):
                    out.add((st.st_dev, st.st_ino))
    return out


def tree(base: Path, *, skip_holding: bool = True) -> dict[str, bytes]:
    out = {}
    for directory, dirs, files in os.walk(base):
        if skip_holding and HOLDING_DIR in dirs and Path(directory) == base:
            dirs.remove(HOLDING_DIR)
        for name in files:
            p = Path(directory) / name
            out[p.relative_to(base).as_posix()] = p.read_bytes()
    return out


def holding(paths: AccountPaths, label: str) -> dict[str, bytes]:
    return tree(paths.library / HOLDING_DIR / DOC / label, skip_holding=False)


# ---- fixtures -------------------------------------------------------------------------------


@pytest.fixture
def paths(tmp_path: Path) -> AccountPaths:
    return account_paths(tmp_path / "data", UID, ACCT)


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> RecordingFs:
    recorder = RecordingFs()
    monkeypatch.setattr(archive_mod, "_fs", recorder)
    return recorder


@pytest.fixture
def os_spy(monkeypatch: pytest.MonkeyPatch, paths: AccountPaths) -> Iterator[list[Any]]:
    """Independent of the facade: no os-level delete/replace ever targets a protected tree."""
    roots = _protected(paths)
    violations: list[Any] = []

    def real(p: Any) -> str | None:
        if not isinstance(p, str | os.PathLike):
            return None
        text = os.fspath(p)
        if isinstance(text, bytes) or not os.path.isabs(text):
            return None  # relative names under a dir_fd: only rmtree internals (checked at entry)
        return os.path.join(os.path.realpath(os.path.dirname(text)), os.path.basename(text))

    def deleting(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(path: Any, *args: Any, **kwargs: Any) -> Any:
            p = real(path)
            if kwargs.get("dir_fd") is None and p is not None and _under(p, roots):
                violations.append((name, p))
            return original(path, *args, **kwargs)

        return wrapper

    def moving(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
            s, d = real(src), real(dst)
            if d is not None and _under(d, roots) and (name == "replace" or os.path.lexists(d)):
                violations.append((name, "replaces", d))
            if s is not None and _under(s, roots) and not (d is not None and _under(d, roots)):
                violations.append((name, "leaves", s))
            return original(src, dst, *args, **kwargs)

        return wrapper

    for name in ("unlink", "remove", "rmdir"):
        if name == "rmdir":
            continue  # rmdir only removes empty directories; the facade check limits where
        monkeypatch.setattr(os, name, deleting(name, getattr(os, name)))
    monkeypatch.setattr(os, "removedirs", deleting("removedirs", os.removedirs))
    monkeypatch.setattr(shutil, "rmtree", deleting("rmtree", shutil.rmtree))
    for name in ("rename", "replace"):
        monkeypatch.setattr(os, name, moving(name, getattr(os, name)))
    yield violations
    assert violations == []


@pytest.fixture
def archive(paths: AccountPaths, rec: RecordingFs, os_spy: list[Any]) -> Iterator[Archive]:
    yield Archive(paths)
    assert_never_deleted(rec, paths)


def store(archive: Archive, files: dict[str, bytes]) -> dict[str, str]:
    out = {}
    for name, data in files.items():
        digest = sha(data)
        archive.put_blob(digest, io.BytesIO(data))
        out[name] = digest
    return out


def commit(
    archive: Archive, files: dict[str, bytes], generation: int, *, uuid: str = DOC, job: str = "j"
) -> Committed:
    mapping = store(archive, files)
    with archive.stage(uuid, job) as staged:
        return archive.commit(staged, mapping, generation, HASH_ROOT)


# ---- blobs ----------------------------------------------------------------------------------


def test_put_blob_stores_a_verified_read_only_blob(archive: Archive, paths: AccountPaths) -> None:
    data = b"x" * (3 * archive_mod._CHUNK + 17)  # several chunks
    path = archive.put_blob(sha(data), io.BytesIO(data))
    assert path == Path(os.path.realpath(paths.blobs)) / sha(data)
    assert path.read_bytes() == data
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert os.listdir(paths.staging / ".incoming") == []


def test_put_blob_refuses_a_hash_mismatch(archive: Archive, paths: AccountPaths) -> None:
    with pytest.raises(RefusedError):
        archive.put_blob(sha(b"expected"), io.BytesIO(b"something else"))
    assert os.listdir(paths.blobs) == []
    assert os.listdir(paths.staging / ".incoming") == []


def test_put_blob_is_idempotent_and_does_not_read_twice(archive: Archive) -> None:
    data = b"same"
    first = archive.put_blob(sha(data), io.BytesIO(data))

    class Exploding(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            raise AssertionError("an existing blob must not be downloaded again")

    assert archive.put_blob(sha(data), Exploding()) == first  # type: ignore[arg-type]


def test_put_blob_concurrent_writers_of_one_blob(archive: Archive, paths: AccountPaths) -> None:
    data = b"raced" * 1000
    results: list[Path] = []
    errors: list[BaseException] = []

    def put() -> None:
        try:
            results.append(archive.put_blob(sha(data), io.BytesIO(data)))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=put) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(set(results)) == 1
    assert os.listdir(paths.blobs) == [sha(data)]
    assert os.listdir(paths.staging / ".incoming") == []


@pytest.mark.parametrize(
    "bad", ["A" * 64, "a" * 63, "a" * 65, "../" + "a" * 61, "a" * 64 + "\n", "", "g" * 64]
)
def test_put_blob_refuses_malformed_hashes(archive: Archive, paths: AccountPaths, bad: str) -> None:
    with pytest.raises(RefusedError):
        archive.put_blob(bad, io.BytesIO(b""))
    with pytest.raises(RefusedError):
        archive.blob_path(bad)
    assert os.listdir(paths.blobs) == []


def test_put_blob_refuses_a_planted_symlink(
    archive: Archive, paths: AccountPaths, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"data")
    (paths.blobs / sha(b"data")).symlink_to(outside)
    with pytest.raises(PathEscapeError):
        archive.put_blob(sha(b"data"), io.BytesIO(b"data"))


def test_put_blob_falls_back_to_rename_without_hardlinks(
    archive: Archive, rec: RecordingFs, paths: AccountPaths
) -> None:
    rec.refuse_links = True
    path = archive.put_blob(sha(b"abc"), io.BytesIO(b"abc"))
    assert path.read_bytes() == b"abc"
    assert os.listdir(paths.staging / ".incoming") == []


# ---- commit ---------------------------------------------------------------------------------


def test_commit_new_document(archive: Archive, paths: AccountPaths) -> None:
    result = commit(archive, V1, 5)
    assert result.uuid == DOC
    assert result.generation == 5
    assert result.hash_root == HASH_ROOT
    assert result.held is None
    assert tree(paths.library) == V1
    assert set(result.files) == set(V1)
    for name, path in archive.current_files(DOC).items():
        blob = archive.blob_path(sha(V1[name]))
        assert os.stat(path).st_ino == os.stat(blob).st_ino  # a hardlink, not a copy
        assert result.files[name] == path
    assert sorted(os.listdir(paths.staging)) == [".incoming", ".journal"]
    assert os.listdir(paths.staging / ".journal") == []


def test_commit_moves_previous_files_to_holding(archive: Archive, paths: AccountPaths) -> None:
    commit(archive, V1, 5)
    before = census(paths)
    result = commit(archive, V2, 6, job="k")
    assert tree(paths.library) == V2
    assert result.held == Path(os.path.realpath(paths.library)) / HOLDING_DIR / DOC / "6"
    assert holding(paths, "6") == V1  # page two survives, though the new version dropped it
    assert census(paths) >= before


def test_commit_same_generation_twice_gets_a_fresh_holding_dir(
    archive: Archive, paths: AccountPaths
) -> None:
    commit(archive, V1, 5)
    commit(archive, V2, 7, job="a")
    commit(archive, V1, 7, job="b")
    assert holding(paths, "7") == V1
    assert holding(paths, "7.1") == V2
    assert tree(paths.library) == V1


def test_commit_two_documents_do_not_touch_each_other(
    archive: Archive, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    commit(archive, OTHER, 1, uuid=DOC2)
    commit(archive, V2, 2)
    assert tree(paths.library) == {**V2, **OTHER}


@pytest.mark.parametrize(
    ("files", "generation", "hash_root"),
    [
        ({"../x": "a" * 64}, 1, HASH_ROOT),
        ({DOC: "a" * 64}, 1, HASH_ROOT),  # the bare <uuid> is the page directory, not a file
        ({f"{DOC}.thumbnails": "a" * 64}, 1, HASH_ROOT),  # and so is <uuid>.thumbnails
        ({f"{DOC}.highlights/../{DOC}.content": "a" * 64}, 1, HASH_ROOT),
        ({f"{DOC2}.thumbnails/{P1}.png": "a" * 64}, 1, HASH_ROOT),  # another document's
        ({f"{DOC}.thumbnails/{P1}.gif": "a" * 64}, 1, HASH_ROOT),
        ({f"{DOC2}.content": "a" * 64}, 1, HASH_ROOT),  # another document's file
        ({f"{DOC}.txt": "a" * 64}, 1, HASH_ROOT),
        ({f"{DOC}/../{DOC2}.content": "a" * 64}, 1, HASH_ROOT),
        ({f"/{DOC}.content": "a" * 64}, 1, HASH_ROOT),
        ({f"{DOC}.content": "A" * 64}, 1, HASH_ROOT),
        ({f"{DOC}.content": "a" * 64}, -1, HASH_ROOT),
        ({f"{DOC}.content": "a" * 64}, True, HASH_ROOT),
        ({f"{DOC}.content": "a" * 64}, 1, "not-a-hash"),
        ({}, 1, HASH_ROOT),
    ],
)
def test_commit_refuses_bad_input(
    archive: Archive, paths: AccountPaths, files: dict[str, str], generation: int, hash_root: str
) -> None:
    commit(archive, V1, 1)
    with archive.stage(DOC, "bad") as staged, pytest.raises(RefusedError):
        archive.commit(staged, files, generation, hash_root)
    assert tree(paths.library) == V1


def test_commit_refuses_a_missing_blob(archive: Archive, paths: AccountPaths) -> None:
    with archive.stage(DOC, "j") as staged, pytest.raises(NotFoundError):
        archive.commit(staged, {f"{DOC}.content": "a" * 64}, 1, HASH_ROOT)
    assert tree(paths.library) == {}


def test_a_staging_directory_commits_once(archive: Archive) -> None:
    mapping = store(archive, V1)
    with archive.stage(DOC, "j") as staged:
        archive.commit(staged, mapping, 1, HASH_ROOT)
        with pytest.raises(RefusedError):
            archive.commit(staged, mapping, 2, HASH_ROOT)
    with pytest.raises(RefusedError):  # closed
        archive.commit(staged, mapping, 3, HASH_ROOT)


def test_commit_refuses_another_archives_staging(archive: Archive, tmp_path: Path) -> None:
    other = Archive(account_paths(tmp_path / "other", UID, ACCT))
    other._fs = archive_mod._Fs()  # its own root: keep it out of this test's recorder
    mapping = store(archive, V1)
    with other.stage(DOC, "j") as staged, pytest.raises(RefusedError):
        archive.commit(staged, mapping, 1, HASH_ROOT)


def test_commit_copies_when_hardlinks_fail(
    archive: Archive, rec: RecordingFs, paths: AccountPaths
) -> None:
    rec.refuse_links = True
    commit(archive, V1, 1)
    assert tree(paths.library) == V1
    for name, path in archive.current_files(DOC).items():
        assert os.stat(path).st_ino != os.stat(archive.blob_path(sha(V1[name]))).st_ino
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o400


def test_commit_refuses_when_a_source_vanished_before_the_plan(
    archive: Archive, paths: AccountPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit(archive, V1, 1)
    mapping = store(archive, V2)
    staged = archive.stage(DOC, "j")
    claim = archive._claim_holding

    def claim_then_interfere(uuid: str, label: str) -> Path:
        held = claim(uuid, label)
        (staged.path / "tree" / f"{DOC}.content").unlink()  # something outside the archive
        return held

    monkeypatch.setattr(archive, "_claim_holding", claim_then_interfere)
    with staged, pytest.raises(RefusedError):
        archive.commit(staged, mapping, 2, HASH_ROOT)
    assert tree(paths.library) == V1  # nothing was journalled or moved
    assert os.listdir(paths.staging / ".journal") == []
    assert not staged.path.exists()


def test_a_source_vanishing_mid_swap_fails_the_commit_and_recovery_finishes(
    archive: Archive, rec: RecordingFs, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    mapping = store(archive, V2)
    staged = archive.stage(DOC, "j")
    victim = staged.path / "tree" / f"{DOC}.content"

    def interfere(op: str, _src: Path, dst: Path | None) -> bool:
        if op == "rename" and dst is not None and HOLDING_DIR in str(dst) and victim.exists():
            victim.unlink()  # something outside the archive, after the plan was journalled
        return False

    rec.crash_when = interfere
    with staged, pytest.raises(RefusedError):
        archive.commit(staged, mapping, 2, HASH_ROOT)  # the caller must not record success
    rec.crash_when = None
    assert len(os.listdir(paths.staging / ".journal")) == 1  # kept for recovery
    assert staged.path.exists()  # with the tree it still needs

    assert archive.take_damaged() == frozenset()
    recovered = Archive(paths)
    recovered.sweep_staging()
    assert tree(paths.library) == {k: v for k, v in V2.items() if k != f"{DOC}.content"}
    assert holding(paths, "2") == V1
    assert os.listdir(paths.staging / ".journal") == []
    # library/ now lacks part of version 2: sync nulls its hash_root (once per recovery).
    assert recovered.take_damaged() == {DOC}
    assert recovered.take_damaged() == frozenset()


def test_recovering_through_stage_reports_the_damage(
    archive: Archive, rec: RecordingFs, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    mapping = store(archive, V2)
    staged = archive.stage(DOC, "j")
    victim = staged.path / "tree" / f"{DOC}.content"

    def interfere(op: str, _src: Path, dst: Path | None) -> bool:
        if op == "rename" and dst is not None and HOLDING_DIR in str(dst) and victim.exists():
            victim.unlink()
        return False

    rec.crash_when = interfere
    with staged, pytest.raises(RefusedError):
        archive.commit(staged, mapping, 2, HASH_ROOT)
    rec.crash_when = None
    archive.tombstone(DOC)  # the next change of the document rolls the plan forward first
    assert archive.take_damaged() == {DOC}


def test_new_directories_are_fsynced_into_their_parents(
    archive: Archive, rec: RecordingFs, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)  # creates the journal directory
    commit(archive, V2, 2)  # creates library/.holding/<uuid>/2
    archive.tombstone(DOC)  # creates tombstones/<uuid>
    staging = os.path.realpath(paths.staging)
    journals = os.path.join(staging, ".journal") + os.sep
    incoming = os.path.join(staging, ".incoming")
    calls = rec.calls
    checked = 0
    for i, (op, path, _other, _existed) in enumerate(calls):
        if op != "mkdir" or path == incoming:  # partial blobs need no durable directory
            continue
        # Durable before the next plan becomes binding (its journal renamed into place).
        end = next(
            (
                j
                for j in range(i + 1, len(calls))
                if calls[j][0] == "rename" and (calls[j][2] or "").startswith(journals)
            ),
            len(calls),
        )
        synced = {c[1] for c in calls[i + 1 : end] if c[0] == "fsync_dir"}
        assert os.path.dirname(path) in synced, path
        checked += 1
    assert checked >= 6
    synced_all = {c[1] for c in calls if c[0] == "fsync_dir"}
    library = os.path.realpath(paths.library)
    for directory in (library, os.path.join(library, HOLDING_DIR), staging):
        assert directory in synced_all, directory
    assert os.path.realpath(paths.tombstones) in synced_all


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX")
def test_changes_wait_for_another_writer_on_the_account(
    archive: Archive, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    done = threading.Event()

    def tombstone() -> None:
        archive.tombstone(DOC)
        done.set()

    fd = os.open(paths.root / ".archive.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # another worker is mid-change on this account
        worker = threading.Thread(target=tombstone)
        worker.start()
        assert not done.wait(0.3)
        assert tree(paths.library) == V1
    finally:
        os.close(fd)
    assert done.wait(10)
    worker.join()
    assert tree(paths.tombstones / DOC) == V1


# ---- staging --------------------------------------------------------------------------------


def test_staging_is_removed_on_success_and_on_error(archive: Archive, paths: AccountPaths) -> None:
    with archive.stage(DOC, "ok") as staged:
        (staged.path / "partial").write_bytes(b"half a download")
    assert not staged.path.exists()

    with pytest.raises(RuntimeError), archive.stage(DOC, "err") as staged:
        (staged.path / "partial").write_bytes(b"half a download")
        raise RuntimeError("download failed")
    assert not staged.path.exists()
    assert tree(paths.library) == {}


@pytest.mark.parametrize(
    ("uuid", "job"),
    [
        (DOC.replace("d1", "D1"), "j"),
        ("../x", "j"),
        (DOC, "a/b"),
        (DOC, ""),
        (DOC, ".."),
        (DOC, "x" * 65),
        (DOC, "j\n"),
    ],
)
def test_stage_refuses_bad_identifiers(archive: Archive, uuid: str, job: str) -> None:
    with pytest.raises(RefusedError):
        archive.stage(uuid, job)


def test_stage_refuses_a_duplicate_but_not_another_job(archive: Archive) -> None:
    with archive.stage(DOC, "j"):
        with pytest.raises(RefusedError):
            archive.stage(DOC, "j")
        with archive.stage(DOC, "k"):
            pass


def test_stage_cleans_a_directory_left_by_a_crash(archive: Archive, paths: AccountPaths) -> None:
    stale = paths.staging / f"{DOC}.j"
    stale.mkdir()
    (stale / "partial").write_bytes(b"x")
    with archive.stage(DOC, "j") as staged:
        assert os.listdir(staged.path) == []


# ---- reading --------------------------------------------------------------------------------


def test_current_files_lists_only_allowlisted_names(archive: Archive, paths: AccountPaths) -> None:
    commit(archive, V1, 1)
    (paths.library / f"{DOC}.thumbnails").mkdir()
    (paths.library / f"{DOC}.local").write_bytes(b"x")
    (paths.library / DOC / "notes.txt").write_bytes(b"x")
    (paths.library / DOC / "ABCDEF00-0000-4000-8000-0000000000AB.rm").write_bytes(b"x")
    assert set(archive.current_files(DOC)) == set(V1)
    assert archive.current_files(DOC2) == {}


@pytest.mark.parametrize(
    "planted",
    [
        f"{DOC}.content",
        DOC,
        f"{DOC}/{P1}.rm",
        f"{DOC}/{P1}-metadata.json",
        f"{DOC}.highlights",
        f"{DOC}.thumbnails",
        f"{DOC}.thumbnails/{P1}.png",
        f"{DOC}.textconversion/{P1}.json",
    ],
)
def test_current_files_refuses_symlinks_out_of_the_library(
    archive: Archive, paths: AccountPaths, tmp_path: Path, planted: str
) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / f"{P1}.rm").write_bytes(b"not yours")
    if "/" in planted:
        (paths.library / planted).parent.mkdir()
    directory = planted in (DOC, f"{DOC}.highlights", f"{DOC}.thumbnails")
    (paths.library / planted).symlink_to(secret if directory else secret / f"{P1}.rm")
    with pytest.raises(PathEscapeError):
        archive.current_files(DOC)


@pytest.mark.parametrize("bad", ["../x", DOC.upper().replace("0", "G"), "", DOC + "/"])
def test_current_files_refuses_bad_uuids(archive: Archive, bad: str) -> None:
    with pytest.raises(RefusedError):
        archive.current_files(bad)


def test_current_files_lists_the_per_page_directories(
    archive: Archive, paths: AccountPaths
) -> None:
    commit(archive, RICH, 1)
    (paths.library / f"{DOC}.thumbnails" / f"{P1}.gif").write_bytes(b"x")
    (paths.library / f"{DOC}.highlights" / f"{P1}.png").write_bytes(b"x")
    (paths.library / f"{DOC}.highlightsx").mkdir()  # not the document's
    (paths.library / f"{DOC}.highlightsx" / f"{P1}.json").write_bytes(b"x")
    files = archive.current_files(DOC)
    assert set(files) == set(RICH)
    for name, path in files.items():
        assert path.read_bytes() == RICH[name]


def test_per_page_directories_commit_hold_tombstone_and_come_back(
    archive: Archive, paths: AccountPaths
) -> None:
    seen = census(paths)

    def nothing_lost() -> None:
        nonlocal seen
        now = census(paths)
        assert seen <= now
        seen = now

    first = commit(archive, RICH, 1)
    assert tree(paths.library) == RICH
    assert first.held is None
    assert set(first.files) == set(RICH)
    for name, path in first.files.items():  # hardlinks to the verified blobs
        assert os.stat(path).st_ino == os.stat(archive.blob_path(sha(RICH[name]))).st_ino
    nothing_lost()

    second = commit(archive, RICH2, 2)
    assert tree(paths.library) == RICH2  # the directories RICH2 lacks left the view whole
    assert not os.path.lexists(paths.library / f"{DOC}.highlights")
    assert not os.path.lexists(paths.library / f"{DOC}.textconversion")
    assert second.held is not None
    assert holding(paths, "2") == RICH  # the whole previous version, directories included
    nothing_lost()

    archive.tombstone(DOC)
    assert tree(paths.library) == {}
    assert tree(paths.tombstones / DOC) == RICH2
    assert archive.current_files(DOC) == {}
    nothing_lost()

    archive.untombstone(DOC)
    assert tree(paths.library) == RICH2
    assert set(archive.current_files(DOC)) == set(RICH2)
    assert not os.path.lexists(paths.tombstones / DOC)
    nothing_lost()

    commit(archive, RICH, 3)  # the directories come back with a later version
    assert tree(paths.library) == RICH
    assert holding(paths, "3") == RICH2
    nothing_lost()


def test_untombstone_beside_a_newer_version_holds_the_per_page_directories(
    archive: Archive, paths: AccountPaths
) -> None:
    commit(archive, RICH, 1)
    archive.tombstone(DOC)
    commit(archive, RICH2, 2)
    archive.untombstone(DOC)
    assert tree(paths.library) == RICH2
    assert holding(paths, "tombstone") == RICH  # never mixed into the newer version


@pytest.mark.parametrize("nth", [1, 4, 7])
def test_crash_mid_swap_with_per_page_directories_is_rolled_forward(
    archive: Archive, rec: RecordingFs, paths: AccountPaths, nth: int
) -> None:
    commit(archive, RICH, 1)  # seven top-level entries to hold
    rec.crash_when = _rename_into(HOLDING_DIR, nth)
    _crash_commit(archive, RICH2, 2)
    rec.crash_when = None
    Archive(paths).sweep_staging()
    assert tree(paths.library) == RICH2
    assert holding(paths, "2") == RICH


# ---- tombstones -----------------------------------------------------------------------------


def test_tombstone_moves_the_document_and_unlinks_nothing(
    archive: Archive, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    commit(archive, OTHER, 1, uuid=DOC2)
    before = census(paths)
    archive.tombstone(DOC)
    assert tree(paths.library) == OTHER
    assert tree(paths.tombstones / DOC) == V1
    assert archive.current_files(DOC) == {}
    assert census(paths) == before


def test_tombstone_is_a_no_op_without_files(archive: Archive, paths: AccountPaths) -> None:
    archive.tombstone(DOC)
    assert os.listdir(paths.tombstones) == []


def test_untombstone_restores_in_place(archive: Archive, paths: AccountPaths) -> None:
    commit(archive, V1, 1)
    archive.tombstone(DOC)
    archive.untombstone(DOC)
    assert tree(paths.library) == V1
    assert os.listdir(paths.tombstones) == []  # the empty directory is gone


def test_untombstone_when_a_newer_version_was_committed(
    archive: Archive, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    archive.tombstone(DOC)
    commit(archive, V2, 2, job="k")  # reappeared and changed before the un-tombstone ran
    archive.untombstone(DOC)
    assert tree(paths.library) == V2  # the two versions are never mixed
    assert holding(paths, "tombstone") == V1
    assert os.listdir(paths.tombstones) == []


def test_tombstone_again_keeps_the_older_tombstone(archive: Archive, paths: AccountPaths) -> None:
    commit(archive, V1, 1)
    archive.tombstone(DOC)
    commit(archive, V2, 2, job="k")
    archive.tombstone(DOC)
    assert tree(paths.tombstones / DOC) == V2
    assert holding(paths, "tombstone") == V1


def test_untombstone_is_a_no_op_without_a_tombstone(archive: Archive, paths: AccountPaths) -> None:
    archive.untombstone(DOC)
    (paths.tombstones / DOC).mkdir()
    archive.untombstone(DOC)
    assert os.listdir(paths.tombstones) == []


def test_tombstone_refuses_a_symlinked_tombstone_directory(
    archive: Archive, paths: AccountPaths, tmp_path: Path
) -> None:
    commit(archive, V1, 1)
    (tmp_path / "elsewhere").mkdir()
    (paths.tombstones / DOC).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(RefusedError):
        archive.tombstone(DOC)
    assert tree(paths.library) == V1
    assert os.listdir(tmp_path / "elsewhere") == []


def test_full_lifecycle_never_loses_a_file(archive: Archive, paths: AccountPaths) -> None:
    seen: set[tuple[int, int]] = set()

    def step(action: Callable[[], object]) -> None:
        action()
        now = census(paths)
        assert now >= seen, "a file disappeared from the protected trees"
        seen.update(now)

    step(lambda: commit(archive, V1, 1))
    step(lambda: commit(archive, OTHER, 1, uuid=DOC2))
    step(lambda: commit(archive, V2, 2, job="a"))
    step(lambda: archive.tombstone(DOC))
    step(lambda: archive.untombstone(DOC))
    step(lambda: archive.tombstone(DOC))
    step(lambda: commit(archive, V1, 3, job="b"))
    step(lambda: archive.untombstone(DOC))
    step(lambda: archive.tombstone(DOC2))
    step(lambda: archive.tombstone(DOC))
    step(lambda: archive.untombstone(DOC2))
    step(archive.sweep_staging)
    assert tree(paths.library) == OTHER
    assert tree(paths.tombstones / DOC) == V1


# ---- crashes --------------------------------------------------------------------------------


def _rename_into(part: str, nth: int) -> Callable[[str, Path, Path | None], bool]:
    """Crash on the nth rename whose destination path contains ``part``."""
    count = 0

    def when(op: str, _src: Path, dst: Path | None) -> bool:
        nonlocal count
        if op == "rename" and dst is not None and part in str(dst):
            count += 1
            return count == nth
        return False

    return when


def _crash_commit(archive: Archive, files: dict[str, bytes], generation: int) -> None:
    mapping = store(archive, files)
    staged = archive.stage(DOC, "crash")  # no `with`: a crash runs no cleanup
    with pytest.raises(SimulatedCrash):
        archive.commit(staged, mapping, generation, HASH_ROOT)


def test_crash_between_prepare_and_swap_keeps_the_old_tree(
    archive: Archive, rec: RecordingFs, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    store(archive, V2)
    rec.crash_when = lambda op, path, _o: op == "create" and ".journal" in str(path)
    _crash_commit(archive, V2, 2)
    assert tree(paths.library) == V1  # prepared, never swapped
    rec.crash_when = None

    restarted = Archive(paths)
    assert restarted.sweep_staging() >= 1
    assert tree(paths.library) == V1
    assert sorted(os.listdir(paths.staging)) == [".incoming", ".journal"]
    assert os.listdir(paths.staging / ".journal") == []


def test_crash_mid_swap_is_rolled_forward_at_startup(
    archive: Archive, rec: RecordingFs, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    rec.crash_when = _rename_into(HOLDING_DIR, 2)
    _crash_commit(archive, V2, 2)
    half = tree(paths.library)
    assert half not in (V1, V2)  # the crash really landed between two renames
    rec.crash_when = None

    restarted = Archive(paths)
    restarted.sweep_staging()
    assert tree(paths.library) == V2
    assert holding(paths, "2") == V1
    assert sorted(os.listdir(paths.staging)) == [".incoming", ".journal"]
    assert os.listdir(paths.staging / ".journal") == []


def test_crash_while_placing_is_rolled_forward(
    archive: Archive, rec: RecordingFs, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    library = os.path.realpath(paths.library)
    rec.crash_when = lambda op, src, dst: (
        op == "rename" and dst is not None and str(dst) == os.path.join(library, f"{DOC}.content")
    )
    _crash_commit(archive, V2, 2)
    rec.crash_when = None
    Archive(paths).sweep_staging()
    assert tree(paths.library) == V2
    assert holding(paths, "2") == V1


def test_crash_mid_swap_is_settled_before_the_next_change(
    archive: Archive, rec: RecordingFs, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    rec.crash_when = _rename_into(HOLDING_DIR, 1)
    _crash_commit(archive, V2, 2)
    rec.crash_when = None
    restarted = Archive(paths)  # no sweep: the next change to the document settles it first
    restarted.tombstone(DOC)
    assert tree(paths.tombstones / DOC) == V2
    assert holding(paths, "2") == V1
    assert archive.current_files(DOC) == {}


def test_crash_mid_tombstone_is_rolled_forward(
    archive: Archive, rec: RecordingFs, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    rec.crash_when = _rename_into(os.path.join("tombstones", DOC), 2)
    with pytest.raises(SimulatedCrash):
        archive.tombstone(DOC)
    rec.crash_when = None
    assert tree(paths.library) not in ({}, V1)
    Archive(paths).sweep_staging()
    assert tree(paths.library) == {}
    assert tree(paths.tombstones / DOC) == V1


def test_crash_mid_untombstone_is_rolled_forward(
    archive: Archive, rec: RecordingFs, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    archive.tombstone(DOC)
    library = os.path.realpath(paths.library)
    count = 0

    def when(op: str, _src: Path, dst: Path | None) -> bool:
        nonlocal count
        if op == "rename" and dst is not None and str(dst.parent) == library:
            count += 1
            return count == 2
        return False

    rec.crash_when = when
    with pytest.raises(SimulatedCrash):
        archive.untombstone(DOC)
    rec.crash_when = None
    Archive(paths).sweep_staging()
    assert tree(paths.library) == V1
    assert os.listdir(paths.tombstones) == []


# ---- sweeping -------------------------------------------------------------------------------


def test_sweep_removes_only_stale_staging(archive: Archive, paths: AccountPaths) -> None:
    commit(archive, V1, 1)
    before = (census(paths), tree(paths.library), tree(paths.blobs, skip_holding=False))
    (paths.staging / f"{DOC}.old").mkdir()
    (paths.staging / f"{DOC}.old" / "partial").write_bytes(b"x")
    (paths.staging / ".incoming" / f"{'a' * 64}.0000.part").write_bytes(b"x")
    (paths.staging / ".journal" / ".half-written.tmp").write_bytes(b"{")
    (paths.staging / "stray-file").write_bytes(b"x")
    assert archive.sweep_staging() == 4
    assert sorted(os.listdir(paths.staging)) == [".incoming", ".journal"]
    assert os.listdir(paths.staging / ".incoming") == []
    assert (census(paths), tree(paths.library), tree(paths.blobs, skip_holding=False)) == before


def test_sweep_spares_what_is_in_use(archive: Archive, paths: AccountPaths) -> None:
    with archive.stage(DOC, "live") as staged:
        (staged.path / "partial").write_bytes(b"x")
        archive._inflight.add("busy.part")
        (paths.staging / ".incoming").mkdir(exist_ok=True)
        (paths.staging / ".incoming" / "busy.part").write_bytes(b"x")
        assert archive.sweep_staging() == 0
        assert (staged.path / "partial").exists()
        assert (paths.staging / ".incoming" / "busy.part").exists()
        archive._inflight.discard("busy.part")


def _plant_journal(paths: AccountPaths, uuid: str, content: bytes) -> Path:
    directory = paths.staging / ".journal"
    directory.mkdir(exist_ok=True)
    path = directory / f"{uuid}.{1:020d}.0123abcd.json"
    path.write_bytes(content)
    return path


def test_a_stuck_journal_is_kept_with_its_documents_staging(
    archive: Archive, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    journal = _plant_journal(paths, DOC, b"{not json")
    (paths.staging / f"{DOC}.x").mkdir()
    (paths.staging / f"{DOC2}.x").mkdir()
    assert archive.sweep_staging() == 1  # only DOC2's directory
    assert journal.exists()
    assert (paths.staging / f"{DOC}.x").exists()
    with pytest.raises(RefusedError):  # nothing more happens to DOC until someone looks
        archive.tombstone(DOC)
    assert tree(paths.library) == V1


def _journal_doc(paths: AccountPaths, src: str, dst: str, kind: str = "place") -> bytes:
    return json.dumps(
        {"v": 1, "op": "commit", "uuid": DOC, "moves": [[src, dst, kind]], "rmdirs": []}
    ).encode()


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (f"library/{DOC}.content", f"blobs/{'a' * 64}"),  # into blobs
        (f"blobs/{'a' * 64}", f"library/{DOC}.content"),  # out of blobs
        (f"library/{DOC}.content", "../../../outside"),
        (f"library/{DOC}.content", "staging/x"),  # out of the protected trees
        (f"library/{DOC}.content", f"library/{DOC}.content/.."),
        (f"/etc/{DOC}", f"library/{DOC}.content"),
        (f"library/{DOC}.content", f"library/..\u0000/{DOC}"),
    ],
)
def test_journal_paths_are_validated(
    archive: Archive, paths: AccountPaths, src: str, dst: str
) -> None:
    commit(archive, V1, 1)
    before = census(paths)
    _plant_journal(paths, DOC, _journal_doc(paths, src, dst))
    archive.sweep_staging()
    assert tree(paths.library) == V1
    assert census(paths) == before
    with pytest.raises(RefusedError):
        archive.tombstone(DOC)


def test_journal_for_another_document_is_refused(archive: Archive, paths: AccountPaths) -> None:
    commit(archive, V1, 1)
    _plant_journal(
        paths, DOC2, _journal_doc(paths, f"library/{DOC}.content", f"tombstones/{DOC}.content")
    )
    archive.sweep_staging()
    assert tree(paths.library) == V1


# ---- guards ---------------------------------------------------------------------------------


def test_discard_refuses_anything_outside_staging(archive: Archive, paths: AccountPaths) -> None:
    commit(archive, V1, 1)
    for target in (
        paths.library / f"{DOC}.content",
        paths.blobs / sha(V1[f"{DOC}.content"]),
        paths.tombstones,
        paths.staging,
        paths.staging / "..",
        paths.staging / ".." / "library",
    ):
        with pytest.raises(RefusedError):
            archive._discard(target)
    assert tree(paths.library) == V1


def test_discard_does_not_follow_a_symlink_out_of_staging(
    archive: Archive, paths: AccountPaths
) -> None:
    commit(archive, V1, 1)
    link = paths.staging / "link-to-library"
    link.symlink_to(paths.library)
    archive._discard(link)  # removes the link itself
    assert not os.path.lexists(link)
    assert tree(paths.library) == V1


def test_init_refuses_a_symlinked_archive_directory(paths: AccountPaths, tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    paths.root.mkdir(parents=True)
    paths.library.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(RefusedError):
        Archive(paths)


def test_init_refuses_directories_on_different_filesystems(
    paths: AccountPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_stat = os.stat
    blobs = os.path.realpath(paths.blobs)

    def fake_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        result = real_stat(path, *args, **kwargs)
        if os.fspath(path) == blobs:
            return SimpleNamespace(st_dev=result.st_dev + 1, st_mode=result.st_mode)
        return result

    monkeypatch.setattr(os, "stat", fake_stat)
    with pytest.raises(RefusedError):
        Archive(paths)


# ---- the checkers themselves ----------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        ("unlink", "library", None, False),
        ("rmtree", "blobs", None, False),
        ("rmtree", "tombstones", None, False),
        ("rmdir", "library", None, False),
        ("rename", "library", "staging", False),  # content leaving the protected trees
        ("rename", "staging", "blobs", True),  # replacing a blob
    ],
)
def test_the_recorder_check_catches_violations(
    paths: AccountPaths, call: tuple[str, str, str | None, bool]
) -> None:
    op, where, other, existed = call
    rec = RecordingFs()
    base = os.path.realpath(paths.root)
    rec.calls.append(
        (
            op,
            os.path.join(base, where, "x"),
            None if other is None else os.path.join(base, other, "y"),
            existed,
        )
    )
    with pytest.raises(AssertionError):
        assert_never_deleted(rec, paths)


def test_the_os_spy_catches_violations(paths: AccountPaths, os_spy: list[Any]) -> None:
    paths.library.mkdir(parents=True)
    paths.staging.mkdir()
    victim = paths.library / "victim"
    victim.write_bytes(b"x")
    (paths.staging / "new").write_bytes(b"y")
    os.replace(paths.staging / "new", victim)
    os.rename(victim, paths.staging / "out")
    (paths.library / "victim2").write_bytes(b"x")
    os.unlink(paths.library / "victim2")
    kinds = [(v[0], v[1]) if len(v) == 3 else (v[0], "deletes") for v in os_spy]
    assert kinds == [("replace", "replaces"), ("rename", "leaves"), ("unlink", "deletes")]
    os_spy.clear()


# ---- real SIGKILL ---------------------------------------------------------------------------

CHILD = textwrap.dedent(
    """
    import ast, hashlib, io, os, signal, sys
    from pathlib import Path
    from scriptorium.library import archive as am
    from scriptorium.users.store import account_paths

    paths = account_paths(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
    mode, doc = sys.argv[4], sys.argv[5]
    v1, v2 = ast.literal_eval(sys.argv[6]), ast.literal_eval(sys.argv[7])

    class KillingFs(am._Fs):
        holds = 0
        def rename(self, src, dst):
            if mode == "swap" and am.HOLDING_DIR in str(dst):
                KillingFs.holds += 1
                if KillingFs.holds == 2:
                    os.kill(os.getpid(), signal.SIGKILL)
            super().rename(src, dst)

    am._fs = KillingFs()
    archive = am.Archive(paths)

    def put(data):
        digest = hashlib.sha256(data).hexdigest()
        archive.put_blob(digest, io.BytesIO(data))
        return digest

    if mode == "download":
        class Dying(io.RawIOBase):
            calls = 0
            def readable(self):
                return True
            def read(self, size=-1):
                Dying.calls += 1
                if Dying.calls > 1:
                    os.kill(os.getpid(), signal.SIGKILL)
                return b"x" * 1000
        archive.put_blob("ab" * 32, Dying())
    else:
        for n, files in ((1, v1), (2, v2)):
            mapping = {name: put(data) for name, data in files.items()}
            with archive.stage(doc, f"job{n}") as staged:
                archive.commit(staged, mapping, n, "f" * 64)
    sys.exit(3)
    """
)


def _run_child(tmp_path: Path, mode: str) -> None:
    script_dir = tmp_path / "child"
    script_dir.mkdir()
    script = script_dir / "child.py"
    script.write_text(CHILD)
    args = [str(tmp_path / "data"), UID, ACCT, mode, DOC, repr(V1), repr(V2)]
    proc = subprocess.run(  # noqa: S603 - fixed interpreter and script, test-owned arguments
        [sys.executable, "-I", str(script), *args], capture_output=True, timeout=60, check=False
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr.decode()[-2000:]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigkill_mid_download_leaves_no_partial_blob(
    tmp_path: Path, paths: AccountPaths, rec: RecordingFs, os_spy: list[Any]
) -> None:
    _run_child(tmp_path, "download")
    assert os.listdir(paths.blobs) == []
    assert len(os.listdir(paths.staging / ".incoming")) == 1  # the partial download
    archive = Archive(paths)
    assert archive.sweep_staging() == 1
    assert os.listdir(paths.blobs) == []
    assert os.listdir(paths.staging / ".incoming") == []
    assert_never_deleted(rec, paths)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigkill_mid_swap_is_recovered(
    tmp_path: Path, paths: AccountPaths, rec: RecordingFs, os_spy: list[Any]
) -> None:
    _run_child(tmp_path, "swap")
    assert tree(paths.library) not in (V1, V2)
    archive = Archive(paths)
    archive.sweep_staging()
    assert tree(paths.library) == V2
    assert holding(paths, "2") == V1
    assert sorted(os.listdir(paths.staging)) == [".incoming", ".journal"]
    assert_never_deleted(rec, paths)
