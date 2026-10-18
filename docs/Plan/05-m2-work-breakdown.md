# M2 work breakdown — OCR modes, search, scope

Architecture v0.2 is the design of record; this document sequences M2 (`docs/Plan/02-milestones.md`) plus the items M1 handed to it (`04-m1-work-breakdown.md` §6) into parallel workstreams with fixed interfaces, so that several coding agents can build against each other's modules before those modules exist. Where this document and the Architecture disagree, the Architecture wins and this file is corrected. The M1 breakdown's conventions (worktrees, waves, hand-backs, the gate) carry over unchanged.

## 1. Starting point

On `main` at `453f51d`, M1 complete and live on the operator's host (acceptance record in `04-m1-work-breakdown.md` §7):

- Archive, index (account schema V2: pages keyed `(document, uuid)`), jobs (`render`, `page_image`, reruns, lease expiry), render pool, cloud client and `SyncOnce` with budgets and the two-listings rule, worker loop with watchdog and heartbeat, the M1 tool set, audit, cassettes, Keycloak realm (admin role mapped on the clients), CI with the licence gate.
- Already present for M2 to build on: `transcripts` with token and cost columns, `transcripts_fts` with triggers, `Index.search` (FTS then name fallback, cursor-paginated) **without a tool in front of it**, `Index.put_transcript` / `transcript`, `pages.stable_since`, `users.server_key_allowed`, `SecretBox`, `tools/_conventions.py` with the error codes and the two `NotImplementedError` stubs for confirmation tokens, `tools/ocr.py` with `LAZY_INSTRUCTION` and `save_transcript`.
- Not present yet: any `ocr/` package, the OCR settings of §12 (`ANTHROPIC_API_KEY`, `SERVER_KEY_USERS`, `OCR_*`) in `config.py`, the `anthropic` dependency, a `tags` table, per-account settings storage, a job priority, `cloud/notifications.py`.

Spike inputs: S6 (client rendering defaults, binding for every tool result), S7 (notifications websocket; **not run**; gates only the wake-up item in I and is read-only), S4 (b)/(c) still waiting for the aged probe (December), S5 soak write-up closing M1.

What M2 adds beyond the milestone table in `02-milestones.md`, all decided with the operator on 2026-10-18: tags indexed from `.content` and exposed in `list` and `search`; `set_sync_scope`; poll-on-notification behind S7; render jobs of tombstoned documents yield to live ones; `admin_clear_anomaly`; confirmation tokens and `disconnect_remarkable(keep_archive=false)`; the unfixed M1 review leftovers.

## 2. Workstreams

| Id | Name | Modules | Effort | Depends on | Security-critical |
| --- | --- | --- | --- | --- | --- |
| **F** | OCR keys, modes, caps, cost | `ocr/__init__.py`, `ocr/modes.py`, `ocr/ledger.py`, `ocr/client.py`, `ocr/prompt.py` + `ocr/prompt.md`, `config.py` (§12 OCR variables), `users/store.py` additions, `tools/account.py` (`set_claude_api_key`, `clear_claude_api_key`), `tools/admin.py` (`admin_allow_server_key`) | xhigh | — | yes: API keys at rest and in memory, spend caps, nothing content-bearing in logs |
| **G** | Transcription pipeline | `jobs/kinds.py` (`OcrSpec`), `ocr/debounce.py`, `ocr/batches.py`, worker OCR job runner and auto-OCR scan, `tools/ocr.py` (`transcribe`, `ocr_status`, `read_page` mode switch), `tools/account.py` (`set_auto_ocr`), account step V5 | high | F's interfaces (fakes), I's `priority` | partly: transcripts are note content; batch results are untrusted input |
| **H** | Search and tags | `library/tags.py`, `library/pages.py` (`content_tags`), `library/index.py` (`tags` table, `replace_tags`, `tags_of`, `children(tags=, recursive=)`, `search(folder=, tags=)`), `tools/library.py` (`search`, `list(tags=)`), worker commit hook | high | — | partly: tags are cloud-supplied strings |
| **I** | Sync scope, wake-up, priority, admin and destructive tools | `cloud/scope.py`, `cloud/sync.py` (scope filter, `excluded`, `anomaly_candidates`), `cloud/notifications.py` (if S7 passes), `jobs/queue.py` (`priority`, `deprioritise`), `tools/_conventions.py` (confirmation tokens), `tools/account.py` (`set_sync_scope`, `disconnect_remarkable(keep_archive=false)`), `tools/admin.py` (`admin_clear_anomaly`), review leftovers | xhigh | S7 result for the wake-up only | yes: the only cloud reader, destructive path, anomaly clearing |

Wave 1 runs F, H and I in parallel, each in its own worktree, against the interfaces in §3; G also starts in wave 1 against fakes of F's `SpendLedger` and `OcrClient` and of I's `priority`, but its worker and tool wiring merges only after F and I are on `main` (wave 2, like E in M1). A fresh Fable adversarial review (xhigh) follows wave 2, then deploy to the operator's stack, then the acceptance checks of §5.

Cloud writes: **none** in M2. `set_sync_scope` changes what the worker downloads, never what is in the cloud. The Claude API is written to (requests carry page images), so every call goes through `OcrClient`, which is the only module that imports the SDK.

## 3. Interface contracts

Seams between workstreams. Names and signatures are binding; bodies are the implementing workstream's business. All synchronous and blocking unless marked; errors are `scriptorium.errors.ScriptoriumError` subclasses with §11.0 codes. Row and settings additions go through `db/` migrations owned by the workstream named, as ordered SQL steps. Account step V3 (`settings` table with `Index.get_setting`/`set_setting`, `documents.excluded_at`) is already on `main`; H owns V4 (`tags`), G owns V5 (`transcripts.prompt_version`); global steps continue from the current `SCRIPTORIUM_STEPS` (F: `ocr_spend`, `users.ocr_cap_usd`; I: `jobs.priority`, `confirmations`), and F's step merges first, so I rebases onto it.

### 3.1 OCR keys, modes, caps, cost (F)

```python
# config.py — §12, all SCRIPTORIUM_-prefixed, *_FILE accepted for the key
anthropic_api_key: SecretStr | None = None
server_key_users: tuple[str, ...] = ()          # uids or emails; "*" accepted, logged as a warning at start
ocr_model: str = "claude-sonnet-5-5"
ocr_monthly_cap_usd: float = 20.0               # global, server key
ocr_user_monthly_cap_usd: float = 10.0          # per user, server key
ocr_price_in_per_mtok: float; ocr_price_out_per_mtok: float   # defaults: list price of ocr_model
ocr_max_tokens: int = 4096

# ocr/prompt.py
@dataclass(frozen=True)
class Prompt: text: str; version: str            # version = "v<N>" from the first line of prompt.md, stored with every transcript
def load_prompt() -> Prompt                       # reads src/scriptorium/ocr/prompt.md once; §8.2 rules live in the file

# ocr/modes.py
Mode = Literal["user", "server", "lazy"]
@dataclass(frozen=True)
class Resolution:
    mode: Mode
    reason: str            # "user_key" | "server_key" | "no_key" | "server_key_not_allowed" | "cap_reached_global" | "cap_reached_user" | "user_cap_reached"
    api_key: SecretStr | None     # never logged, never returned by a tool; dropped after the call
def resolve(settings: Settings, store: GlobalStore, secrets: SecretBox, ledger: SpendLedger, uid: str, now: datetime) -> Resolution
# order: user key (and the user's own cap) → server key if allowed and both caps have room → lazy. §8.1.

# ocr/ledger.py — global DB table ocr_spend(id, uid, mode, model, input_tokens, output_tokens, cost_usd, at); index (uid, at), (at)
class SpendLedger:
    def record(self, *, uid: str, mode: Mode, model: str, input_tokens: int, output_tokens: int, cost_usd: float, at: datetime) -> None
    def month(self, *, uid: str | None, mode: Mode | None, at: datetime) -> Spend   # Spend(cost_usd, pages, input_tokens, output_tokens) for the UTC calendar month of `at`; uid=None = everyone
    def allowance(self, settings: Settings, store: GlobalStore, uid: str, at: datetime) -> Allowance
    # Allowance(global_remaining_usd, user_remaining_usd, own_remaining_usd | None, degraded: Literal[False] | "global" | "user" | "own")
def cost_usd(settings: Settings, input_tokens: int, output_tokens: int) -> float    # includes thinking tokens in output

# ocr/client.py — the only importer of `anthropic`
@dataclass(frozen=True)
class Transcription: text: str; model: str; input_tokens: int; output_tokens: int; stop_reason: str
class OcrClient:
    def __init__(self, api_key: SecretStr, *, model: str, max_tokens: int, timeout_s: float = 120.0) -> None
    def transcribe(self, png: bytes, prompt: Prompt) -> Transcription       # one Messages call; 3 retries with backoff on 429/529/overload; raises OcrUnavailable (backoff) / OcrRefused (verdict: image rejected, auth failed)
    def submit_batch(self, items: Sequence[BatchItem], prompt: Prompt) -> str   # Message Batches; BatchItem(custom_id, png); returns batch id
    def batch_status(self, batch_id: str) -> BatchStatus      # BatchStatus(state: "in_progress"|"ended"|"canceling"|"expired", counts)
    def batch_results(self, batch_id: str) -> Iterator[tuple[str, Transcription | OcrItemError]]   # custom_id → result; streams, never loads the whole file
    def validate_key(self) -> None                            # cheapest possible call; raises OcrRefused("bad_key")
# Image handling: PNG longest side ≤ 2576 px, produced by a PNG-to-PNG downscale helper next to library/render.page_image_bytes (G adds it, same Lanczos/deterministic rules); the request never carries document names or paths.

# users/store.py additions (global migration)
#   users.ocr_cap_usd REAL NULL               — the user's own cap on their own key (set_claude_api_key(monthly_cap_usd))
class GlobalStore:
    def set_server_key_allowed(self, uid: str, allowed: bool) -> None
    def set_user_ocr_cap(self, uid: str, cap_usd: float | None) -> None
# SecretBox name for the user's key: "claude_api_key". set_claude_api_key calls OcrClient.validate_key before sealing and
# records nothing about the key beyond its presence; clear_claude_api_key forgets it and leaves transcripts and spend rows alone.
```

Tools in F: `set_claude_api_key(key, monthly_cap_usd=null)`, `clear_claude_api_key()`, `admin_allow_server_key(user, allowed)` (`user` is the 16-character uid shown by `account_status`/audit, never an email; the admin tool module is new and role-gated like the spike admin tool). `account_status.ocr` grows `mode`, `reason`, `spend_this_month` and the applicable caps (F edits the field; G fills `queue` numbers).

### 3.2 Transcription pipeline (G)

```python
# jobs/kinds.py
@dataclass(frozen=True, slots=True)
class OcrSpec: document: str; page: str; hash: str; generation: int; force: bool = False
    # kind "ocr"; key f"ocr:{acct}:{document}:{page}:{hash}"; payload validated like RenderSpec
OCR_KINDS = ("ocr", "ocr_batch")   # "ocr_batch": one job per submitted Batches id, payload {"batch_id", "items": [custom_id → OcrSpec]}, polled with backoff until ended

# ocr/debounce.py — §7 step 6
def is_stable(page: PageRow, *, now: datetime, previous_poll_at: datetime | None) -> bool
    # True when stable_since ≤ previous_poll_at (unchanged across two complete polls) or now − stable_since ≥ 30 min

# library/index.py — account migration V5 (G owns the step): transcripts.prompt_version TEXT NULL
# `settings` (V3) is on main: Index.get_setting(key) -> str | None, Index.set_setting(key, json_text | None)
class Index:
    def put_transcript(self, document, page_id, content_hash, text, model, mode, *, prompt_version: str | None = None,
                       input_tokens: int | None = None, output_tokens: int | None = None, cost_usd: float | None = None) -> None
    def pages_without_transcript(self, *, folders: Sequence[str], recursive: bool = True, limit: int) -> list[PageRow]   # live documents only
# settings keys: "auto_ocr" = {"enabled": bool, "folders": [uuid, …]}   (set_auto_ocr)
```

Worker (G): after each `ok`/`partial` poll, when `resolve(...)` is `user` or `server`, scan `pages_without_transcript` under the auto-OCR folders, enqueue `ocr` jobs for pages that `is_stable`, batching them: more than `OCR_BATCH_MIN` (10) pages at once go through `submit_batch` as one `ocr_batch` job, fewer run as single `ocr` jobs; both paths call `SpendLedger.record` per page and `put_transcript` with `prompt_version`. A page whose hash changed before its job ran is skipped (`stale`, not an error). Caps are checked before every submission through `allowance`; a degraded allowance stops enqueuing and `ocr_status` says why. Per-user OCR jobs are leased fairly: at most `OCR_USER_CONCURRENCY` (2) leased `ocr` jobs per uid at once.

Tools in G: `transcribe(document, pages=[], force=false)` (user/server: enqueue, return `{jobs, mode}`; lazy: return the images with `LAZY_INSTRUCTION`, at most 3 pages per call, else `invalid_argument` with the count), `ocr_status()` (queue depth per kind, dead count, last error reason codes without content, spend against each applicable cap from F's ledger), `set_auto_ocr(folders, enabled)`, and the `read_page` mode switch: with a user or server key a cache miss enqueues `ocr` and waits `PENDING_WAIT_SECONDS` before `pending`; in lazy mode it behaves as in M1.

### 3.3 Search and tags (H)

```python
# library/tags.py
MAX_TAGS_PER_DOCUMENT = 100; MAX_TAG_LEN = 64
@dataclass(frozen=True)
class TagSet: document: frozenset[str]; pages: Mapping[str, frozenset[str]]     # page UUID → names
def parse_tags(content: Mapping[str, Any]) -> TagSet
    # `.content` "tags": [{"name": …}], "pageTags": [{"name": …, "pageId": …}]; names NFC-normalised, stripped, control characters → rejected,
    # over-long → truncated to MAX_TAG_LEN, more than MAX_TAGS_PER_DOCUMENT → the first 100 by sort order and a counted skip; page ids not in cPages → dropped
# library/pages.py
def content_tags(paths: AccountPaths, version: Version) -> TagSet          # same bounded JSON read as page_sources

# library/index.py — account migration V4
#   tags(document, page NULL, name, PRIMARY KEY (document, page, name)) WITHOUT ROWID; index (name)
class Index:
    def replace_tags(self, document: str, tags: TagSet) -> None          # whole replace, inside the commit transaction
    def tags_of(self, document: str) -> TagSet
    def children(self, parent, *, include_trash, include_deleted, tags: Sequence[str] | None = None, cursor, limit) -> Page[DocumentRow]
        # tags set ⇒ any-of on document tags and recursive below `parent` (§11.2); DocumentRow gains `tags: tuple[str, ...]` (joined, sorted)
    def search(self, query: str, *, folder: str | None = None, tags: Sequence[str] | None = None, cursor, limit) -> Page[SearchHit | DocumentHit]
        # folder ⇒ subtree filter on doc_path; tags ⇒ page hit matches on page tags ∪ document tags, document hit on document tags;
        # SearchHit/DocumentHit gain `tags`; tag names are indexed as text in transcripts_fts via doc_tags column (or matched by name fallback)
    def documents_tagged(self, names: Sequence[str]) -> frozenset[str]     # for I's scope filter: documents (and folders) carrying any of `names`
```

Worker hook (H, in `worker.refresh`): after `replace_pages`, `replace_tags(uuid, content_tags(...))` in the same transaction. Tools in H: `search(query, folder=null, tags=null, limit, cursor)` (new; results per §11.2, snippets, `current` flag, `tags`), `list(..., tags=null)`. Tag names are display data: tools return them, nothing joins them into paths or logs.

### 3.4 Sync scope, wake-up, priority, admin and destructive tools (I)

```python
# cloud/scope.py
@dataclass(frozen=True)
class SyncScope: mode: Literal["all", "allow", "deny"]; tags: frozenset[str]
def load_scope(index: Index) -> SyncScope        # settings key "sync_scope"; default all
def in_scope(scope: SyncScope, doc_tags: frozenset[str], ancestor_tags: frozenset[str]) -> bool
# cloud/sync.py: a document outside the scope has its `.metadata` and `.content` fetched (small, needed for the row and the tags),
# its row upserted with excluded_at set and state "excluded", and nothing else downloaded; a document that enters the scope later
# is committed like a new one; narrowing never tombstones or removes anything already archived (§11.1). Folder tags apply to
# descendants: the planner resolves ancestors from the metadata it already has in the listing.
# documents.excluded_at TEXT NULL is on main (V3); I adds `excluded_at` to DocumentRow, the commit path and `state` in tool results

# cloud/notifications.py — only if S7 passes; otherwise the file is not created and NOTIFICATIONS is rejected at start
class NotificationListener:
    async def run(self, stop: asyncio.Event) -> None       # reconnect with backoff; on any event call `on_event()`; never parses beyond event name and generation
# worker: AccountLoop.poke() schedules a poll now if the last one ended ≥ NOTIFY_MIN_GAP_SECONDS (10) ago; budgets and the two-listings rule unchanged

# jobs/queue.py — global migration: jobs.priority INTEGER NOT NULL DEFAULT 0; lease orders by priority DESC, created_at, id
class JobQueue:
    def enqueue(self, ..., priority: int = 0) -> JobRow
    def deprioritise(self, *, uid: str, acct: str, document: str, kinds: Sequence[str]) -> int   # queued/failed jobs whose payload.document matches → priority −1
# worker: tombstoned (and trashed) documents' renders are enqueued at −1 and deprioritised when a document is tombstoned

# tools/_conventions.py — the two stubs become real
def issue_confirmation_token(settings: Settings, *, uid: str, tool: str, args: Mapping[str, Any], now: datetime) -> str
def verify_confirmation_token(settings: Settings, token: str, *, uid: str, tool: str, args: Mapping[str, Any], now: datetime) -> None
    # HMAC-SHA256 under a key derived from the newest master key (HKDF, info "confirmation"); payload = uid, tool, canonical JSON of args, issued_at;
    # 10 min validity; a mismatch is `refused` with reason malformed|expired|not_bound|used; tokens are single-use within validity (global table confirmations(sha256, used_at))

# cloud/sync.py: on a tripped breaker persist sync_state.anomaly_candidates (JSON list of UUIDs, never logged);
# tools/admin_sync.py (landed there, not in tools/admin.py): admin_clear_anomaly(user, reason, allow_mass_delete=false) → clears anomaly/anomaly_reason, and with allow_mass_delete builds
# the approval from anomaly_candidates with mass_delete_approval(); refused when no candidates are recorded. reason is audited as a
# short code or free text ≤ 200 chars (not content: it is the administrator's own words, still never logged in full — audit stores its length).
```

Tools in I: `set_sync_scope(mode, tags=[])`, `disconnect_remarkable(keep_archive=false, confirmation_token)` (dry run lists counts only: documents, pages, blobs, bytes; the execute step deactivates, forgets the token and **moves** the account directory to `users/<uid>/removed/<acct>-<ts>/`; nothing under it is deleted, which keeps the hard rule and leaves `admin_gc_blobs` (M5) as the only way bytes leave `/data`), `admin_clear_anomaly`. Review leftovers owned by I: null `hash_root` when `archive_journal_source_missing` fires; `gone` render results stop consuming reruns (complete the job with `{"state": "gone"}` and skip the family on revive); `SyncOnce._library_sets` → `Index.document_uuids`; log the suffix shape of skipped names; pin GitHub actions and base images by digest (CI, coordinator reviews); the abandoned-poll-thread future and `_check_length` items.

## 4. Rules for every agent

Everything in `04-m1-work-breakdown.md` §4 applies. In addition:

- **No live Claude API calls in tests.** `OcrClient` is faked under `tests/fakes/ocr.py` (shared); the only live path is `tests/live/test_ocr_live.py` behind `SCRIPTORIUM_LIVE_OCR_TEST=1` with the operator's key, run by the coordinator, capped at five pages of the S3 fixture.
- **Transcripts are note content.** They never appear in logs, job payloads, error strings, audit identifiers or test fixture names; tests use the S3 fixture pages and synthetic text.
- **Keys** exist as `SecretStr` and are passed to `OcrClient` only; no `repr`, no exception message, no settings dump includes them (`config.py` already redacts; keep it so). A tool response never echoes a key or its prefix.
- **Cloud strings** (tag names, batch custom ids built from UUIDs) pass the same discipline as file names: UUID allowlist for identifiers, bounded and normalised for display strings, never joined into paths.
- **Migrations** are append-only steps; F/G/H/I own the step numbers named in §3 and nobody renumbers. When a step lands on `main` before yours, rebase and renumber yours above it.
- **New dependency:** `anthropic` (MIT) only, pinned with an upper bound, added by F with `scriptorium licence-check` output in the hand-back; the coordinator records it in Architecture §4.4. No other new runtime dependency without the same.
- Security-critical workstreams (F, I) run the `security-guidance` plugin's review before hand-back and report what it found.

## 5. Acceptance mapping

| M2 check | Proven by |
| --- | --- |
| 1 one transcription per content hash across 10 polls; `force=true` re-transcribes | G: fake client call count over a 10-poll harness; F ledger rows; live: `ocr_status` after a day on the operator's stack |
| 2 no OCR while a page keeps changing; starts once stable | G `is_stable` unit tests; worker harness with a hash that changes every poll |
| 3 $1 test cap: spend stops, mode `lazy`, `read_page` returns the image | F `allowance` tests; G worker stops enqueuing; live with `OCR_USER_MONTHLY_CAP_USD=1` on the operator's user |
| 4 `ocr_status` spend within 5 % of the console over a day | live, operator reads the Claude console; F `cost_usd` includes thinking tokens |
| 5 `search("retro")` returns the page with a snippet in < 500 ms | H tool test over the fixture index; latency benchmark in CI |
| 6 user key preferred over server key | F `resolve` tests; live: operator sets a key, `account_status.ocr.reason = user_key` |
| + tags listed and filterable | H: fixture notebook gets tags on the tablet (operator), `list(tags=)` and `search(tags=)` live |
| + `set_sync_scope` excludes and never deletes | I cassette with tagged documents; live: deny a tag, confirm `excluded` rows and untouched archive |
| + poll on notification (if S7 passes) | S7 write-up; live: tablet edit visible within 30 s |
| + tombstoned renders yield | I queue ordering test; live: delete a large document while renders are queued, live pages render first |
| + `disconnect_remarkable(keep_archive=false)` two-step | I token tests (bound to user and args, single use, expiry); live dry run then execute on the scratch link |
| + `admin_clear_anomaly` | I: cassette that trips the breaker, clear without approval re-trips, clear with approval tombstones once |

## 5a. Integration log

- **H merged 2026-10-18 (`e352ac5`)**, 1411 tests. Deviations accepted and recorded in Architecture §6.1/§11.2: `page=''` for document tags; the 100-tag bound counts page tags; `folder` is a UUID subtree; `doc_tags` on transcripts feeds the FTS. Follow-up for the review wave: a page tag on an untranscribed notebook is findable by neither text nor filter; let document hits in the name fallback match page tags too.

- **F merged 2026-10-18 (`c89dc64`)**, 1618 tests, `anthropic` 1.x added (MIT, licence-check passed). Deviations accepted and recorded in Architecture §4.4/§6.2/§8/§11/§12: `cost_usd(..., batch=)` at half price; `allowance(..., mode=)`; `OcrRefused` carries tokens; own-cap-reached falls through to a permitted server key; `key_unreadable` fails closed; key checks rate-limited; `users.server_key_email_sha256`; `account_status.user`. The security plugin reviewed only 5 of 13 commits (hourly limit); the coordinator read the client, modes and key tools by hand; the adversarial review covers the rest.
- **G merged 2026-10-18 (`deef5ca`)** after the wave-2 binding to F, 1731 tests. Recorded in Architecture §6.1/§6.2/§7/§8/§11: `ocr_pending_batches` and `ocr_key_refused` settings, `prompt_version` in the batch payload, `record_dead`, the 6-hour key pause, image pre-check, shared `tools/_ocr.py` summary. The coordinator's mechanical rebase had dropped the key-tool registrations; the agent restored them (`a958a4c`), a reminder that `tests/tools/test_acceptance.py` must list every tool. Edge cases left for the adversarial review: spend for batch item errors may be recorded twice if a batch job crashes mid-collection and re-runs; a malformed batch id from the API is paid for and lost; re-runs after a refused key count against `RERUN_CAP`.
- **I merged 2026-10-18 (`d90b7c5`)**, 1892 tests. Recorded in Architecture §6.1/§6.2/§7/§11.0/§11.1/§11.6: `decide(scope, own, ancestors, complete)` replaces `in_scope` and answers None on missing information (fail closed, `scope_tags_unknown`/`scope_ancestor_unknown`); only folder-shaped `CollectionType` items are exempt from the scope; `scope_excluded` seen-map and `scope_applied` with a `pending` marker; page tags do not decide the scope; `admin_clear_anomaly` lives in `tools/admin_sync.py`, acts on the active account only, withdraws unspent approvals, refuses `no_anomaly|not_mass_delete|no_candidates`; an approval is spent only if unchanged since the poll read it; tokens are `base64url(issued_at || mac)`, reason `used` (the breakdown's `unused` was a typo), spent before acting, `refuse_spent_token` checked before the target; disconnect holds the connect lock and the poll lock, refuses `busy|move_failed|used`, targets the active or latest kept archive; `accounts.removed_at`; `Services.evict_index`; `AccountLoop.poke()` is the S7 seam and `cloud/notifications.py` is not created; gone renders complete `{"state": "gone"}`; renders of excluded documents are low priority too; `name_shape` logging; `Content-Length` parsed as bounded ASCII digits. Tests drive the real worker against `FakeCloud` rather than cassettes. The coordinator resolved the rebase conflicts with G by hand (lease SQL keeps the per-user cap filters and adds `priority DESC`; both `record_dead` and `deprioritise` kept). Left for the adversarial review: the `pages.write_png` `mkdir(parents=True)` window that can recreate a moved account directory (needs `library/pages.py` to refuse to create the account root); the worker's cached `AccountContext` pointing at the moved index until its loop stops; `evict_index` and the connect lock are per process; a gone version repaired at the same generation stays `not_found`. To confirm on a live listing: real folder indexes list exactly `.metadata` and `.content`.
- **Adversarial review 2026-10-19** (three read-only Fable reviewers at xhigh over `453f51d..db6883d`: OCR; scope/tokens/removal/admin; tags/search/worker). No HIGH finding outside OCR. Confirmed and fixed in three waves: **removal path (`c2b2c92`)**: a fsync failure after a successful rename un-removed an account whose archive was already in `removed/` (now decided from the filesystem); the token is forgotten after the move, not before; a symlinked `removed/` pointing inside the user directory was accepted; `Services.index` checked `removed_at` only on a cache miss; `anomaly_reason` was returned verbatim (now shape-checked both ways); a two-concurrent-executes test added. Left as noted: the lease index `jobs_lease_order` is never chosen by the planner (`jobs_ready` + temp B-tree); harmless write cost, drop or restructure in a later step; a small window between the `removed/` re-check and the rename remains (needs `dst_dir_fd`, which Windows dev builds lack); `account_status` applies the same reason shape check (coordinator, same wave). **Tags, search, scope and worker (`ee6b423`)**, 1958 tests: `write_png` never recreates the account root; page tags make untranscribed documents findable by `search` and `list(tags=)` (the H follow-up); `recent` reports `excluded`; Cf characters stripped from tag names (ZWJ/ZWNJ/variation selectors kept); an absent `.content` means unknown tags, not "no tags" (deny mode was fail-open); an unfetched `CollectionType` is exempt from the scope only when its archived version is folder-shaped; excluded rows keep the cloud type and get their document tags indexed, so `unmatched_tags` sees them; newly excluded renders deprioritised and re-included documents refreshed (`SyncReport.newly_excluded`/`included`); the worker closes and refuses a removed account's context; `list(tags=)` stays out of tombstoned folders. Follow-ups noted: a hit found by a page tag shows `tags: []` (a `matched_page_tags` field would help); a job already running when its context is evicted may hit one closed-connection error before it sees the account gone; subdivision-flag emoji lose their tag characters (deliberate). **OCR (`ba5c6c0`)**, 2025 tests: the HIGH finding (deferrals and key refusals spent `RERUN_CAP`, so three waits capped a page for life) is fixed by `JobQueue.defer` (back to `queued`, attempt returned, `next_run_at` = when the cause can have changed) plus `wake_deferred` from the key tools, the server-key grant, `transcribe` and a scan with room; `read_page` neither revives dead `key_*` jobs nor wakes deferred ones and answers lazily during the 6-hour hold; batch spend is idempotent per (uid, batch id, custom id) through the new `ocr_spend batch items` global step and is recorded whenever the API billed; a submission is written with a nonce before the request and a lost answer is recovered from the key's batch listing (one page of 100; a key refused or gone for 26 h gives up and may pay twice, logged); batch admission estimates output at `OCR_MAX_TOKENS`; `SERVER_KEY_USERS` text corrected; the runner treats a removed account, a refused context and a moved directory as gone. Left: `ocr_status` does not show pending submissions; single jobs queued while a batch is in flight do not count that batch's not-yet-recorded worst case against the room; re-noting a refusal when a batch's results are re-read restarts the hold; the reviewer's probe script is stale against the nonce-tagged custom ids (its scenarios live on as tests). The review's `tests/live` run against a real key is still to do.

## 6. Order of work and what the operator provides

1. Coordinator: Architecture edits for the seams above (§6.1 `settings`, `tags`, `excluded_at`, `prompt_version`; §6.2 `ocr_spend`, `users.ocr_cap_usd`, `jobs.priority`, `confirmations`; §7 scope, wake-up, priority; §11 signatures; §12 `OCR_MAX_TOKENS`, `OCR_BATCH_MIN`, `OCR_USER_CONCURRENCY`, `NOTIFICATIONS`, `NOTIFY_MIN_GAP_SECONDS`), then spawn F, H, I (wave 1) and G against fakes.
2. S7 spike, read-only with the development token, during wave 1; needs about half an hour of tablet and desktop-app activity from the operator.
3. Wave 2: G's worker and tool wiring after F and I merge; then the adversarial review; then deploy.
4. From the operator at acceptance time: a Claude API key for the server mode and the cap to set on it (lazy and user-key modes need nothing), a tag or two on the test notebook, the claude.ai checks in §5, and a day of `ocr_status` versus the console for check 4.
