# Scriptorium — working notes for Claude Code

Read `docs/Architecture.md` (v0.2) before changing anything. It is the design of record; `docs/Plan/` sequences the work and `docs/Plan/03-review-dispositions.md` explains why the design says what it says. When architecture and plan disagree, the architecture wins and the plan gets fixed. If you want to change the architecture, edit the doc in the same PR and say why.

## What this is

A self-hosted MCP server + sync worker (one Docker image, two entrypoints) that mirrors reMarkable cloud libraries to `/data`, keeps every version, renders and OCRs pages, pushes documents to the tablet, and restores after a wipe. Auth is delegated to whatever OIDC issuer the operator provides; Scriptorium ships none (Keycloak is an optional compose profile). Scope is reMarkable + managing Scriptorium. Nothing about calendars, ticketing, documentation or HR systems belongs in this repo.

## Hard rules

- **Never lose a note.** No code path deletes files under `users/*/accounts/*/{library,blobs,tombstones}` except `admin_purge_tombstones` and `admin_gc_blobs`, both behind a dry-run confirmation token. Tombstoning requires two consecutive complete listings; the mass-delete circuit breaker is not optional.
- **The worker is the only cloud writer.** The server enqueues jobs. Every cloud write is an upsert by document UUID with the generation check and a retry that re-applies the mutation. Never write to a real library in development: use cassettes, or the scratch account behind `SCRIPTORIUM_LIVE_TEST=1`.
- **Licensing:** the image is MIT. No GPL/AGPL Python package may end up in `site-packages`: `pymupdf`, `ebooklib`, `rmink` are the known offenders and must never be importable from `scriptorium/*`. `rmc` is dropped (dependency conflict). Image→ink goes through the `scriptorium-ink` sidecar over HTTP. Run `scriptorium licence-check` (or `pip-licenses`) before adding a dependency and record the licence source in Architecture §4.4.
- **Clean room:** anyone implementing the native image-to-ink converter must not have rmink's source in their working context. Do not read, fetch or vendor rmink code into this repo.
- **Privacy:** never log note content, transcripts, tokens, API keys, or raw tool arguments. Log hashed user id, tool, duration, outcome, and non-content identifiers (document UUIDs, job ids, byte counts, URL host, snapshot id).
- **Tenancy:** no tool takes a user id or a filesystem path. The user comes from the bearer token as `(iss, sub)`; paths are derived in `users/store.py` only; every resolved path is asserted inside the account root; cloud-supplied filenames pass the UUID allowlist regex.
- **Untrusted input:** cloud metadata, note content, Markdown, URLs and the model's own arguments are attacker-controlled. Markdown renders with HTML disabled and a deny-all fetcher in a subprocess; URL fetches go through `push/fetch.py` only; nothing user- or cloud-derived ever reaches a shell.
- **Secrets:** env or `*_FILE`; per-user secrets encrypted with `SCRIPTORIUM_MASTER_KEYS` (MultiFernet); never returned by any tool, including admin tools. The master keys live outside `/data`.
- **Default deny:** the server refuses to start without `SCRIPTORIUM_OIDC_ALLOWED_USERS` (or the explicit opt-out) and without master keys. `SERVER_KEY_USERS` defaults to empty.
- **Spikes before features:** S0–S6 in `docs/Plan/01-spikes.md` gate the milestones. Do not start code that depends on a spike without a written result in `docs/Spikes/<id>.md` (scrubbed evidence, decision).
- **Public repo hygiene:** cassettes and fixtures contain no tokens, no real document UUIDs/names, no note content. Fixture notebooks are written for the purpose (S3).

## Stack and conventions

- Python 3.12, `uv` for everything (`uv sync`, `uv run`, `uv lock`). No `pip install` in docs or Dockerfiles.
- Lint/format: `ruff` (line length 100); `mypy --strict` on `src/scriptorium`. Both run in CI and must pass.
- Tests: `pytest`; unit tests mirror the module tree under `tests/`; fixtures in `tests/fixtures/` (the S3 test notebook, golden PNGs, a sample `.rmdoc`, scrubbed HTTP cassettes). A change to rendering updates goldens deliberately, in its own commit.
- MCP: official `mcp` Python SDK (version pinned at S0), Streamable HTTP, stateless. Per-request user via ASGI middleware + contextvar; `tools/list` filtered per user. Blocking library calls run in a thread pool. Tool names are flat snake_case as in Architecture §11; conventions (identifiers, 1-based pages, pagination, error codes, confirmation tokens, annotations) live in `tools/_conventions.py` and §11.0. Every tool has a one-paragraph docstring that becomes its description.
- SQLite: WAL, `synchronous=NORMAL`, `busy_timeout=5000`, short per-document transactions; migrations via `user_version`, owned by the server.
- Settings: `pydantic-settings`, prefix `SCRIPTORIUM_`, typed; add every new variable to Architecture §12 in the same commit.
- Commits: small, imperative subject, body explains why. One spike or one tool per commit where possible. Don't commit `/data`, `.env`, keys.
- Docker: `python:3.12-slim`, non-root, read-only root filesystem (`/tmp` tmpfs, `XDG_CACHE_HOME=/tmp/fontconfig`), multi-arch (amd64, arm64). System packages limited to WeasyPrint's needs, each with a comment. Image: `ghcr.io/milkyengineer/scriptorium`.

## Repository layout (target)

```
src/scriptorium/   docker/{Dockerfile,compose.example.yml,keycloak/,proxy/,host-snapshots/}   tests/   docs/{Architecture.md, Plan/, Spikes/}   README.md   CLAUDE.md
```

## Things only the operator can provide — ask, don't guess

- A reMarkable one-time code from https://my.remarkable.com/device/browser/connect (expires in minutes; ask right before use), and which account it is for (scratch vs. real — development uses scratch only).
- Whether the scratch account has a Connect subscription (S4).
- Public hostname for the MCP endpoint (e.g. `mcp.example.com`), TLS/ACME arrangement, and which reverse proxy already runs on the Docker host and how its config is managed.
- The OIDC issuer: an existing one (issuer URL, how to register two public clients, which claim marks admins) or permission to run the Keycloak compose profile (and whether to broker Microsoft/Google logins).
- The `/data` filesystem type and whether host snapshots (ZFS/btrfs) are available — decides `SCRIPTORIUM_SNAPSHOT_STRATEGY` and S5.
- A Claude API key for the server-wide OCR mode, and the monthly caps.
- `SCRIPTORIUM_MASTER_KEYS` (generate with `scriptorium gen-master-key` or `openssl rand -base64 32 | tr '+/' '-_'`; Docker secret outside `/data`; escrow copy). Development gets its own key, never production's.
- Checks on the physical tablet (S1, S3, M3, M4) and manual runs from claude.ai mobile (S2, S6, M1).
- Confirmation that any organisation whose notes will be synced permits it under its data policy (Architecture open question 5; M1 acceptance #12).
- Where the repository's deploy key lives on the Docker host, if one is used.

## Definition of done for a milestone

CI green (ruff, mypy, pytest, image build for both arches, licence gate), the milestone's acceptance checks in `docs/Plan/02-milestones.md` demonstrated against a compose stack and recorded in the PR, and Architecture.md updated for anything that changed.
