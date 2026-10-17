# Milestones

Each milestone ends with a tagged release (`0.<n>.0`), a green CI run including the licence gate, and the acceptance checks below recorded in the PR. "Done" means every check passed or has a written, operator-approved exception.

---

## M1 — Safe archive, single user (`0.1.0`)

**Gated by:** S0, S2, S3, S4, S5, S6.

**Contents.**

- Project skeleton: `uv`, ruff, mypy `--strict`, pytest, Dockerfile (python:3.12-slim, non-root, read-only rootfs), compose example, GitHub Actions (lint, test, build, SBOM, `pip-licenses` gate).
- `config.py` with every §12 variable; refuse to start without `OIDC_ALLOWED_USERS` (or the explicit opt-out), without `MASTER_KEYS`, or with `/data` on a FUSE/network filesystem.
- Auth: PRM document, 401/403 contract, JWT validation (`aud` membership, pinned algs, JWKS cooldown), `(iss, sub)` user keying, `email_verified` on email allowlists, admin claim. Pre-registered client docs; Keycloak realm JSON as an optional profile.
- Global and per-account SQLite schemas with migrations (`user_version`); leased job queue.
- Sync worker exactly as §7: full-listing requirement, staged + verified + atomic commits, `history`, tombstones with two-poll rule, circuit breaker, un-tombstone, failure classes, safe mode, trash handling.
- Rendering (§8.3) with S3's geometry; archive PNG at 1×.
- Tools: `connect_remarkable`, `disconnect_remarkable`, `account_status`, `list`, `recent`, `read_page` (lazy OCR path), `read_document(markdown)`, `page_image`, `versions`, `trash`, `save_transcript`, plus `_conventions` (identifiers, pagination, error model, confirmation tokens, annotations).
- Audit log with identifiers only; structured logs.
- Cloud cassettes (scrubbed) and the fixtures from S3.
- Operator README: deploy, bring-your-own-issuer requirements, trust model, data-policy note.

**Acceptance.**

| # | Check |
| --- | --- |
| 1 | Fresh deploy from the compose example + an external issuer: claude.ai (web and mobile) and Claude Code log in; a user not in `OIDC_ALLOWED_USERS` gets 403 |
| 2 | After `connect_remarkable`, the whole scratch library is on disk in xochitl layout within 15 min; `list` matches the tablet |
| 3 | Edit a page on the tablet → new PNG and `history` row within one poll; the old blob is still present and `read_page(version=…)` returns it |
| 4 | Delete a document on the tablet → tombstone after two polls; **no file is unlinked**; `trash()` shows it with `deletion_cause` |
| 5 | Inject a partial listing (cassette) → zero tombstones, `last_full_listing_ok=false` |
| 6 | Inject a root with 60 % of documents missing → deletes halt, `anomaly="mass_delete"`, `account_status` says so |
| 7 | Kill the worker mid-download (`SIGKILL`) → on restart no partial document is visible, staging is swept, sync completes |
| 8 | Token-endpoint outage (cassette 503) → backoff, no `needs_relink`; a rejected token ×3 → `needs_relink` |
| 9 | p95 latency of `list`/`read_page` on cached data < 2 s over 100 calls |
| 10 | `pip-licenses` over the built image: no GPL/AGPL; image runs with read-only rootfs |
| 11 | Audit rows contain no arguments or content (grep test over a day of usage) |
| 12 | Open question 5 (organisational data policy) answered and recorded before any library holding such notes is linked |

---

## M2 — OCR modes and search (`0.2.0`)

**Contents.** `set_claude_api_key`/`clear`, server key with `SERVER_KEY_USERS` + `admin_allow_server_key`, mode resolution (user → server → lazy), `transcribe`, `ocr_status`, auto-OCR with `set_auto_ocr`, debounce (stable ≥ 2 polls / 30 min), Message Batches for background jobs, global + per-user caps with degrade-to-lazy, cost accounting from `usage`, `transcripts_fts` and `search`, prompt file with version stamping.

**Acceptance.**

| # | Check |
| --- | --- |
| 1 | A page is transcribed exactly once per content hash across 10 polls; `force=true` re-transcribes |
| 2 | While a page is being edited (hash changes every poll), no OCR job is enqueued until it stabilises |
| 3 | With a $1 test cap, spend stops at the cap, mode reports `lazy`, on-demand `read_page` returns the image path instead of calling the API |
| 4 | Spend reported by `ocr_status` is within 5 % of the Claude console for the test key over a day |
| 5 | `search("retro")` returns the page whose transcript contains it, with a snippet, within 500 ms |
| 6 | A stored user key is used in preference to the server key for that user |

---

## M3 — Push (`0.3.0`)

**Gated by:** S1 for `push_ink` only.

**Contents.** `push_markdown` (markdown-it `html=False`, WeasyPrint in a resource-limited subprocess, deny-all `url_fetcher`, bundled fonts), `push_file(url|data_base64)` with `push/fetch.py` (SSRF guard, allowlist / confirmation token, audit of host), size caps, per-user push rate limits, `push_status`, worker-side idempotent upload with fixed UUID; `push_ink` behind `INK_SIDECAR_URL` and the `ink` profile, `scriptorium-ink` as a separate repo/image.

**Acceptance.**

| # | Check |
| --- | --- |
| 1 | Markdown containing `<img src="file:///etc/passwd">`, `<img src="http://scriptorium-worker:9100/metrics">`, `<link rel=stylesheet href=…>` renders with none of them fetched (sandbox test asserts no network/file access) |
| 2 | `push_file(url=…)` to `127.0.0.1`, `10.0.0.1`, `169.254.169.254`, `100.64.0.1`, `[::1]`, an IPv4-mapped address, and a host that redirects to any of these — all refused with `refused` |
| 3 | `push_file(url=https://example.org/x.pdf)` with an empty allowlist returns `confirm_required` with the host in the summary; with the token it proceeds; the audit row has the host |
| 4 | A pushed Markdown PDF and a pushed PDF open on the tablet in the chosen folder |
| 5 | Running the same push job twice (crash after blob upload, before root commit) produces one document |
| 6 | A 300 KB inline payload is rejected with `invalid_argument` and the hint names `url` |
| 7 | (If S1 passed) `push_ink` of an SVG yields erasable strokes on the tablet |

---

## M4 — Backup and restore (`0.4.0`)

**Gated by:** S1, S5.

**Contents.** `snapshots()` for `host` (read-only mount, naming convention) and `restic` (`VACUUM INTO`, independent password, `admin_snapshot_now`), `restore(scope, from, mode)` with dry-run confirmation tokens, parents-first batches, per-UUID progress, resume; `export` with signed download URLs and `/dl/<token>`; `admin_purge_tombstones` and `admin_gc_blobs` (dry run first).

**Acceptance.**

| # | Check |
| --- | --- |
| 1 | Factory-reset the scratch tablet (or use a second scratch account) → `restore(scope=all, from=archive)` rebuilds the full tree, folders included, with original UUIDs; a second run is a no-op (zero uploads) |
| 2 | Kill the worker mid-restore → restart resumes; final root has no duplicates |
| 3 | `restore(mode=overwrite)` without a token returns a summary and token; with a token bound to different arguments it is refused |
| 4 | `restore(from="snapshot:<id>")` with an id not in `snapshots()` is refused; nothing reaches a shell (grep for `subprocess` with `shell=True` fails CI) |
| 5 | `export(pdf)` from claude.ai mobile yields a URL that downloads in the browser within 10 min and 404s after |
| 6 | restic restore of a backup taken under load yields a passing `integrity_check` |
| 7 | `admin_purge_tombstones` dry run lists exactly the eligible UUIDs; executing removes those and nothing else |

---

## M5 — Multi-user and admin (`0.5.0`)

**Contents.** Quotas (`DEFAULT_QUOTA_GB`, `admin_set_quota`, behaviour at quota: sync continues for metadata, new blobs paused, status warns), `admin_status`, `admin_users`, `admin_disable_user` (revokes PATs, stops polling), `admin_delete_user`, `admin_transfer_link`, `admin_clear_anomaly`, `admin_audit`, PATs (`scr_`, ≤ 90 d, no admin), `forget_me` scope (data, secrets, jobs, PATs, registry rows, audit identifiers), `connect_remarkable(replace=true)` with inactive account directories, operator documentation.

**Acceptance.**

| # | Check |
| --- | --- |
| 1 | Two users, two scratch reMarkable accounts: every tool called as user A never returns B's data (automated isolation suite over all tools) |
| 2 | Linking A's reMarkable account as B is refused with the generic message; `admin_users` shows the holder; `admin_transfer_link` moves it |
| 3 | A PAT created by an admin cannot call any `admin_*` tool |
| 4 | `forget_me` leaves no file under `users/<uid>` and no rows keyed on the uid in any table except retention-bound audit stubs |
| 5 | Quota hit: `account_status` warns, sync does not tombstone or delete anything, `list` still works |
| 6 | `connect_remarkable(replace=true)` keeps the old account readable under `list(account=…)` and causes zero tombstones |

---

## M6 — Hardening and 1.0 (`1.0.0`)

**Contents.** Prometheus metrics on `METRICS_BIND`, rate limits on `/dl/*` and the 401 path, JWKS cooldown tests, multi-arch image, Dependabot, SBOM publication, threat-model walkthrough against §14, operator and user docs (including the trust model and data-policy notes), release checklist, `docs/Spikes/` complete.

**Acceptance.**

| # | Check |
| --- | --- |
| 1 | Images for amd64 and arm64 pass the full test suite |
| 2 | 7-day soak on the operator's host: zero anomalies, zero `needs_relink`, sync lag p95 < 15 min |
| 3 | Every §14 bullet maps to at least one automated test or a documented manual check |
| 4 | A fresh operator can deploy from the README alone against their own issuer in under an hour (timed walkthrough) |
| 5 | Decision recorded on open question 7 (IP allowlist vs. bearer-only) |
