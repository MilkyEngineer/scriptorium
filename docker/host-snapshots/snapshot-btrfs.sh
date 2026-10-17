#!/usr/bin/env bash
# Scriptorium host snapshots on btrfs (Architecture §10.1, SNAPSHOT_STRATEGY=host).
#
# Runs on the Docker host as root, from cron or a systemd timer. Takes a read-only snapshot of
# the btrfs subvolume that is mounted at /data in the containers, named by UTC time
# (YYYY-MM-DDTHH-MM-SSZ), into SNAP_DIR, then prunes older snapshots by an hourly/daily/weekly
# schedule. Scriptorium itself only reads SNAP_DIR, through a read-only bind mount at
# /data/snapshots; it never creates or deletes snapshots.
#
# Environment:
#   DATA_SUBVOL   btrfs subvolume bind-mounted at /data              (required)
#   SNAP_DIR      directory for the snapshots; same btrfs filesystem,
#                 NOT inside DATA_SUBVOL                              (required)
#   KEEP_HOURLY   newest snapshot of each of the last N hours         (default 24)
#   KEEP_DAILY    newest snapshot of each of the last N days          (default 14)
#   KEEP_WEEKLY   newest snapshot of each of the last N ISO weeks     (default 8)
#   MAX_DELETE    refuse to prune if more than N would go (breaker)   (default 20)
#   LOCK_FILE     serialises overlapping runs   (default /run/lock/scriptorium-snapshot.lock)
#
# Options:
#   --dry-run     print what would be created and deleted; change nothing (root not required)
#
# Only direct children of SNAP_DIR whose names match the timestamp pattern and that are btrfs
# subvolumes are ever deleted, with `btrfs subvolume delete` (never rm). The newest snapshot is
# always kept. Requires bash >= 4.4, GNU date and btrfs-progs.

set -euo pipefail

readonly NAME_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}Z$'

usage() {
    sed -n '2,/^$/s/^# \{0,1\}//p' "$0"
}

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run | -n) DRY_RUN=1 ;;
        --help | -h)
            usage
            exit 0
            ;;
        *)
            printf 'unknown argument: %s\n' "$arg" >&2
            exit 2
            ;;
    esac
done

: "${DATA_SUBVOL:?set DATA_SUBVOL to the btrfs subvolume mounted at /data}"
: "${SNAP_DIR:?set SNAP_DIR to the snapshot directory (outside DATA_SUBVOL)}"
KEEP_HOURLY=${KEEP_HOURLY:-24}
KEEP_DAILY=${KEEP_DAILY:-14}
KEEP_WEEKLY=${KEEP_WEEKLY:-8}
MAX_DELETE=${MAX_DELETE:-20}
LOCK_FILE=${LOCK_FILE:-/run/lock/scriptorium-snapshot.lock}

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() {
    log "error: $*"
    exit 1
}
run() {
    if ((DRY_RUN)); then
        log "dry-run: $*"
    else
        "$@"
    fi
}

for var in KEEP_HOURLY KEEP_DAILY KEEP_WEEKLY MAX_DELETE; do
    [[ ${!var} =~ ^[0-9]+$ ]] || die "$var must be a non-negative integer"
done
command -v btrfs >/dev/null 2>&1 || die "btrfs (btrfs-progs) not found"
date -u -d 2026-01-05 +%G-W%V >/dev/null 2>&1 || die "GNU date is required (date -d)"
if ((!DRY_RUN)) && ((EUID != 0)); then
    die "must run as root: deleting a btrfs subvolume needs it (use --dry-run to preview)"
fi

data_real=$(realpath -e -- "$DATA_SUBVOL") || die "DATA_SUBVOL does not exist"
snap_real=$(realpath -e -- "$SNAP_DIR") || die "SNAP_DIR does not exist"
[[ $data_real != / && $snap_real != / ]] || die "refusing to use / as DATA_SUBVOL or SNAP_DIR"
[[ -d $snap_real ]] || die "SNAP_DIR is not a directory"
case "$snap_real/" in "$data_real/"*) die "SNAP_DIR must not be inside DATA_SUBVOL" ;; esac
case "$data_real/" in "$snap_real/"*) die "DATA_SUBVOL must not be inside SNAP_DIR" ;; esac
[[ $(stat -f -c %T -- "$data_real") == btrfs ]] || die "DATA_SUBVOL is not on btrfs"
[[ $(stat -c %i -- "$data_real") == 256 ]] || die "DATA_SUBVOL is not the root of a subvolume"
[[ $(stat -f -c %T -- "$snap_real") == btrfs ]] || die "SNAP_DIR is not on btrfs"

if ((!DRY_RUN)); then
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCK_FILE"
        flock -n 9 || die "another run holds $LOCK_FILE"
    else
        log "warning: flock not found; overlapping runs are not prevented"
    fi
fi

# ---- create ---------------------------------------------------------------------------------
new=$(date -u +%Y-%m-%dT%H-%M-%SZ)
[[ ! -e $snap_real/$new ]] || die "$snap_real/$new already exists"
run btrfs subvolume snapshot -r "$data_real" "$snap_real/$new"
log "created $new"

# ---- prune ----------------------------------------------------------------------------------
names=()
shopt -s nullglob
for path in "$snap_real"/*; do
    name=${path##*/}
    if [[ $name =~ $NAME_RE ]]; then names+=("$name"); fi
done
shopt -u nullglob
if ((DRY_RUN)); then names+=("$new"); fi
if ((${#names[@]})); then
    mapfile -t names < <(printf '%s\n' "${names[@]}" | LC_ALL=C sort -ru)
fi

declare -A keep=()
keep[$new]=1

hour_of() { printf '%s' "${1:0:13}"; }
day_of() { printf '%s' "${1:0:10}"; }
week_of() { date -u -d "${1:0:10}" +%G-W%V; }

# Walk newest first and keep the first snapshot seen in each of the newest $1 buckets.
keep_by() {
    local count=$1 bucket_fn=$2 last="" kept=0 name bucket
    ((count > 0)) || return 0
    for name in "${names[@]}"; do
        bucket=$("$bucket_fn" "$name")
        if [[ $bucket != "$last" ]]; then
            keep[$name]=1
            last=$bucket
            kept=$((kept + 1))
            ((kept < count)) || break
        fi
    done
}
keep_by "$KEEP_HOURLY" hour_of
keep_by "$KEEP_DAILY" day_of
keep_by "$KEEP_WEEKLY" week_of

doomed=()
for name in "${names[@]}"; do
    if [[ -z ${keep[$name]:-} ]]; then doomed+=("$name"); fi
done
log "${#names[@]} snapshots, keeping $((${#names[@]} - ${#doomed[@]})), pruning ${#doomed[@]}"
if ((${#doomed[@]} > MAX_DELETE)); then
    die "would delete ${#doomed[@]} snapshots (> MAX_DELETE=$MAX_DELETE); check the clock and" \
        "retention, then rerun with a higher MAX_DELETE if intended"
fi

failures=0
for name in "${doomed[@]}"; do
    path=$snap_real/$name
    [[ $name =~ $NAME_RE ]] || continue
    if [[ -L $path || ! -d $path ]]; then
        log "skipping $name: not a directory"
        continue
    fi
    if [[ $(stat -c %i -- "$path") != 256 ]]; then
        log "skipping $name: not a btrfs subvolume"
        continue
    fi
    if run btrfs subvolume delete "$path" >/dev/null; then
        log "deleted $name"
    else
        log "error: could not delete $name"
        failures=$((failures + 1))
    fi
done
((failures == 0)) || die "$failures snapshot(s) could not be deleted"
