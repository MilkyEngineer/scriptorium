# SQLite WAL soak (spike S5, step 2)

`sqlite_soak.py` runs the server/worker access pattern of Architecture §4.1 and §6 against one
SQLite file: a **writer** (the worker) rewrites one document and its pages per second and leases
and completes jobs; a **reader** (the server) reads a document and enqueues a job every 250 ms,
owns the schema migration, and runs `PRAGMA integrity_check` plus `PRAGMA wal_checkpoint(PASSIVE)`
every `--integrity-every` seconds (default 3600) and once at the end. Settings are exactly §6:
`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`. All writes use
`BEGIN IMMEDIATE`, so a lock wait goes through the busy timeout rather than failing at once.

It is one stdlib-only file for Python 3.12; nothing to install. It is not collected by pytest;
`test_soak_smoke.py` runs it for five seconds.

**Pass criterion** (the `passed` field, exit status 0): `busy_errors == 0`, `other_errors == 0`,
and every `integrity_checks[].result == "ok"` (the reader must have run at least one).
`busy_errors` counts `database is locked` (SQLITE_BUSY/SQLITE_LOCKED) that survived the 5 s busy
timeout, which is what S5 counts.

## Local smoke run (one minute)

```sh
uv run python tests/soak/sqlite_soak.py --data /tmp/scriptorium-soak --role both --minutes 1 --integrity-every 20
```

`--role both` migrates, runs writer and reader as two child processes, and writes
`soak-writer-*.json`, `soak-reader-*.json` and a combined `soak-both-*.json` into `--data`.

## 24 h run in two containers on the Docker host

Run from a checkout of the repository on the host. `/srv/scriptorium/data` stands for the
host directory that will be mounted at `/data` in production (the dedicated subvolume or
dataset); the soak uses a `soak/` sub-directory of it and touches nothing else. Run as the
uid:gid that owns that directory, the same user the real stack uses.

```sh
DATA=/srv/scriptorium/data          # host path that becomes /data
RUN_AS="$(stat -c %u:%g "$DATA")"

docker run -d --name scriptorium-soak-reader --user "$RUN_AS" \
  --read-only --tmpfs /tmp --stop-timeout 30 \
  -v "$DATA:/data" -v "$PWD/tests/soak:/soak:ro" \
  python:3.12-slim \
  python /soak/sqlite_soak.py --data /data/soak --role reader --hours 24

docker run -d --name scriptorium-soak-writer --user "$RUN_AS" \
  --read-only --tmpfs /tmp --stop-timeout 30 \
  -v "$DATA:/data" -v "$PWD/tests/soak:/soak:ro" \
  python:3.12-slim \
  python /soak/sqlite_soak.py --data /data/soak --role writer --hours 24
```

Start the reader first; the writer waits up to two minutes for the schema, as the worker waits
for the server's migration. Once the Scriptorium image exists, the same commands work with
`--entrypoint python ghcr.io/milkyengineer/scriptorium:<tag>` in place of `python:3.12-slim python`.

Progress while it runs: `docker logs -f scriptorium-soak-reader` prints every integrity check and
any error (error messages are SQLite's; the database holds synthetic rows only).

To end early, `docker stop scriptorium-soak-reader scriptorium-soak-writer`: SIGTERM ends the
loop and the summary is still written.

## Reading the result

```sh
docker wait scriptorium-soak-reader scriptorium-soak-writer   # prints the exit codes: 0 = passed
ls "$DATA"/soak/soak-*.json
python3 - "$DATA"/soak/soak-*.json <<'EOF'
import json, sys
for path in sys.argv[1:]:
    s = json.load(open(path))
    print(path.rsplit("/", 1)[-1], "passed" if s["passed"] else "FAILED",
          "busy", s["busy_errors"], "other", s["other_errors"],
          "fs", s.get("filesystem"), "txns", s["transactions"]["total"],
          "latency_ms", s["latency_ms"],
          "checks", [c["result"] for c in s["integrity_checks"]])
EOF
```

Each summary records the role, start and end time, why it stopped, Python and SQLite versions,
the filesystem type of `--data` (from `/proc/self/mountinfo`), the effective pragmas, transaction
counts per kind, busy and other errors (with up to 20 samples), p50/p99/max transaction latency,
transactions slower than 1 s, and every integrity check with its checkpoint result.

Afterwards remove the soak directory: `rm -r "$DATA/soak"` (it contains only the soak database
and summaries; copy the summaries out first if you want them).
