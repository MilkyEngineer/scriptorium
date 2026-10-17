# Host snapshots (`SCRIPTORIUM_SNAPSHOT_STRATEGY=host`)

Point-in-time copies of `/data` are the Docker host's job (Architecture §10.1). A root cron job on
the host snapshots the filesystem behind `/data` and prunes old snapshots; the containers see
the snapshots through a **read-only** bind mount at `/data/snapshots` and only list and read
them. The containers never get `CAP_SYS_ADMIN`, the btrfs/zfs tools, or write access to a
snapshot.

| File | Filesystem | Snapshot command | Prune command |
| --- | --- | --- | --- |
| `snapshot-btrfs.sh` | btrfs subvolume | `btrfs subvolume snapshot -r` | `btrfs subvolume delete` |
| `snapshot-zfs.sh` | ZFS dataset | `zfs snapshot` | `zfs destroy` (never `-r`/`-R`) |

Both take `--dry-run` (prints what would be created and deleted, changes nothing) and `--help`.

## Naming and retention

Snapshots are named by UTC time, `YYYY-MM-DDTHH-MM-SSZ` (`date -u +%Y-%m-%dT%H-%M-%SZ`).
Scriptorium lists exactly the names matching that pattern; anything else in the directory is
reported as "skipped" and never used. The scripts likewise only ever delete matching names.

Retention, newest first: the newest snapshot of each of the last `KEEP_HOURLY` hours (default
24), `KEEP_DAILY` days (14) and `KEEP_WEEKLY` ISO weeks (8). The newest snapshot is always kept.
`MAX_DELETE` (default 20) is a circuit breaker: if a run would delete more than that, typically
after a clock jump or a retention typo, it deletes nothing and fails.

A snapshot is atomic and includes writes that were not yet `fsync`ed, so each SQLite database
in it looks as it would after the processes were killed: consistent, with its committed state
split between the main file and the `-wal` file. See "Reading SQLite from a snapshot" below.

## btrfs

Layout on the host (example paths; any location on the same btrfs filesystem works):

```
/srv/scriptorium/data        btrfs subvolume, bind-mounted at /data
/srv/scriptorium/snapshots   plain directory (or subvolume) beside it, NOT inside it
```

```sh
btrfs subvolume create /srv/scriptorium/data
mkdir /srv/scriptorium/snapshots
install -m 0755 docker/host-snapshots/snapshot-btrfs.sh /usr/local/sbin/scriptorium-snapshot-btrfs
DATA_SUBVOL=/srv/scriptorium/data SNAP_DIR=/srv/scriptorium/snapshots \
  /usr/local/sbin/scriptorium-snapshot-btrfs --dry-run
```

`SNAP_DIR` must be on the same btrfs filesystem as `DATA_SUBVOL` (a snapshot cannot cross
filesystems) and must not be inside it; the script refuses otherwise. Keeping them apart means a
snapshot of the data never contains earlier snapshots, the containers' read-write `/data` mount
never contains the history, and the data subvolume can be replaced from a snapshot without
touching the others.

**Why pruning runs as root.** An unprivileged user may create a read-only snapshot of a
subvolume it owns, but deleting a subvolume needs `CAP_SYS_ADMIN` unless the filesystem is
mounted with `user_subvol_rm_allowed`, which would let that user delete any subvolume it owns,
including the live data. So creation and pruning both live in one root job on the host, and
nothing in a container can remove history.

Cron, as `/etc/cron.d/scriptorium-snapshots` (hourly at minute 17):

```
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
17 * * * * root DATA_SUBVOL=/srv/scriptorium/data SNAP_DIR=/srv/scriptorium/snapshots /usr/local/sbin/scriptorium-snapshot-btrfs >>/var/log/scriptorium-snapshots.log 2>&1
```

In root's own crontab (`crontab -e` as root) drop the `root` user field. A systemd timer works the
same way. The script takes a lock (`LOCK_FILE`, default `/run/lock/scriptorium-snapshot.lock`) so
overlapping runs do not race.

Compose, in **both** `scriptorium` and `scriptorium-worker`:

```yaml
    environment:
      SCRIPTORIUM_SNAPSHOT_STRATEGY: host
    volumes:
      - /srv/scriptorium/data:/data
      - /srv/scriptorium/snapshots:/data/snapshots:ro
```

New btrfs snapshots appear inside the running containers at once: they are directories of the
same filesystem, not new mounts.

## ZFS

```sh
install -m 0755 docker/host-snapshots/snapshot-zfs.sh /usr/local/sbin/scriptorium-snapshot-zfs
DATASET=tank/scriptorium/data /usr/local/sbin/scriptorium-snapshot-zfs --dry-run
```

```
17 * * * * root DATASET=tank/scriptorium/data /usr/local/sbin/scriptorium-snapshot-zfs >>/var/log/scriptorium-snapshots.log 2>&1
```

ZFS snapshots are read-only by nature and live under the dataset's mount point at
`.zfs/snapshot/<name>`, so that is the directory to mount:

```yaml
    volumes:
      - /srv/scriptorium/data:/data            # the mountpoint of tank/scriptorium/data
      - /srv/scriptorium/data/.zfs/snapshot:/data/snapshots:ro,rslave
```

Each ZFS snapshot is auto-mounted on first access; `rslave` lets mounts made on the host after the
container started propagate into it. Instead of root, `zfs allow -u <user> snapshot,destroy,mount
tank/scriptorium/data` delegates exactly what the script needs. (Not yet exercised on a live ZFS
host; spike S5 ran on btrfs.)

## Reading SQLite from a snapshot

A read-only snapshot cannot hold the `-wal`/`-shm` files SQLite normally creates beside a
database, so `scriptorium.backup.snapshots.open_index_readonly` chooses per database:

- no `-wal` file, or an empty one: open with `?mode=ro&immutable=1` (no locks, no `-shm`);
- a non-empty `-wal`: open with `?mode=ro` and let SQLite rebuild the WAL index in memory.
  `immutable=1` would silently ignore the `-wal` file and show the database as of its last
  checkpoint, missing committed transactions. This path needs the mount to be read-only (it is
  refused otherwise), so SQLite cannot write a `-shm` into the snapshot.

Check from a running container (S5 step 3):

```sh
docker compose exec scriptorium python -c '
from pathlib import Path
from scriptorium.backup.snapshots import list_snapshots, open_index_readonly
listing = list_snapshots(Path("/data/snapshots"))
print([ (s.id, s.readonly) for s in listing ], "skipped:", listing.skipped)
if listing:
    conn = open_index_readonly(listing[0], "scriptorium.sqlite")
    print(conn.execute("PRAGMA integrity_check").fetchone())
'
```

## Backup of the backup (§13)

Snapshots live on the same filesystem as the data. Once a night, copy the newest
one to another filesystem (a second pool, an array, a mounted backup disk):

    MIRROR_DIR=/path/on/another/filesystem DATA_SUBVOL=... SNAP_DIR=... \
      snapshot-btrfs.sh --mirror-only

The copy goes to `MIRROR_DIR/latest/` (rsync from the read-only snapshot, never
from the live directory) and `MIRROR_DIR/latest.snapshot` records which snapshot
it holds, written only after the copy completes. Restoring from it is an rsync
back into a fresh data directory. Back up `secrets/` and the Keycloak database
separately; they are not under `/data`.
