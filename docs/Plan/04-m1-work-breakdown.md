# M1 work breakdown — safe archive, single user

Architecture v0.2 is the design of record; this document only sequences M1 (`docs/Plan/02-milestones.md`) into parallel workstreams with fixed interfaces, so that several coding agents can build against each other's modules before those modules exist. Where this document and the Architecture disagree, the Architecture wins and this file is corrected.

## 1. Starting point

Already on `main` and gated (CI green, reviewed):

- Skeleton, `config.py` (every §12 variable so far, default deny), `logging.py`, `cli.py`.
- Auth core: `auth/oidc.py`, `auth/metadata.py`, `auth/context.py`; `server.py` with the PRM, 401/403 contract, `/healthz`, `/dl/<token>` store, forwarded-header handling, stateless MCP mount, per-user `tools/list` filtering; `tools/_conventions.py` and the S6 spike tools.
- `users/fs.py` (mountinfo refusal + probes), `backup/snapshots.py` (host snapshot enumeration and read-only index open).
- Docker image, compose example, Keycloak realm, proxy examples, host snapshot scripts, CI with the licence gate.

Spike inputs: S0 (fork tag `scriptorium-s3`, identity claim `auth0-userid`), S1 (upload/restore semantics), S3 (geometry and goldens), S5 (steps 1 and 3; soak pending, 11 h clean at the time of writing), S6 (client rendering defaults, now in §11). S2 step 5 (lifetimes) gates only realm numbers, not code. S4 (b)/(c) gate only the `deletion_cause=aged` heuristic, which M1 implements behind a flag defaulting to `unknown`.

## 2. Workstreams

| Id | Name | Modules | Effort | Depends on | Security-critical |
| --- | --- | --- | --- | --- | --- |
| **A** | Archive store | `errors.py`, `users/store.py`, `users/secrets.py`, `library/layout.py`, `library/archive.py`, `library/tombstones.py` | xhigh | — | yes: path containment, never-delete, breaker |
| **B** | Index and jobs | `library/index.py`, `jobs/queue.py`, `db/migrations.py`, global schema | high | — | partly: migrations, lease semantics |
| **C** | Render | `library/render.py`, goldens, process pool helper | high | — | no |
| **D** | Cloud client and sync | `cloud/client.py`, `cloud/sync.py`, cassette harness, `tests/fixtures/cassettes/` | xhigh | interfaces of A, B (not their code) | yes: attacker-controlled cloud metadata, the only cloud reader |
| **E** | Worker, tools, audit | `worker.py`, `tools/account.py`, `tools/library.py`, `tools/ocr.py` (lazy path + `save_transcript` only), audit log, `server.py` wiring | high | A, B, C, D merged | partly: tool argument handling |

Wave 1 runs A, B, C, D in parallel, each in its own worktree, against the interfaces in §3 (agents implement their own side and test against fakes of the others). Wave 2 is E alone, after A–D are merged and the full gate passes on `main`. A fresh Fable adversarial review (xhigh) follows E, then the first live run against the operator's scratch folder with the S1 kill switch; nothing touches a real library before that review.

Everything in this milestone is **read-only towards the cloud**: no workstream makes a cloud write. `connect_remarkable` is the single exception (device registration), and it is in E.

## 3. Interface contracts

These are the seams between workstreams. Names and signatures are binding; bodies are the implementing workstream's business. All are synchronous and blocking; the server and worker call them from a thread pool. All raise `scriptorium.errors.ScriptoriumError` subclasses carrying one of the §11.0 error codes (`errors.py` is created by A on day one and is the first thing merged, so B–D import it rather than defining their own).

### 3.1 Paths and secrets (A)

```python
# users/store.py
@dataclass(frozen=True)
class AccountPaths:
    root: Path          # /data/users/<uid>/accounts/<acct>
    blobs: Path; library: Path; tombstones: Path; pages: Path; staging: Path
    index: Path         # root / "index.sqlite"
    def inside(self, p: Path) -> Path: ...   # resolves and asserts containment; raises PathEscapeError

def account_paths(data_dir: Path, uid: str, acct: str) -> AccountPaths   # uid, acct validated by regex
def user_dir(data_dir: Path, uid: str) -> Path

# users/secrets.py
class SecretBox:   # MultiFernet over SCRIPTORIUM_MASTER_KEYS
    def seal(self, name: str, value: bytes) -> None
    def open(self, name: str) -> bytes | None
    def forget(self, name: str) -> None
```

`uid` is `uid_hash[:16]` from `auth/context.py`; `acct` is a ULID (`^[0-9A-HJKMNP-TV-Z]{26}$`). Any filename derived from cloud data passes `library/layout.py:is_safe_name` (the §11.0 allowlist regex) before it is joined.

### 3.2 Archive (A)

```python
# library/archive.py
class Archive:
    def __init__(self, paths: AccountPaths): ...
    def stage(self, uuid: str, job: str) -> Staging          # staging/<uuid>.<job>/ ; context manager, swept on error
    def put_blob(self, sha256: str, stream: BinaryIO) -> Path   # verifies the hash while writing; fsync; idempotent
    def commit(self, staged: Staging, files: Mapping[str, str], generation: int, hash_root: str) -> Committed
        # files: xochitl-relative name -> blob sha256; hardlinks/copies blobs into library/ atomically
        # (rename of a prepared tree); previous files of the uuid move to a per-generation holding area
    def tombstone(self, uuid: str) -> None                   # move library/<uuid>* -> tombstones/<uuid>/ ; never unlink
    def untombstone(self, uuid: str) -> None
    def sweep_staging(self) -> int
    def current_files(self, uuid: str) -> Mapping[str, Path]
    def blob_path(self, sha256: str) -> Path
```

Invariant tested in `tests/library/test_archive.py`: no method ever calls `unlink`/`rmtree` on anything under `library/`, `blobs/` or `tombstones/` (a fake `os` layer asserts it).

### 3.3 Tombstone planner (A)

```python
# library/tombstones.py  (pure; no I/O)
@dataclass(frozen=True)
class Listing: uuids: frozenset[str]; complete: bool; generation: int
@dataclass(frozen=True)
class TombstonePlan: to_tombstone: frozenset[str]; to_untombstone: frozenset[str]; anomaly: str | None
def plan(previous: Listing | None, current: Listing, library: frozenset[str], tombstoned: frozenset[str],
         max_per_poll: int, *, allow_mass_delete: bool = False) -> TombstonePlan
```

`allow_mass_delete` (added after A landed) is the one-poll override an administrator grants through `admin_clear_anomaly(..., allow_mass_delete=true)` (Architecture §7 step 4); it skips rule 5 only. The caller never applies a plan while the account is read-only, and passes the immediately preceding listing as `previous`.

Rules encoded exactly as §7 step 1 and 4: no candidates unless `previous.complete and current.complete`; candidates = absent from both; breaker when candidates > `max(5, 10 % of library)` or > `max_per_poll` → `anomaly="mass_delete"` and an empty `to_tombstone`; generation lower than previous → `anomaly="generation_regressed"`; zero entries after non-empty → `anomaly="empty_root"`.

### 3.4 Index (B)

```python
# library/index.py
class Index:
    @classmethod
    def open(cls, path: Path, *, migrate: bool) -> Index     # WAL, synchronous=NORMAL, busy_timeout=5000, foreign_keys=ON
    def version(self) -> int
    # documents
    def upsert_document(self, doc: DocumentRow) -> None
    def mark_deleted(self, uuid: str, cause: str, at: datetime) -> None
    def unmark_deleted(self, uuid: str) -> None
    def document(self, uuid: str) -> DocumentRow | None
    def children(self, parent: str | None, *, include_trash: bool, include_deleted: bool, cursor: str | None, limit: int) -> Page[DocumentRow]
    def resolve_path(self, parts: Sequence[str]) -> list[str]   # candidate uuids; len>1 == ambiguous
    def recent(self, since: datetime, cursor: str | None, limit: int) -> Page[ChangeRow]
    # history and pages
    def append_history(self, uuid: str, generation: int, hash_root: str, seen_at: datetime) -> None
    def history(self, uuid: str) -> list[HistoryRow]
    def replace_pages(self, uuid: str, pages: Sequence[PageRow]) -> None
    def page(self, uuid: str, idx: int) -> PageRow | None
    def page_by_id(self, document: str, page_id: str) -> PageRow | None
    def set_rendered(self, document: str, page_id: str, content_hash: str, png_path: str, at: datetime) -> bool  # marks every document sharing (page_id, content_hash); returns whether the caller's row was one
    # transcripts (M1: lazy write-back only)
    def put_transcript(self, document: str, page_id: str, content_hash: str, text: str, model: str, mode: str) -> None
    def transcript(self, document: str, page_id: str, content_hash: str) -> TranscriptRow | None
    # sync state
    def get_state(self, key: str) -> str | None
    def set_state(self, key: str, value: str | None) -> None
    # one explicit transaction for the commit step of §7.3
    @contextmanager
    def transaction(self) -> Iterator[None]
```

Row dataclasses live in `library/index.py`; `Page[T]` is `(items, next_cursor)`. Cursors are opaque base64 of the sort key, never offsets. `migrate=True` only in the server; the worker opens with `migrate=False` and raises `SchemaMismatch` if `user_version` differs from the code's.

### 3.5 Jobs and global store (B)

```python
# jobs/queue.py  (scriptorium.sqlite)
class JobQueue:
    def enqueue(self, *, uid: str, acct: str | None, kind: str, dedupe_key: str, payload: Mapping[str, Any],
                max_attempts: int = 5, run_at: datetime | None = None) -> JobRow        # idempotent on dedupe_key
    def lease(self, owner: str, *, kinds: Sequence[str] | None = None, ttl_s: int = 60) -> JobRow | None   # BEGIN IMMEDIATE … RETURNING
    def heartbeat(self, job_id: int, owner: str, ttl_s: int = 60) -> None
    def complete(self, job_id: int, owner: str, result: Mapping[str, Any]) -> None
    def fail(self, job_id: int, owner: str, error: str) -> JobRow     # backoff min(2^attempts×30 s, 1 h); dead after max_attempts
    def cancel(self, job_id: int, uid: str) -> bool                   # only while queued
    def requeue_expired(self) -> int
    def get(self, job_id: int, uid: str) -> JobRow | None

# users/store.py also owns the global tables users / accounts / audit:
class GlobalStore:
    def ensure_user(self, user: User) -> UserRow
    def accounts(self, uid: str) -> list[AccountRow]
    def link_account(self, uid: str, rm_identity: str | None, device_desc: str) -> AccountRow   # refuses if rm_identity is linked to another uid
    def deactivate_account(self, uid: str, acct: str) -> None
    def audit(self, *, uid_hash: str, tool: str, duration_ms: float, outcome: str, identifiers: Mapping[str, Any]) -> None
```

`db/migrations.py` holds both schemas as ordered SQL steps keyed by `user_version`; the server runs them at start for the global DB and lazily per account on first open.

### 3.6 Render (C)

```python
# library/render.py
@dataclass(frozen=True)
class RenderResult: png: bytes; width: int; height: int; extent_units: tuple[float, float, float, float]
def render_rm_page(rm_bytes: bytes, *, scale: float = 1.0, pdf_page: bytes | None = None) -> RenderResult
def render_pdf_page(pdf_bytes: bytes, index: int, *, scale: float = 1.0) -> RenderResult
def page_image_bytes(result: RenderResult, *, max_side_px: int = 1024) -> bytes     # the ~200 KB class of §11.2
```

Geometry per §8.3 exactly; the S3 goldens in `tests/fixtures/move-test-notebook/golden/` are the acceptance test, byte-compared after a deterministic encode. Rendering runs in a `concurrent.futures.ProcessPoolExecutor` owned by the caller; `render.py` has no global state.

### 3.7 Cloud client and sync (D)

```python
# cloud/client.py  (thin, typed wrapper over the fork; the only module that imports it)
class CloudClient:
    @classmethod
    def from_device_token(cls, token: str, *, cache_dir: Path, http_timeout_s: float = 30) -> CloudClient
    def root(self) -> RootInfo                       # hash, generation, entries
    def document_index(self, uuid: str, hash: str) -> DocumentIndex    # files: name -> (sha256, size)
    def open_blob(self, sha256: str) -> ContextManager[BinaryIO]      # streaming
    def account_identity(self) -> str | None        # S0: auth0-userid from the token, or None
    def register_device(code: str, description: str) -> str          # staticmethod; the only write; used by E

# cloud/sync.py
class SyncOnce:
    def __init__(self, client: CloudClient, archive: Archive, index: Index, settings: SyncSettings, clock: Callable[[], datetime]): ...
    def run(self) -> SyncReport        # §7 steps 1–4 and 7, exactly; rendering and OCR are the worker's (E)
```

Every string from the cloud is untrusted: names are display data, file names go through `is_safe_name`, sizes are capped, JSON is bounded. Facts from A's archive that D must honour: hashes are normalised to lowercase hex before they reach the archive or the index; the document index blob is stored with `put_blob` under the SHA-256 of its body and that digest is passed to `append_history(index_sha256=…)` so history rows resolve to a file set (§6); files in a real index whose names fail the allowlist are skipped and counted in the `SyncReport`, never passed to the archive; `commit` refuses a bare `<uuid>` file name; a plan is applied only when the account is not read-only. Tests use the cassette harness (`tests/cloud/cassettes.py`: recorded, **scrubbed** request/response pairs played back through `httpx.MockTransport`); the recorder refuses to write a cassette containing a bearer token, a UUID not from the fixture allowlist, or any `.rm` payload that is not from the S3 fixture notebook. Live recording needs `SCRIPTORIUM_LIVE_TEST=1` and the operator's scratch device token, and is never run by an agent.

### 3.8 Worker and tools (E)

`worker.py`: per-account asyncio task running `SyncOnce` on the §7 schedule (jitter, backoff classes, `needs_relink` after three token rejections, safe mode on anomaly), then render for changed pages via the process pool, then the job runner loop (`lease` → run → `complete`/`fail`, `requeue_expired` every minute). M1 job kinds: `render`, `page_image`. OCR jobs are M2.

Tools (§11.1–11.3 subset for M1): `connect_remarkable`, `disconnect_remarkable`, `account_status`, `list`, `recent`, `read_page` (lazy path only: returns the image block and the §8.2 instruction when there is no transcript), `read_document(markdown)`, `page_image`, `versions`, `trash`, `save_transcript`. Every tool follows `tools/_conventions.py`; `page_image` returns the image block **and** a Markdown `/dl/` link (S6). Audit on every call through `GlobalStore.audit`.

Follow-ups E inherits from A–D (all on main as of 2026-10-18):

- **Worker loop around `SyncOnce`:** `SyncOnce.run()` only classifies (`SyncReport.outcome` ∈ ok/partial/backoff/token_rejected/anomaly); the loop owns the backoff schedule, the three-consecutive-rejections → `needs_relink` rule, and the per-poll watchdog (Architecture §7 step 7). The per-poll byte and time budgets are D's and already in `SyncSettings` (`sync_max_poll_bytes`, `sync_max_poll_seconds`); E wires them from `Settings` (`SCRIPTORIUM_SYNC_MAX_POLL_BYTES`, `SCRIPTORIUM_SYNC_MAX_POLL_SECONDS`, §12) and treats `partial` with reason `byte_budget`/`time_budget` as "poll again soon". A poll that ends early never tombstones. `SyncOnce` also accepts `monotonic=` for tests. `SyncOnce` takes `planner=` and reads the one-shot `sync_state` key `allow_mass_delete` (value `"true"`); only `admin_clear_anomaly` (M2) writes it.
- **Pages:** `SyncOnce` does not populate `pages`; after a commit the worker reads the `.content` page list and calls `Index.replace_pages`, then enqueues `render` jobs for pages whose `rendered_at` is null. `pages/` under the account root is E's (§6).
- **Index additions:** `Index.document_uuids(*, deleted: bool) -> frozenset[str]` (sync currently pages `recent` from year 1 to build the planner's sets; switch `SyncOnce._library_sets` to it), and the `search` name fallback of §11.2 for documents without transcripts.
- **Store facts:** `GlobalStore.link_account` refuses while another account is active, so `connect_remarkable(replace=true)` calls `deactivate_account` first; relinking the same identity reuses the acct. Extend `AUDIT_IDENTIFIER_KEYS` if a tool needs another identifier (never content). Readers of `library/` may see a brief `FileNotFoundError` during a swap (retry once) and must skip `.holding/`.
- **Schema:** `scriptorium.sqlite` opens through `SCRIPTORIUM_STEPS` (`db/schema_global.py`) whichever store opens it first; the server migrates, the worker checks. `history.index_sha256` names the blob holding a version's file list.
- **Render:** tools convert 1-based pages to the 0-based `render_pdf_page(index)`; annotated PDF pages get a single-page PDF from `extract_pdf_page`; configure rmscene's logger once (it logs "Some data has not been read" per page, content-free but noisy).
- **Fixtures and cassettes:** `tests/cloud/build_cassettes.py` regenerates the synthetic cassettes; run it after any change to the fixture, the fake cloud or the client's request pattern.

## 4. Rules for every agent

- Read `CLAUDE.md`, the Architecture sections your workstream cites, and this file. Do not edit `docs/Architecture.md`; put proposed changes in your hand-back and the coordinator edits it.
- Implement exactly the interface in §3 for your workstream. If it is wrong, say so in the hand-back with the signature you propose; do not silently change a seam another workstream builds against.
- Fakes for the other workstreams' interfaces live under `tests/fakes/` and are shared; add to them, do not fork them.
- Hard rules apply in full: no code path deletes under `library/`, `blobs/`, `tombstones/`; no cloud write; no real library; nothing user- or cloud-derived reaches a shell or a path join without the allowlist; no content, tokens or arguments in logs; no new dependency without `scriptorium licence-check` and a note for §4.4.
- Gate before hand-back, unmasked: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run --with pyyaml pytest -q`. Tests mirror the module tree under `tests/`.
- Small imperative commits, one concern each, with the attribution lines from the session. Never push; the coordinator merges.
- Security-critical workstreams (A, D) use the `security-guidance` plugin's review before hand-back and report what it found.

## 5. Acceptance mapping

| M1 check | Proven by |
| --- | --- |
| 1 login + 403 | already (S2); E adds `connect_remarkable` behind it |
| 2 whole library on disk in xochitl layout | D cassette replay of a full listing → A archive; E `list` against tablet by the operator |
| 3 edit → PNG + history, old blob kept, `read_page(version=…)` | D + A + C; E tool |
| 4 delete → tombstone after two polls, no unlink | A planner + archive invariant test; D sync; E `trash()` |
| 5 partial listing → zero tombstones | A planner unit test; D cassette with one failed index fetch |
| 6 60 % missing → `mass_delete` anomaly | A planner; D; E `account_status` |
| 7 SIGKILL mid-download → no partial document, staging swept | A `stage`/`commit`/`sweep_staging` tests; D kill test with cassette |
| 8 token outage vs rejection ×3 | D failure classes; E `needs_relink` |
| 9 p95 < 2 s on cached data | E benchmark test over fixtures |
| 10 licence gate, read-only rootfs | already in CI |
| 11 audit rows contain no arguments or content | B/E audit test with a grep over a synthetic day |
| 12 data-policy answer | operator, before any real library is linked |

## 6. Deferred from M1 (recorded at integration, 2026-10-18)

Known gaps left on main after A–E merged; each is owned by the milestone named.

- **M2:** confirmation tokens (`disconnect_remarkable(keep_archive=false)` is `refused` until then); `admin_clear_anomaly`; per-user caps on queued jobs and fair leasing across users in `jobs/queue.py` (today one user can queue about two `page_image` jobs per page version, bounded by the scale snapping); a renderer version in the render dedupe key so a renderer fix can re-render pages whose job died.
- **M3:** `read_document(format="pdf")`; `page_image(version=)`; `MAX_INLINE_KB` (config default 256, Architecture §12 says 64 — settle it with the push tools, which are its only consumer).
- **D (small):** switch `SyncOnce._library_sets` to `Index.document_uuids(deleted=)`; log the suffix shape of skipped names so a new xochitl file type shows up in the logs before it is lost.
- **From the M1 adversarial review (2026-10-18), not fixed:** journal recovery with a vanished `place` source leaves `hash_root` claiming a version `library/` lacks (null that document's `hash_root` when `archive_journal_source_missing` fires, so the next poll re-commits from blobs); pin GitHub actions and the `python`/`uv` base images by digest; a `gone` render result is a done job without a PNG and burns re-runs; `admin_clear_anomaly` (M2) needs the tripped candidate set, so the breaker should persist it as `sync_state.anomaly_candidates`; a budget stop after planning spends the mass-delete approval, which is safe now that it is bound to candidates but could be kept instead. Unconfirmed: `_library_sets` via `recent(_BEGINNING)` could miss rows with a `last_modified` before the epoch (removed by the `document_uuids` switch); the abandoned poll thread after a watchdog `client.close()` may surface an untranslated httpx error as an unretrieved future; `_check_length` uses `isdigit()`; many `page_png` waiters polling SQLite every 0.25 s could saturate the default thread pool. Open design question for the operator: should a listing count as complete whenever the root parsed, even if some document indexes failed, so one broken document cannot suspend deletion mirroring (changes §7 step 2)?
- **Operator checks before M1 closes:** acceptance #1, #2, #3, #4, #7 (real SIGKILL) and #10 against the compose stack; the lazy OCR round trip from claude.ai web and mobile; duplicate a notebook in the scratch folder to settle open question 8.
