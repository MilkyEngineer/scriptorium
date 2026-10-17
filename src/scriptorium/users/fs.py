"""Refuse to run on a data directory that cannot hold SQLite WAL safely (Architecture §6, S5).

SQLite in WAL mode needs three things from the filesystem under ``DATA_DIR``: POSIX advisory
locks that are honoured across processes (the server and the worker are two containers sharing
one directory), a ``-shm`` file that every process can map coherently, and ``fsync`` that means
what it says. Local disk filesystems provide all three. FUSE drivers, network filesystems and
VM host shares provide some of them some of the time, and the failure mode is silent corruption
of an index, so they are refused outright rather than probed and hoped for.

Two independent checks run:

1. **Filesystem type**, from ``/proc/self/mountinfo`` on Linux (the mount with the longest
   mount point that contains ``path.resolve()``). Only types on :data:`ACCEPTED_FSTYPES` pass;
   anything else, known-bad or merely unknown, is refused (default deny). On platforms without
   ``/proc/self/mountinfo`` (macOS development machines) the type is not determined:
   :attr:`FilesystemReport.fstype` and :attr:`FilesystemReport.is_local` are ``None`` and no
   problem is recorded, so the functional probes alone decide. A Linux host whose mountinfo
   cannot be read is a problem, because that is where the check matters.
2. **Functional probes**, always: the directory is writable; an exclusive ``flock`` and an
   exclusive POSIX record lock (``lockf``, the kind SQLite uses) held here make a non-blocking
   attempt from a *child process* fail; a WAL database written by one connection is readable by a
   second. These catch a mislabelled mount, and they are what the S5 FUSE check exercises.

Decisions the Architecture leaves open, taken here:

- ``fuseblk`` (ntfs-3g, exfat-fuse) is refused with the rest of FUSE even though it sits on a
  local disk: the userspace driver decides lock, shared-mmap and ``fsync`` behaviour, ntfs-3g
  does not implement POSIX locks itself (the kernel fakes them locally), and neither filesystem
  stores POSIX ownership.
- ``virtiofs`` is refused. It can honour POSIX locks and keep ``-shm`` coherent, but only with
  the right daemon flags (lock support on, DAX off) that the guest cannot see; it is how VM-based
  Docker runtimes expose host directories, which is a development setup. Use a named volume.
- ``overlay`` is refused: it is the container's own root filesystem, so ``DATA_DIR`` was not
  mounted at all and the data would vanish with the container.
- ``tmpfs``/``ramfs`` are refused unless ``allow_volatile=True`` (tests only).

Messages contain the path and the filesystem type, never file contents.
"""

from __future__ import annotations

import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__all__ = [
    "ACCEPTED_FSTYPES",
    "FilesystemReport",
    "MountEntry",
    "UnsafeDataDir",
    "check_data_dir",
    "classify_fstype",
    "find_mount",
    "parse_mountinfo",
    "require_local_data_dir",
]

MOUNTINFO_PATH = Path("/proc/self/mountinfo")

#: Local disk filesystems with working POSIX locks, shared mmap and fsync.
ACCEPTED_FSTYPES: frozenset[str] = frozenset({"ext4", "xfs", "btrfs", "zfs", "f2fs", "bcachefs"})

_VOLATILE_FSTYPES: frozenset[str] = frozenset({"tmpfs", "ramfs"})

_NETWORK = (
    "a network filesystem: SQLite WAL needs a shared-memory file that every process maps "
    "coherently, which no network filesystem provides, and its locks are only as good as the "
    "server's lock manager"
)
_HOST_SHARE = (
    "a host directory shared into a VM: lock support and shared-memory coherency depend on "
    "daemon options the container cannot see"
)

#: Known-bad types with the reason given to the operator. Anything not accepted is refused anyway.
_REFUSED_FSTYPES: dict[str, str] = {
    "nfs": _NETWORK,
    "nfs4": _NETWORK,
    "cifs": _NETWORK,
    "smb3": _NETWORK,
    "smbfs": _NETWORK,
    "afs": _NETWORK,
    "ceph": _NETWORK,
    "glusterfs": _NETWORK,
    "lustre": _NETWORK,
    "beegfs": _NETWORK,
    "9p": _HOST_SHARE,
    "drvfs": _HOST_SHARE,
    "virtiofs": _HOST_SHARE,
    "fakeowner": _HOST_SHARE,
    "overlay": (
        "the container's own root filesystem: no volume is mounted at the data directory, so "
        "everything written would be lost with the container"
    ),
}

_FUSE_REASON = (
    "a FUSE filesystem (userspace driver: union mounts, sshfs, rclone, ntfs-3g and similar): "
    "SQLite WAL needs POSIX locks honoured across processes, a coherently shared-memory-mapped "
    "-shm file and a real fsync, and a FUSE driver guarantees none of them"
)
_VOLATILE_REASON = "a RAM-backed filesystem: everything written disappears on restart"

_REMEDY = (
    "Mount a directory from a local ext4, xfs, btrfs or ZFS filesystem at the data directory "
    "instead: a bind mount of a path on the Docker host's own disk (for example a dedicated "
    "btrfs subvolume or ZFS dataset), or a Docker named volume. Do not bind-mount a path that "
    "goes through a FUSE, union or network layer."
)

_PROBE_PREFIX = ".scriptorium-fsprobe-"
_CHILD_TIMEOUT_S = 15.0

# Runs in a child interpreter: tries to take the locks the parent already holds. Prints one
# word per lock kind: "blocked" (correct), "acquired" (locks do not work across processes),
# or "error:<errno>".
_CHILD_LOCK_PROBE = """
import errno, fcntl, sys

def attempt(path, take):
    with open(path, "r+b") as handle:
        try:
            take(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return "blocked"
            return "error:" + str(exc.errno)
        return "acquired"

print(attempt(sys.argv[1], fcntl.flock), attempt(sys.argv[2], fcntl.lockf))
"""


class UnsafeDataDir(RuntimeError):  # noqa: N818 - the name is the contract
    """The data directory cannot be trusted with SQLite WAL; the message says why and what to do."""

    def __init__(self, report: FilesystemReport) -> None:
        self.report = report
        lines = [f"Refusing to use {report.path} as the data directory:"]
        lines.extend(f"  - {problem}" for problem in report.problems)
        lines.append(_REMEDY)
        super().__init__("\n".join(lines))


@dataclass(frozen=True)
class MountEntry:
    """One line of ``/proc/self/mountinfo`` (proc(5)), with octal escapes decoded."""

    mount_id: int
    parent_id: int
    root: str
    mount_point: str
    options: tuple[str, ...]
    optional_fields: tuple[str, ...]
    fstype: str
    source: str
    super_options: tuple[str, ...]


@dataclass(frozen=True)
class FilesystemReport:
    """Outcome of :func:`check_data_dir`.

    ``fstype``/``mount_point`` are ``None`` when the type could not be determined. ``is_local``
    is ``True`` for an accepted type, ``False`` for a refused one, and ``None`` when the type was
    not determined (non-Linux development machines: the probes alone decide). The directory is
    safe exactly when ``problems`` is empty (:attr:`ok`).
    """

    path: Path
    fstype: str | None
    mount_point: Path | None
    is_local: bool | None
    writable: bool
    wal_ok: bool
    locks_ok: bool
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def _unescape(field_text: str) -> str:
    """Decode the kernel's octal escapes (``\\040`` space, ``\\011``, ``\\012``, ``\\134``)."""
    return _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), field_text)


def parse_mountinfo(text: str) -> list[MountEntry]:
    """Parse the text of ``/proc/<pid>/mountinfo``. Malformed lines are skipped.

    Layout per proc(5): ``id parent major:minor root mount-point options [optional...] - fstype
    source super-options``. The optional fields are variable in number, so the fstype is found
    after the ``-`` separator rather than at a fixed index.
    """
    entries: list[MountEntry] = []
    for line in text.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-", 6)
        except ValueError:
            continue
        if len(fields) < separator + 3:
            continue
        try:
            mount_id = int(fields[0])
            parent_id = int(fields[1])
        except ValueError:
            continue
        entries.append(
            MountEntry(
                mount_id=mount_id,
                parent_id=parent_id,
                root=_unescape(fields[3]),
                mount_point=_unescape(fields[4]),
                options=tuple(fields[5].split(",")),
                optional_fields=tuple(fields[6:separator]),
                fstype=fields[separator + 1],
                source=_unescape(fields[separator + 2]),
                super_options=tuple(fields[separator + 3].split(","))
                if len(fields) > separator + 3
                else (),
            )
        )
    return entries


def find_mount(entries: Iterable[MountEntry], path: PurePosixPath) -> MountEntry | None:
    """Return the mount that holds ``path`` (absolute, already resolved).

    The longest mount point that is ``path`` or a component-wise ancestor wins (``/data2`` is
    not under ``/data``); among equal mount points the later line wins, because it was mounted
    on top.
    """
    best: MountEntry | None = None
    best_depth = -1
    for entry in entries:
        mount_point = PurePosixPath(entry.mount_point)
        if path != mount_point and mount_point not in path.parents:
            continue
        depth = len(mount_point.parts)
        if depth >= best_depth:
            best, best_depth = entry, depth
    return best


def classify_fstype(fstype: str, *, allow_volatile: bool = False) -> str | None:
    """Return why ``fstype`` is refused, or ``None`` if it is accepted."""
    if fstype in ACCEPTED_FSTYPES:
        return None
    if fstype in _VOLATILE_FSTYPES:
        return None if allow_volatile else _VOLATILE_REASON
    if fstype.startswith("fuse"):
        return _FUSE_REASON
    known = _REFUSED_FSTYPES.get(fstype)
    if known is not None:
        return known
    accepted = ", ".join(sorted(ACCEPTED_FSTYPES))
    return f"not a filesystem Scriptorium has verified for SQLite WAL (accepted: {accepted})"


def check_data_dir(
    path: Path, *, allow_volatile: bool = False, mountinfo: str | None = None
) -> FilesystemReport:
    """Check that ``path`` can hold Scriptorium's SQLite databases; never raises for a bad answer.

    ``mountinfo`` overrides reading ``/proc/self/mountinfo`` (tests feed fixtures through it, on
    any platform). The probes create and remove their own uniquely named files in a temporary
    sub-directory of ``path`` and touch nothing else.
    """
    resolved = path.resolve()
    problems: list[str] = []

    fstype: str | None = None
    mount_point: Path | None = None
    is_local: bool | None = None
    text = mountinfo
    if text is None and platform.system() == "Linux":
        try:
            text = MOUNTINFO_PATH.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError as exc:
            problems.append(
                f"cannot read {MOUNTINFO_PATH} to determine the filesystem type of {resolved} "
                f"({exc.strerror or type(exc).__name__})"
            )
    if text is not None:
        mount = find_mount(parse_mountinfo(text), PurePosixPath(resolved))
        if mount is None:
            if not problems:
                problems.append(f"no mount found for {resolved}; cannot determine its filesystem")
        else:
            fstype = mount.fstype
            mount_point = Path(mount.mount_point)
            reason = classify_fstype(fstype, allow_volatile=allow_volatile)
            is_local = reason is None
            if reason is not None:
                problems.append(
                    f"{resolved} is on {fstype} (mounted at {mount_point}), which is {reason}"
                )

    writable, locks_ok, wal_ok = _run_probes(resolved, problems)
    return FilesystemReport(
        path=resolved,
        fstype=fstype,
        mount_point=mount_point,
        is_local=is_local,
        writable=writable,
        wal_ok=wal_ok,
        locks_ok=locks_ok,
        problems=problems,
    )


def require_local_data_dir(
    path: Path, *, allow_volatile: bool = False, mountinfo: str | None = None
) -> FilesystemReport:
    """Like :func:`check_data_dir`, but raise :class:`UnsafeDataDir` unless the report is clean."""
    report = check_data_dir(path, allow_volatile=allow_volatile, mountinfo=mountinfo)
    if not report.ok:
        raise UnsafeDataDir(report)
    return report


# --------------------------------------------------------------------------------------- probes


def _run_probes(directory: Path, problems: list[str]) -> tuple[bool, bool, bool]:
    """Run the writable, lock and WAL probes; append a problem per failure. Never raises."""
    if not directory.is_dir():
        problems.append(f"{directory} does not exist or is not a directory")
        return False, False, False
    try:
        scratch = Path(tempfile.mkdtemp(prefix=_PROBE_PREFIX, dir=directory))
    except OSError as exc:
        problems.append(f"{directory} is not writable ({_describe(exc)})")
        return False, False, False
    try:
        writable = _probe_writable(scratch, problems)
        locks_ok = writable and _probe_locks(scratch, problems)
        wal_ok = writable and _probe_wal(scratch, problems)
    finally:
        _remove_scratch(scratch)
    return writable, locks_ok, wal_ok


def _probe_writable(scratch: Path, problems: list[str]) -> bool:
    target = scratch / "write"
    try:
        with target.open("xb") as handle:
            handle.write(b"probe")
            handle.flush()
            os.fsync(handle.fileno())
        target.unlink()
    except OSError as exc:
        problems.append(f"{scratch.parent} is not writable ({_describe(exc)})")
        return False
    return True


def _probe_locks(scratch: Path, problems: list[str]) -> bool:
    """Hold ``flock`` and ``lockf`` exclusively here; a child process must fail to take either."""
    try:
        import fcntl
    except ImportError:
        problems.append("cannot test file locking on this platform (no fcntl module)")
        return False
    if not sys.executable:
        problems.append("cannot test file locking across processes (no Python executable path)")
        return False

    flock_path = scratch / "flock"
    lockf_path = scratch / "lockf"
    try:
        flock_path.write_bytes(b"\0")
        lockf_path.write_bytes(b"\0")
        with flock_path.open("r+b") as flock_file, lockf_path.open("r+b") as lockf_file:
            fcntl.flock(flock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.lockf(lockf_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Fixed program, interpreter path from sys.executable, arguments are our own
            # temporary file names: nothing user- or cloud-derived reaches the child.
            child = subprocess.run(  # noqa: S603
                [sys.executable, "-I", "-S", "-c", _CHILD_LOCK_PROBE, flock_path, lockf_path],
                capture_output=True,
                text=True,
                timeout=_CHILD_TIMEOUT_S,
                check=False,
            )
    except subprocess.TimeoutExpired:
        problems.append(
            f"the cross-process lock probe in {scratch.parent} did not finish within "
            f"{_CHILD_TIMEOUT_S:.0f} s"
        )
        return False
    except OSError as exc:
        problems.append(f"file locking does not work in {scratch.parent} ({_describe(exc)})")
        return False

    outcome = child.stdout.split()
    if child.returncode != 0 or len(outcome) != 2:
        problems.append(
            f"the cross-process lock probe in {scratch.parent} failed to run "
            f"(exit status {child.returncode})"
        )
        return False
    ok = True
    for kind, result in zip(("flock", "POSIX record lock (lockf)"), outcome, strict=True):
        if result == "blocked":
            continue
        ok = False
        if result == "acquired":
            problems.append(
                f"a {kind} held by one process in {scratch.parent} does not stop another "
                "process from taking it: locks do not work across processes here"
            )
        else:
            problems.append(f"a {kind} in {scratch.parent} could not be tested ({result})")
    return ok


def _probe_wal(scratch: Path, problems: list[str]) -> bool:
    """Write a row through a WAL database and read it back from a second connection."""
    db_path = scratch / "probe.sqlite"
    writer: sqlite3.Connection | None = None
    reader: sqlite3.Connection | None = None
    try:
        writer = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
        writer.execute("PRAGMA busy_timeout=5000")
        mode_row = writer.execute("PRAGMA journal_mode=WAL").fetchone()
        mode = str(mode_row[0]).lower() if mode_row else ""
        if mode != "wal":
            problems.append(
                f"SQLite cannot use WAL in {scratch.parent} (journal_mode reports {mode!r})"
            )
            return False
        writer.execute("PRAGMA synchronous=NORMAL")
        writer.execute("CREATE TABLE probe(id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO probe(id, value) VALUES (1, 42)")
        writer.execute("COMMIT")
        # The writer stays open, so the reader must go through the shared -shm wal-index.
        reader = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
        row = reader.execute("SELECT value FROM probe WHERE id = 1").fetchone()
        if row is None or row[0] != 42:
            problems.append(
                f"a row committed through SQLite WAL in {scratch.parent} was not visible to a "
                "second connection"
            )
            return False
    except sqlite3.Error as exc:
        problems.append(f"SQLite WAL does not work in {scratch.parent} ({exc})")
        return False
    finally:
        for connection in (reader, writer):
            if connection is not None:
                connection.close()
    return True


def _remove_scratch(scratch: Path) -> None:
    """Remove exactly the files the probes may have created, then the empty directory."""
    for name in (
        "write",
        "flock",
        "lockf",
        "probe.sqlite",
        "probe.sqlite-wal",
        "probe.sqlite-shm",
        "probe.sqlite-journal",
    ):
        try:
            (scratch / name).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        scratch.rmdir()
    except OSError:
        pass


def _describe(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__
