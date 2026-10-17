#!/usr/bin/env bash
# Scriptorium host snapshots on ZFS (Architecture §10.1, SNAPSHOT_STRATEGY=host).
#
# Runs on the Docker host as root (or as a user granted `zfs allow ... snapshot,destroy,mount`),
# from cron or a systemd timer. Takes a snapshot DATASET@YYYY-MM-DDTHH-MM-SSZ (UTC) of the
# dataset mounted at /data in the containers, then prunes older ones by an hourly/daily/weekly
# schedule. ZFS snapshots are read-only by nature and appear under
# <dataset mountpoint>/.zfs/snapshot/<name>; that directory is what gets bind-mounted read-only
# at /data/snapshots. Scriptorium only reads it.
#
# Environment:
#   DATASET       the ZFS dataset mounted at /data, e.g. tank/scriptorium/data   (required)
#   KEEP_HOURLY   newest snapshot of each of the last N hours         (default 24)
#   KEEP_DAILY    newest snapshot of each of the last N days          (default 14)
#   KEEP_WEEKLY   newest snapshot of each of the last N ISO weeks     (default 8)
#   MAX_DELETE    refuse to prune if more than N would go (breaker)   (default 20)
#   LOCK_FILE     serialises overlapping runs   (default /run/lock/scriptorium-snapshot.lock)
#
# Options:
#   --dry-run     print what would be created and destroyed; change nothing
#
# Only snapshots of DATASET itself (never children, never -r/-R) whose names match the timestamp
# pattern are destroyed. The newest snapshot is always kept. Requires bash >= 4.4, GNU date and
# the zfs command.

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

: "${DATASET:?set DATASET to the ZFS dataset mounted at /data}"
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
[[ $DATASET != *@* && $DATASET != -* ]] || die "DATASET must be a dataset name, not a snapshot"
command -v zfs >/dev/null 2>&1 || die "zfs not found"
date -u -d 2026-01-05 +%G-W%V >/dev/null 2>&1 || die "GNU date is required (date -d)"
[[ $(zfs list -H -o type "$DATASET") == filesystem ]] || die "DATASET is not a ZFS filesystem"

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
run zfs snapshot "$DATASET@$new"
log "created $DATASET@$new"

# ---- prune ----------------------------------------------------------------------------------
names=()
while IFS= read -r full; do
    [[ $full == "$DATASET@"* ]] || continue
    name=${full#"$DATASET@"}
    if [[ $name =~ $NAME_RE ]]; then names+=("$name"); fi
done < <(zfs list -H -t snapshot -o name -d 1 "$DATASET")
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
    die "would destroy ${#doomed[@]} snapshots (> MAX_DELETE=$MAX_DELETE); check the clock and" \
        "retention, then rerun with a higher MAX_DELETE if intended"
fi

failures=0
for name in "${doomed[@]}"; do
    [[ $name =~ $NAME_RE ]] || continue
    if run zfs destroy "$DATASET@$name"; then
        log "destroyed $DATASET@$name"
    else
        log "error: could not destroy $DATASET@$name (held or cloned?)"
        failures=$((failures + 1))
    fi
done
((failures == 0)) || die "$failures snapshot(s) could not be destroyed"
