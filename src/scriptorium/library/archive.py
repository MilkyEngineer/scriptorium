"""The on-disk archive of one reMarkable account: blobs, library view, tombstones (§6, §7).

Layout under the account root (``users/<uid>/accounts/<acct>/``):

- ``blobs/<sha256>``: content-addressed, verified on the way in, read-only, kept forever
  (only ``admin_gc_blobs`` may remove one).
- ``library/``: the current view in xochitl layout, built from hardlinks to blobs (a copy
  where a hardlink is impossible). ``library/.holding/<uuid>/<label>/`` keeps every entry a
  commit replaced (label = the generation that replaced it) or a superseded tombstone
  (label ``tombstone``); a label already taken gets ``.1``, ``.2``, ...
- ``tombstones/<uuid>/``: documents that left the cloud, same layout; never purged here.
- ``staging/``: ``<uuid>.<job>/`` working directories, ``.incoming/`` partial blobs and
  ``.journal/`` move plans. The only place this module ever deletes anything.

**Never lose a note.** Nothing under ``library/``, ``blobs/`` or ``tombstones/`` is ever
unlinked, removed or overwritten: entries only move, with renames that refuse to replace an
existing destination. Every filesystem mutation goes through the :class:`_Fs` facade, so a
test can prove it, and the one deleting helper (:meth:`Archive._discard`) refuses any path
outside ``staging/``.

**Crash atomicity.** A document is several top-level entries (``<uuid>.metadata``, ...,
``<uuid>/``), so no single rename can swap it. Each multi-entry change (commit, tombstone,
un-tombstone) is planned first, the plan is written to ``staging/.journal/`` atomically, and
only then are the renames made; a plan left behind by a crash or an I/O error is rolled
forward (idempotently) before the next change to that document and by
:meth:`Archive.sweep_staging` at worker start. Readers can briefly see a document missing
between its old entries leaving and the new ones arriving, never a mix of two versions after
recovery.

One writer per account: the worker. Methods are thread-safe within one :class:`Archive`.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Final, Literal

from scriptorium.errors import NotFoundError, RefusedError
from scriptorium.library.layout import (
    DOC_EXTENSIONS,
    belongs_to,
    doc_entries,
    is_safe_name,
    is_sha256,
    is_uuid,
    resolve_inside,
)
from scriptorium.logging import log_event
from scriptorium.users.store import AccountPaths

__all__ = ["HOLDING_DIR", "Archive", "Committed", "Staging"]

log = logging.getLogger(__name__)

#: Under ``library/``: entries replaced by a commit or superseded tombstones, never deleted.
HOLDING_DIR: Final = ".holding"
_INCOMING: Final = ".incoming"
_JOURNAL: Final = ".journal"
_TREE: Final = "tree"
_JOURNAL_VERSION: Final = 1
_JOB_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_JOURNAL_NAME_RE: Final = re.compile(r"([0-9a-f-]{36})\.[0-9]{20}\.[0-9a-f]{8}\.json")
_CHUNK: Final = 1 << 20
_MAX_JOURNAL_BYTES: Final = 4 * 1024 * 1024
_MAX_HOLDING_SUFFIX: Final = 10_000
#: link() failures that mean "this filesystem will not hardlink here", so copy instead.
_NO_LINK_ERRNOS: Final = frozenset(
    {errno.EXDEV, errno.EMLINK, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP}
)
_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)

MoveKind = Literal["hold", "place"]


class _Fs:
    """Every filesystem mutation the archive makes, in one replaceable object.

    Tests substitute a recording subclass to assert which operation touched which path.
    :meth:`rename` never replaces an existing destination.
    """

    def mkdir(self, path: Path, *, exclusive: bool = False) -> None:
        if exclusive:
            os.mkdir(path, 0o700)
        else:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)

    def rename(self, src: Path, dst: Path) -> None:
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "rename destination exists")
        os.rename(src, dst)

    def link(self, src: Path, dst: Path) -> None:
        os.link(src, dst)

    def create(self, path: Path, mode: int = 0o600) -> int:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, mode)

    def chmod(self, path: Path, mode: int) -> None:
        os.chmod(path, mode)

    def fsync(self, fd: int) -> None:
        os.fsync(fd)

    def fsync_dir(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def unlink(self, path: Path) -> None:
        os.unlink(path)

    def rmtree(self, path: Path) -> None:
        shutil.rmtree(path)

    def rmdir(self, path: Path) -> None:
        os.rmdir(path)  # only ever succeeds on an empty directory


_fs = _Fs()


@dataclass(frozen=True)
class Committed:
    """The result of :meth:`Archive.commit`."""

    uuid: str
    generation: int
    hash_root: str
    files: Mapping[str, Path]  # xochitl-relative name -> path in library/
    held: Path | None  # where the replaced entries went, or None for a new document


@dataclass(frozen=True)
class _Move:
    src: Path
    dst: Path
    # "hold": dst is in a directory claimed for this plan, so dst existing means done.
    # "place": dst was free when planned, so dst existing while src still exists is a conflict.
    kind: MoveKind


class Staging:
    """``staging/<uuid>.<job>/``: where one document's commit is prepared.

    Use as a context manager. On exit, success or error, the directory is removed, unless a
    commit's swap was interrupted, in which case it stays for recovery.
    """

    def __init__(self, archive: Archive, uuid: str, job: str, path: Path) -> None:
        self._archive = archive
        self.uuid = uuid
        self.job = job
        self.path = path
        self.committed = False
        self._pending: Path | None = None  # journal of an interrupted swap

    @property
    def name(self) -> str:
        return self.path.name

    def __enter__(self) -> Staging:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release the directory (idempotent); the context manager calls this."""
        self._archive._release(self)


class Archive:
    """Blobs, the library view and tombstones of one account (plan §3.2)."""

    def __init__(self, paths: AccountPaths) -> None:
        self._paths = paths
        self._fs = _fs
        self._lock = threading.RLock()
        self._active: set[str] = set()
        self._inflight: set[str] = set()
        for directory in (paths.root, paths.blobs, paths.library, paths.tombstones):
            self._fs.mkdir(directory)
        self._fs.mkdir(paths.staging)
        self._root = resolve_inside(paths.root, paths.root)
        self._blobs = self._subdir(paths.blobs)
        self._lib = self._subdir(paths.library)
        self._tomb = self._subdir(paths.tombstones)
        self._staging = self._subdir(paths.staging)
        devices = {os.stat(d).st_dev for d in (self._blobs, self._lib, self._tomb, self._staging)}
        if len(devices) != 1:
            raise RefusedError(
                "the archive directories are on different filesystems",
                hint="blobs, library, tombstones and staging must share one filesystem",
            )

    def _subdir(self, path: Path) -> Path:
        """A direct, real (non-symlink) subdirectory of the account root, resolved."""
        st = os.lstat(path)
        real = resolve_inside(self._root, path)
        if not stat.S_ISDIR(st.st_mode) or real != self._root / path.name:
            raise RefusedError("an archive directory is not a plain directory in the account")
        return real

    # ---- staging and blobs ---------------------------------------------------------------

    def stage(self, uuid: str, job: str) -> Staging:
        """Create ``staging/<uuid>.<job>/`` and return it (a context manager)."""
        _require_uuid(uuid)
        if not isinstance(job, str) or _JOB_RE.fullmatch(job) is None:
            raise RefusedError("job identifier is malformed")
        name = f"{uuid}.{job}"
        with self._lock:
            if name in self._active:
                raise RefusedError("this document is already being staged by this job")
            path = self._staging / name
            if os.path.lexists(path):  # left by a crash: settle any plan, then start clean
                self._recover(uuid)
                self._discard(path)
            self._fs.mkdir(path, exclusive=True)
            self._active.add(name)
        return Staging(self, uuid, job, path)

    def put_blob(self, sha256: str, stream: BinaryIO) -> Path:
        """Store the bytes of ``stream`` as ``blobs/<sha256>`` and return its path.

        The content is hashed while it is written to ``staging/.incoming/``; a mismatch is
        refused (:class:`RefusedError`) and nothing reaches ``blobs/``. The blob is fsynced,
        made read-only and linked in without replacing anything. Idempotent: if the blob is
        already present the stream is not read.
        """
        _require_sha(sha256)
        final = self._blobs / sha256
        if os.path.lexists(final):
            return self._existing_blob(sha256)
        incoming = self._staging / _INCOMING
        self._fs.mkdir(incoming)
        tmp_name = f"{sha256}.{secrets.token_hex(8)}.part"
        tmp = incoming / tmp_name
        with self._lock:
            self._inflight.add(tmp_name)
        try:
            digest = hashlib.sha256()
            fd = self._fs.create(tmp)
            with os.fdopen(fd, "wb") as fh:
                while chunk := stream.read(_CHUNK):
                    digest.update(chunk)
                    fh.write(chunk)
                fh.flush()
                self._fs.fsync(fh.fileno())
            if digest.hexdigest() != sha256:
                raise RefusedError("blob content does not match its hash")
            self._fs.chmod(tmp, 0o400)
            try:
                self._fs.link(tmp, final)
            except FileExistsError:
                pass  # a concurrent writer stored the same verified content first
            except OSError as exc:
                if exc.errno not in _NO_LINK_ERRNOS:
                    raise
                try:
                    self._fs.rename(tmp, final)
                except FileExistsError:
                    pass
            self._fs.fsync_dir(self._blobs)
        finally:
            if os.path.lexists(tmp):
                self._discard(tmp)
            with self._lock:
                self._inflight.discard(tmp_name)
        return self._existing_blob(sha256)

    def blob_path(self, sha256: str) -> Path:
        """``blobs/<sha256>`` (whether or not it exists), asserted inside ``blobs/``."""
        _require_sha(sha256)
        return resolve_inside(self._blobs, self._blobs / sha256)

    def _existing_blob(self, sha256: str) -> Path:
        path = self.blob_path(sha256)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            raise NotFoundError("blob is not in the archive") from None
        if not stat.S_ISREG(st.st_mode):
            raise RefusedError("blob is not a regular file")
        return path

    # ---- commit --------------------------------------------------------------------------

    def commit(
        self, staged: Staging, files: Mapping[str, str], generation: int, hash_root: str
    ) -> Committed:
        """Make ``files`` (xochitl-relative name -> blob sha256) the document's library view.

        The new tree is prepared in the staging directory from hardlinks to the blobs (copies
        where hardlinks fail), fsynced, and swapped in by a journalled sequence of renames:
        the previous entries of the document move to
        ``library/.holding/<uuid>/<generation>/``; nothing is unlinked. Every name must be an
        allowlisted name of ``staged.uuid`` and every blob must already be stored.
        """
        uuid = staged.uuid
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise RefusedError("generation must be a non-negative integer")
        _require_sha(hash_root, what="document hash")
        if not files:
            raise RefusedError("a commit needs at least one file")
        blobs: dict[str, Path] = {}
        for name, sha in files.items():
            if not belongs_to(name, uuid):
                raise RefusedError("file name is not on the allowlist for this document")
            _require_sha(sha)
            blobs[name] = self._existing_blob(sha)
        with self._lock:
            self._check_staging(staged)
            self._recover(uuid)
            tree = staged.path / _TREE
            self._fs.mkdir(tree, exclusive=True)
            for name in sorted(blobs):
                dst = tree / name
                if dst.parent != tree:
                    self._fs.mkdir(dst.parent)
                self._place_blob(blobs[name], dst)
            self._fsync_tree(tree)

            old = self._present(self._lib, uuid)
            moves: list[_Move] = []
            held = None
            if old:
                held = self._claim_holding(uuid, str(generation))
                moves += [_Move(self._lib / e, held / e, "hold") for e in old]
            top_level = sorted({name.split("/", 1)[0] for name in files})
            moves += [_Move(tree / e, self._lib / e, "place") for e in top_level]
            # Set before the journal can exist: from then on the tree must outlive a failure.
            journal = staged._pending = self._journal_path_for(uuid)
            self._write_journal(
                journal, uuid, "commit", moves, [], generation=generation, hash_root=hash_root
            )
            self._apply(journal, moves, [])
            staged._pending = None
            staged.committed = True
        return Committed(
            uuid=uuid,
            generation=generation,
            hash_root=hash_root,
            files={name: self._lib / name for name in sorted(files)},
            held=held,
        )

    def _place_blob(self, blob: Path, dst: Path) -> None:
        try:
            self._fs.link(blob, dst)
            return
        except OSError as exc:
            if exc.errno not in _NO_LINK_ERRNOS:
                raise
        fd = self._fs.create(dst)
        with os.fdopen(fd, "wb") as out, open(blob, "rb") as src:
            shutil.copyfileobj(src, out, _CHUNK)
            out.flush()
            self._fs.fsync(out.fileno())
        self._fs.chmod(dst, 0o400)

    def _fsync_tree(self, tree: Path) -> None:
        for directory, _dirs, _files in os.walk(tree):
            self._fs.fsync_dir(Path(directory))

    # ---- tombstones ----------------------------------------------------------------------

    def tombstone(self, uuid: str) -> None:
        """Move the document's library entries to ``tombstones/<uuid>/``; never unlinks.

        A no-op if the library holds nothing for ``uuid``. If ``tombstones/<uuid>/`` already
        holds an older tombstone, that one moves to ``library/.holding/<uuid>/tombstone/``.
        """
        _require_uuid(uuid)
        with self._lock:
            self._recover(uuid)
            old = self._present(self._lib, uuid)
            if not old:
                return
            tdir = self._tomb / uuid
            moves: list[_Move] = []
            if os.path.lexists(tdir):
                self._require_plain_dir(tdir, self._tomb)
                existing = self._present(tdir, uuid)
                if existing:
                    held = self._claim_holding(uuid, "tombstone")
                    moves += [_Move(tdir / e, held / e, "hold") for e in existing]
            else:
                self._fs.mkdir(tdir)
            moves += [_Move(self._lib / e, tdir / e, "place") for e in old]
            journal = self._journal_path_for(uuid)
            self._write_journal(journal, uuid, "tombstone", moves, [])
            self._apply(journal, moves, [])

    def untombstone(self, uuid: str) -> None:
        """Move ``tombstones/<uuid>/`` back into the library; never unlinks.

        A no-op if there is no tombstone. If the library already holds the document (a newer
        version was committed meanwhile), the tombstone moves to
        ``library/.holding/<uuid>/tombstone/`` instead, so two versions never mix.
        """
        _require_uuid(uuid)
        with self._lock:
            self._recover(uuid)
            tdir = self._tomb / uuid
            if not os.path.lexists(tdir):
                return
            self._require_plain_dir(tdir, self._tomb)
            entries = self._present(tdir, uuid)
            if not entries:
                self._rmdir_if_empty(tdir)
                return
            if self._present(self._lib, uuid):
                held = self._claim_holding(uuid, "tombstone")
                moves = [_Move(tdir / e, held / e, "hold") for e in entries]
            else:
                moves = [_Move(tdir / e, self._lib / e, "place") for e in entries]
            journal = self._journal_path_for(uuid)
            self._write_journal(journal, uuid, "untombstone", moves, [tdir])
            self._apply(journal, moves, [tdir])

    # ---- reading -------------------------------------------------------------------------

    def current_files(self, uuid: str) -> Mapping[str, Path]:
        """The document's files in ``library/``: xochitl-relative name -> resolved path.

        Only allowlisted names are listed. A path that resolves outside ``library/`` (a
        planted symlink) raises :class:`~scriptorium.errors.PathEscapeError`.
        """
        _require_uuid(uuid)
        out: dict[str, Path] = {}
        for ext in DOC_EXTENSIONS:
            name = f"{uuid}.{ext}"
            path = self._lib / name
            if os.path.lexists(path):
                real = resolve_inside(self._lib, path)
                if real.is_file():
                    out[name] = real
        page_dir = self._lib / uuid
        if os.path.lexists(page_dir):
            real_dir = resolve_inside(self._lib, page_dir)
            if real_dir.is_dir():
                for entry in sorted(os.listdir(real_dir)):
                    name = f"{uuid}/{entry}"
                    if not is_safe_name(name):
                        continue
                    real = resolve_inside(self._lib, real_dir / entry)
                    if real.is_file():
                        out[name] = real
        return out

    # ---- staging housekeeping ------------------------------------------------------------

    def sweep_staging(self) -> int:
        """Finish interrupted changes, then delete stale staging data; returns entries removed.

        Run at worker start, before any sync. Every journalled change is rolled forward
        first; a journal that cannot be applied is kept, together with that document's
        staging directories, and logged. Only ``staging/`` is ever deleted from, and never a
        directory or partial blob that this instance is using.
        """
        with self._lock:
            stuck = self._recover_all()
            removed = 0
            for name in sorted(os.listdir(self._staging)):
                path = self._staging / name
                if name == _JOURNAL:
                    for jname in sorted(os.listdir(path)):
                        if jname.startswith("."):  # a journal write that never completed
                            self._discard(path / jname)
                            removed += 1
                elif name == _INCOMING:
                    for part in sorted(os.listdir(path)):
                        if part not in self._inflight:
                            self._discard(path / part)
                            removed += 1
                elif name not in self._active and name[:36] not in stuck:
                    self._discard(path)
                    removed += 1
        if removed:
            log_event(log, "archive_staging_swept", count=removed)
        return removed

    # ---- internals: staging --------------------------------------------------------------

    def _check_staging(self, staged: Staging) -> None:
        if staged._archive is not self or staged.name not in self._active:
            raise RefusedError("staging directory is not open in this archive")
        if staged.committed:
            raise RefusedError("this staging directory was already committed")

    def _release(self, staged: Staging) -> None:
        with self._lock:
            if staged._archive is not self or staged.name not in self._active:
                return
            self._active.discard(staged.name)
            if staged._pending is not None and os.path.lexists(staged._pending):
                return  # an interrupted swap still needs this tree; recovery will finish it
            if os.path.lexists(staged.path):
                self._discard(staged.path)

    def _discard(self, path: Path) -> None:
        """Delete ``path`` (file or tree). Refuses anything not strictly inside ``staging/``."""
        parent = resolve_inside(self._staging, path.parent)
        if path.name in ("", ".", "..") or parent / path.name == self._staging:
            raise RefusedError("refusing to delete outside staging")
        target = parent / path.name
        st = os.lstat(target)
        if stat.S_ISDIR(st.st_mode):
            self._fs.rmtree(target)
        else:
            self._fs.unlink(target)

    # ---- internals: library entries ------------------------------------------------------

    @staticmethod
    def _present(directory: Path, uuid: str) -> list[str]:
        """The entries of document ``uuid`` that exist (as anything) in ``directory``."""
        return [e for e in doc_entries(uuid) if os.path.lexists(directory / e)]

    def _require_plain_dir(self, path: Path, root: Path) -> None:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or resolve_inside(root, path) != path:
            raise RefusedError("archive entry is not a plain directory")

    def _claim_holding(self, uuid: str, label: str) -> Path:
        """Create and return a fresh ``library/.holding/<uuid>/<label>[.n]/``."""
        base = self._lib / HOLDING_DIR / uuid
        resolve_inside(self._lib, base)  # before mkdir: a planted symlink must not be followed
        self._fs.mkdir(base)
        resolve_inside(self._lib, base)
        for n in range(_MAX_HOLDING_SUFFIX):
            candidate = base / (label if n == 0 else f"{label}.{n}")
            try:
                self._fs.mkdir(candidate, exclusive=True)
            except FileExistsError:
                continue
            self._fs.fsync_dir(base)
            return candidate
        raise RefusedError("too many held versions of one document")

    # ---- internals: journal --------------------------------------------------------------

    def _journal_path_for(self, uuid: str) -> Path:
        name = f"{uuid}.{time.time_ns():020d}.{secrets.token_hex(4)}.json"
        return self._staging / _JOURNAL / name

    def _write_journal(
        self,
        path: Path,
        uuid: str,
        op: str,
        moves: Sequence[_Move],
        rmdirs: Sequence[Path],
        **extra: Any,
    ) -> None:
        """Write the plan to ``path`` atomically (temp file, fsync, rename, dir fsync)."""
        directory = path.parent
        self._fs.mkdir(directory)
        doc = {
            "v": _JOURNAL_VERSION,
            "op": op,
            "uuid": uuid,
            "moves": [[self._rel(m.src), self._rel(m.dst), m.kind] for m in moves],
            "rmdirs": [self._rel(d) for d in rmdirs],
            **extra,
        }
        data = json.dumps(doc, sort_keys=True).encode()
        tmp = directory / f".{path.name}.tmp"
        fd = self._fs.create(tmp)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            self._fs.fsync(fh.fileno())
        self._fs.rename(tmp, path)
        self._fs.fsync_dir(directory)

    def _rel(self, path: Path) -> str:
        return path.relative_to(self._root).as_posix()

    def _apply(self, journal: Path, moves: Sequence[_Move], rmdirs: Sequence[Path]) -> None:
        """Make every move of a plan (idempotently), then retire the journal."""
        touched: set[Path] = set()
        for move in moves:
            src_there, dst_there = os.path.lexists(move.src), os.path.lexists(move.dst)
            if dst_there and (move.kind == "hold" or not src_there):
                continue  # already done
            if dst_there:
                raise RefusedError("archive change conflicts with what is on disk")
            if not src_there:
                log_event(log, "archive_journal_source_missing", level=logging.WARNING)
                continue
            self._fs.mkdir(move.dst.parent)
            self._fs.rename(move.src, move.dst)
            touched.update((move.src.parent, move.dst.parent))
        for directory in rmdirs:
            if self._rmdir_if_empty(directory):
                touched.add(directory.parent)
        for directory in sorted(touched):
            if os.path.isdir(directory):
                self._fs.fsync_dir(directory)
        self._discard(journal)
        self._fs.fsync_dir(journal.parent)

    def _rmdir_if_empty(self, directory: Path) -> bool:
        """Remove an empty directory; anything in it (or it being gone) leaves it alone."""
        try:
            self._fs.rmdir(directory)
        except OSError:
            return False
        return True

    def _journals(self, uuid: str | None = None) -> list[tuple[Path, str]]:
        directory = self._staging / _JOURNAL
        try:
            names = sorted(os.listdir(directory))
        except FileNotFoundError:
            return []
        out = []
        for name in names:
            match = _JOURNAL_NAME_RE.fullmatch(name)
            if match is None or (uuid is not None and match.group(1) != uuid):
                continue
            out.append((directory / name, match.group(1)))
        return out

    def _recover(self, uuid: str) -> None:
        """Roll forward every pending plan of ``uuid``; raises if one cannot be applied."""
        for path, _ in self._journals(uuid):
            moves, rmdirs = self._load_journal(path, uuid)
            self._apply(path, moves, rmdirs)
            log_event(log, "archive_journal_recovered", count=len(moves))

    def _recover_all(self) -> set[str]:
        """Roll forward every pending plan; returns the UUIDs whose plan could not be applied."""
        stuck: set[str] = set()
        for path, uuid in self._journals():
            if uuid in stuck:
                continue
            try:
                moves, rmdirs = self._load_journal(path, uuid)
                self._apply(path, moves, rmdirs)
            except (RefusedError, OSError, ValueError):
                stuck.add(uuid)
                log_event(log, "archive_journal_stuck", level=logging.ERROR, exc_info=True)
                continue
            log_event(log, "archive_journal_recovered", count=len(moves))
        return stuck

    def _load_journal(self, path: Path, uuid: str) -> tuple[list[_Move], list[Path]]:
        """Parse and validate a journal: every path must stay where its role allows."""
        with open(path, "rb") as fh:
            raw = fh.read(_MAX_JOURNAL_BYTES + 1)
        try:
            doc = json.loads(raw)
        except ValueError:
            raise RefusedError("archive journal is unreadable") from None
        if (
            len(raw) > _MAX_JOURNAL_BYTES
            or not isinstance(doc, dict)
            or doc.get("v") != _JOURNAL_VERSION
            or doc.get("uuid") != uuid
            or not isinstance(doc.get("moves"), list)
            or not isinstance(doc.get("rmdirs"), list)
        ):
            raise RefusedError("archive journal is invalid")
        moves = []
        for item in doc["moves"]:
            if not (isinstance(item, list) and len(item) == 3 and item[2] in ("hold", "place")):
                raise RefusedError("archive journal is invalid")
            src = self._journal_path(item[0], (self._lib, self._tomb, self._staging))
            dst = self._journal_path(item[1], (self._lib, self._tomb))
            moves.append(_Move(src, dst, item[2]))
        rmdirs = [self._journal_path(d, (self._tomb,)) for d in doc["rmdirs"]]
        return moves, rmdirs

    def _journal_path(self, rel: object, allowed: Sequence[Path]) -> Path:
        if not isinstance(rel, str) or not rel or rel.startswith("/") or "\x00" in rel:
            raise RefusedError("archive journal is invalid")
        path = self._root / rel
        if path.name in ("", ".", "..") or ".." in Path(rel).parts:
            raise RefusedError("archive journal is invalid")
        parent = resolve_inside(self._root, path.parent)
        if not any(parent == a or parent.is_relative_to(a) for a in allowed):
            raise RefusedError("archive journal is invalid")
        return parent / path.name


def _require_uuid(uuid: str) -> None:
    if not is_uuid(uuid):
        raise RefusedError("document identifier is not on the allowlist")


def _require_sha(value: str, *, what: str = "blob hash") -> None:
    if not is_sha256(value):
        raise RefusedError(f"{what} is not a lowercase hex SHA-256")
