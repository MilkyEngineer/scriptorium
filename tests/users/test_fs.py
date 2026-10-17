from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from scriptorium.users import fs
from scriptorium.users.fs import (
    ACCEPTED_FSTYPES,
    UnsafeDataDir,
    check_data_dir,
    classify_fstype,
    find_mount,
    parse_mountinfo,
    require_local_data_dir,
)

# A container's view: overlay root, /data a bind-mounted btrfs subvolume, a read-only snapshot
# directory nested inside it, a FUSE union mount, an NFS mount, a mount point with escaped
# spaces, a sibling /data2 that must not match /data, and one malformed line.
MOUNTINFO = r"""22 1 0:21 / / rw,relatime - overlay overlay rw,lowerdir=/l,upperdir=/u,workdir=/w
1035 22 0:45 /@data /data rw,noatime master:12 - btrfs /dev/sdb1 rw,subvolid=260,subvol=/@data
1036 1035 0:45 /@snaps /data/snapshots ro,noatime master:12 - btrfs /dev/sdb1 rw,subvol=/@snaps
1040 22 0:60 / /mnt/union rw,nosuid,nodev shared:5 master:2 - fuse.mergerfs union rw,allow_other
1041 22 0:61 / /mnt/nfs rw,relatime shared:7 - nfs4 server.example:/export rw,vers=4.2
1042 22 8:1 /dir\040with\040space /mnt/my\040disk rw - ext4 /dev/sda1 rw
1043 22 0:62 / /data2 rw - tmpfs tmpfs rw
this line is not mountinfo
"""


def _entries() -> list[fs.MountEntry]:
    return parse_mountinfo(MOUNTINFO)


def _mount_for(path: str) -> fs.MountEntry:
    entry = find_mount(_entries(), PurePosixPath(path))
    assert entry is not None
    return entry


def test_parse_skips_malformed_lines_and_reads_fields() -> None:
    entries = _entries()
    assert [e.mount_id for e in entries] == [22, 1035, 1036, 1040, 1041, 1042, 1043]
    data = entries[1]
    assert data.parent_id == 22
    assert data.root == "/@data"
    assert data.mount_point == "/data"
    assert data.fstype == "btrfs"
    assert data.source == "/dev/sdb1"
    assert data.optional_fields == ("master:12",)
    assert "subvol=/@data" in data.super_options


def test_separator_found_after_variable_optional_fields() -> None:
    entries = {e.mount_point: e for e in _entries()}
    assert entries["/"].optional_fields == ()
    assert entries["/"].fstype == "overlay"
    assert entries["/mnt/union"].optional_fields == ("shared:5", "master:2")
    assert entries["/mnt/union"].fstype == "fuse.mergerfs"
    assert entries["/mnt/nfs"].fstype == "nfs4"


def test_escaped_spaces_are_decoded() -> None:
    entry = _mount_for("/mnt/my disk/notes")
    assert entry.mount_point == "/mnt/my disk"
    assert entry.root == "/dir with space"
    assert entry.fstype == "ext4"


def test_bind_mounted_btrfs_subvolume() -> None:
    entry = _mount_for("/data/users/abc/accounts/x/index.sqlite")
    assert (entry.mount_point, entry.fstype) == ("/data", "btrfs")
    assert _mount_for("/data").mount_id == 1035


def test_longest_prefix_wins_for_nested_mounts() -> None:
    assert _mount_for("/data/snapshots/2026-10-17T03-00-00Z").mount_id == 1036
    assert _mount_for("/data/snapshots-not-nested").mount_id == 1035


def test_prefix_match_is_by_component() -> None:
    assert _mount_for("/data2/x").fstype == "tmpfs"
    assert _mount_for("/etc/hostname").fstype == "overlay"


def test_later_mount_on_the_same_point_wins() -> None:
    stacked = MOUNTINFO + "1050 1035 0:70 / /data rw - nfs files.example:/x rw\n"
    entry = find_mount(parse_mountinfo(stacked), PurePosixPath("/data/x"))
    assert entry is not None
    assert entry.fstype == "nfs"


def test_no_mount_when_nothing_matches() -> None:
    assert find_mount([], PurePosixPath("/data")) is None


@pytest.mark.parametrize("fstype", sorted(ACCEPTED_FSTYPES))
def test_accepted_fstypes(fstype: str) -> None:
    assert classify_fstype(fstype) is None


@pytest.mark.parametrize(
    "fstype",
    [
        "fuse",
        "fuseblk",
        "fuse.mergerfs",
        "fuse.sshfs",
        "fuse.rclone",
        "fusectl",
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "9p",
        "drvfs",
        "afs",
        "ceph",
        "glusterfs",
        "virtiofs",
        "overlay",
        "tmpfs",
        "ramfs",
        "something-new",
    ],
)
def test_refused_fstypes(fstype: str) -> None:
    assert classify_fstype(fstype) is not None


def test_fuse_reason_names_fuse() -> None:
    reason = classify_fstype("fuse.anything")
    assert reason is not None
    assert "FUSE" in reason


def test_tmpfs_allowed_only_when_volatile_is_allowed() -> None:
    assert classify_fstype("tmpfs", allow_volatile=True) is None
    assert classify_fstype("ramfs", allow_volatile=True) is None
    assert classify_fstype("fuse", allow_volatile=True) is not None


def _single_mount(path: Path, fstype: str) -> str:
    point = str(path.resolve()).replace(" ", "\\040")
    return (
        "22 1 0:21 / / rw - overlay overlay rw\n"
        f"40 22 0:50 / {point} rw,relatime shared:1 - {fstype} src rw\n"
    )


def test_local_dir_passes(tmp_path: Path) -> None:
    report = require_local_data_dir(tmp_path, mountinfo=_single_mount(tmp_path, "btrfs"))
    assert report.ok
    assert report.fstype == "btrfs"
    assert report.mount_point == tmp_path.resolve()
    assert report.is_local is True
    assert report.writable and report.locks_ok and report.wal_ok


def test_fuse_dir_is_refused_with_operator_message(tmp_path: Path) -> None:
    mountinfo = _single_mount(tmp_path, "fuse.mergerfs")
    report = check_data_dir(tmp_path, mountinfo=mountinfo)
    assert report.is_local is False
    assert report.fstype == "fuse.mergerfs"
    # The probes still ran and passed: the type alone is enough to refuse.
    assert report.locks_ok and report.wal_ok
    assert len(report.problems) == 1
    with pytest.raises(UnsafeDataDir) as excinfo:
        require_local_data_dir(tmp_path, mountinfo=mountinfo)
    message = str(excinfo.value)
    assert str(tmp_path.resolve()) in message
    assert "fuse.mergerfs" in message
    assert "local ext4, xfs, btrfs or ZFS" in message
    assert excinfo.value.report.problems == report.problems


def test_nfs_dir_is_refused(tmp_path: Path) -> None:
    report = check_data_dir(tmp_path, mountinfo=_single_mount(tmp_path, "nfs4"))
    assert report.is_local is False
    assert "network filesystem" in report.problems[0]


def test_tmpfs_refused_unless_allowed(tmp_path: Path) -> None:
    mountinfo = _single_mount(tmp_path, "tmpfs")
    assert not check_data_dir(tmp_path, mountinfo=mountinfo).ok
    assert check_data_dir(tmp_path, mountinfo=mountinfo, allow_volatile=True).ok


def test_unmatched_mountinfo_is_a_problem(tmp_path: Path) -> None:
    report = check_data_dir(tmp_path, mountinfo="garbage\n")
    assert report.fstype is None
    assert not report.ok


def test_probes_pass_on_the_dev_machine_and_leave_nothing_behind(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("untouched")
    report = check_data_dir(tmp_path)
    assert report.writable
    assert report.locks_ok, report.problems
    assert report.wal_ok, report.problems
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt"]
    assert (tmp_path / "keep.txt").read_text() == "untouched"
    if fs.MOUNTINFO_PATH.exists():
        assert report.fstype is not None
    else:
        assert (report.fstype, report.is_local) == (None, None)


def test_missing_dir_is_a_problem(tmp_path: Path) -> None:
    report = check_data_dir(tmp_path / "absent", mountinfo=_single_mount(tmp_path, "ext4"))
    assert not report.writable
    assert any("does not exist" in p for p in report.problems)


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root ignores modes")
def test_read_only_dir_is_a_problem(tmp_path: Path) -> None:
    target = tmp_path / "ro"
    target.mkdir()
    target.chmod(0o555)
    try:
        report = check_data_dir(target, mountinfo=_single_mount(target, "ext4"))
    finally:
        target.chmod(0o755)
    assert not report.writable
    assert not report.locks_ok and not report.wal_ok
    assert any("not writable" in p for p in report.problems)


def _fcntl_or_skip() -> object:
    return pytest.importorskip("fcntl", reason="no fcntl on this platform")


def test_lock_probe_detects_locks_that_do_not_cross_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fcntl = _fcntl_or_skip()

    # Make the parent's locks no-ops, as on a filesystem whose locks are process-local: the child
    # then acquires both and the probe must say so.
    def _no_lock(_fd: int, _op: int) -> None:
        return None

    monkeypatch.setattr(fcntl, "flock", _no_lock)
    monkeypatch.setattr(fcntl, "lockf", _no_lock)
    problems: list[str] = []
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert fs._probe_locks(scratch, problems) is False
    assert len(problems) == 2
    assert all("across processes" in p for p in problems)


def test_lock_probe_passes_with_real_locks(tmp_path: Path) -> None:
    _fcntl_or_skip()
    problems: list[str] = []
    assert fs._probe_locks(tmp_path, problems) is True, problems
    assert problems == []
