# Adversarial review of Architecture v0.1 — dispositions

Review date: 2026-10-17. Reviewer verified claims against the Claude connector docs, Keycloak source (`TrustedHostClientRegistrationPolicy`, `RedirectUtils`), the `remarkable-mcp` source, PyPI licence metadata and reMarkable's free-tier support text. Each finding below is marked **Accepted** (architecture changed), **Accepted-partial**, or **Rejected** with a reason, and points to the section of v0.2 that now covers it.

Severity key: B = blocker, M = major, m = minor.

## 1. OAuth / OIDC

| # | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| 1.1 | B | Keycloak Trusted Hosts has no CIDR support; cannot be limited to Anthropic's range; Claude Code registers from the user's machine | **Accepted.** Anonymous DCR dropped. Two pre-registered public clients; DCR and CIMD evaluated in S2 (§5.3) |
| 1.2 | M | Anonymous-DCR default policies (Max Clients 200, Allowed Scopes, Full Scope Disabled) not addressed | **Accepted.** `scriptorium` is a realm default scope with audience + realm-roles mappers; DCR policies listed under the S2 alternative (§5.3) |
| 1.3 | M | IP allowlist breaks Claude Code and the login flow; login needs more than the auth endpoint | **Accepted.** Per-path proxy table; bearer auth is the control, allowlist optional (§13); open question 7 |
| 1.4 | M | Admin console and master realm publicly reachable | **Accepted.** `KC_HOSTNAME_ADMIN`, proxy denies `/admin` and `/realms/master` (§5.3, §13) |
| 1.5 | M | "Refresh rotating, 30 days" is not a Keycloak setting; reuse 0 breaks concurrent refresh | **Accepted.** Exact lifetimes, Revoke Refresh Token on with Max Reuse 1; S2 tests (§5.3) |
| 1.6 | M | `scopes_supported` undefined | **Accepted.** PRM JSON fixed to `["scriptorium"]`; `scope=` in WWW-Authenticate (§5.1) |
| 1.7 | m | `resource` parameter ignored by Keycloak; `aud` is an array | **Accepted.** Membership check; audience from the mapper only (§5.1) |
| 1.8 | m | Floating `keycloak:26` tag; feature availability depends on version | **Accepted.** Pinned version from S2; realm JSON via `--import-realm` (§5.3, §13) |
| 1.9 | m | 401 contract incomplete | **Accepted.** `invalid_token`, middleware before the SDK, byte-exact `resource`, POST initialize (§5.1) |
| 1.10 | m | "Entra does not work" overstated | **Accepted.** Reworded; Entra works with a pre-registered client (§5.2). Keycloak remains the reference and is now an optional profile |
| 1.11 | m | PATs: no max TTL, undefined admin rights, no prefix | **Accepted.** `scr_` prefix, ≤ 90 d, never admin, revoked on disable/forget (§5.4) |

## 2. Sync / archive

| # | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| 2.1 | B | Free-tier aging makes the cloud drop documents; v0.1 tombstoned and purged them after 180 d, destroying the only copy | **Accepted.** Tombstones never auto-purge; `admin_purge_tombstones` with dry run only; `deletion_cause` heuristic; un-tombstone on reappearance (§7) |
| 2.2 | B | Free-account API access unverified; upstream README says Connect required | **Accepted.** New spike S4 |
| 2.3 | M | False tombstones on partial listing / protocol change | **Accepted.** Full-listing requirement, two-poll rule, circuit breaker, generation-regression anomaly, safe mode (§7) |
| 2.4 | M | Download atomicity undefined | **Accepted.** Staging, hash verification, fsync, atomic swap, single transaction, startup sweep (§7 step 3) |
| 2.5 | M | No version history; erased page lost in 10 min | **Accepted.** Retained content-addressed blobs + `history` table; `library/` as materialised view; `versions` tool; manual GC only (§6) |
| 2.6 | M | Relinking a different account mass-tombstones and mixes archives | **Accepted.** `accounts/<acct>/` directories, `replace=true`, inactive accounts stay readable (§6, §11.1); identity derivation moved to S0 |
| 2.7 | M | Retry claim only true if mutation is idempotent; upstream appends and mints UUIDs | **Accepted.** Upsert by UUID, caller-supplied UUID (fork), `broadcast: true` (§7, §4.3) |
| 2.8 | M | Auto-OCR re-transcribes while writing | **Accepted.** Stable ≥ 2 polls / 30 min; Batches API (§7 step 6) |
| 2.9 | m | Any auth failure stops polling forever | **Accepted.** Failure classes; `needs_relink` only after 3 consecutive token rejections (§7 step 7) |
| 2.10 | m | Double retry layers; 16 workers default | **Accepted.** Single retry layer, `SYNC_WORKERS=4` (§7) |
| 2.11 | m | Trash undefined; folder deletes cascade | **Accepted.** Trash semantics and parent reconstruction (§7) |

## 3. Multi-tenancy and security

| # | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| 3.1 | B | WeasyPrint fetches arbitrary resources via raw HTML | **Accepted.** `html=False`, deny-all `url_fetcher`, bundled fonts, resource-limited subprocess (§9) |
| 3.2 | M | `url` is an exfiltration channel | **Accepted.** `PUSH_URL_ALLOWLIST` or per-fetch confirmation token; host in audit (§9) |
| 3.3 | M | SSRF guard underspecified | **Accepted.** Resolve-once/pinned connect, `is_global`, redirect re-validation, byte cap, image/PDF bomb checks (§9) |
| 3.4 | M | Open defaults: empty allowlist means anyone; `*` server-key users; uid ignores `iss`; no `email_verified` | **Accepted.** `(iss, sub)` keying, `email_verified`, `OIDC_ALLOWED_USERS` required, `SERVER_KEY_USERS` empty, Keycloak self-registration off and manual broker linking (§5.2, §5.3, §12) |
| 3.5 | M | OCR cap covers only auto-OCR; global vs per-user unclear; mode order | **Accepted.** Global + per-user caps on all server-key spend; user key wins (§8.1) |
| 3.6 | M | Cloud-supplied names are untrusted; zip-slip; `snapshot:<id>` reaches a shell | **Accepted.** Filename allowlist, path containment, zip entry checks, snapshot ids from the server list, no shell (§11.0, §14) |
| 3.7 | M | Master key: no rotation, under `/data`, restic password coupled | **Accepted.** `MASTER_KEYS` MultiFernet, Docker secret outside `/data`, escrow, independent restic password (§10.1, §12) |
| 3.8 | m | One-account rule leaks display names; edge cases; no revocation API | **Accepted.** Generic refusal, `admin_transfer_link`, `replace=true`, disconnect tells the user to remove the device at reMarkable (§5.1, §11.1) |
| 3.9 | m | "Never arguments" blocks forensics | **Accepted.** Non-content identifiers logged; retention setting (§6.2, §11.0) |
| 3.10 | m | `/metrics` on the public bind | **Accepted.** `METRICS_BIND` on a separate port, never proxied (§12, §15) |
| 3.11 | m | JWKS refetch DoS; trust model unstated; forget_me vs snapshots | **Accepted.** Cooldown + pinned algs; trust model and retention caveats stated (§5.1, §14) |
| 3.12 | m | `confirm=true` is set by the model itself | **Accepted.** Dry-run confirmation tokens bound to user + arguments; `destructiveHint` (§11.0) |

## 4. Two processes, SQLite, jobs

| # | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| 4.1 | M | SQLite WAL on a FUSE user-share layer corrupts | **Accepted (generalised).** `/data` must be a local POSIX filesystem; `statfs` refusal on FUSE/NFS/SMB; S5 verifies on the operator's host (§6) |
| 4.2 | M | Contradictions: where jobs live, who writes to the cloud | **Accepted.** Jobs in the global DB; worker is the single cloud writer; server's only cloud call is the connect-code exchange (§1, §4.1, §6.2) |
| 4.3 | M | Queue semantics unspecified | **Accepted.** Full column list, `BEGIN IMMEDIATE … RETURNING` claim, leases, backoff, dead state, dedupe, cancellation, ≤ 2 s poll (§6.2) |
| 4.4 | M | Push/restore idempotency | **Accepted.** UUID and blob set fixed at enqueue; root as single commit point; batched resumable restore (§7, §10.2) |
| 4.5 | M | Snapshot hook cannot run inside the container; restic over live SQLite inconsistent | **Accepted (generalised).** `host` strategy = host-scheduled snapshots mounted read-only; restic uses `VACUUM INTO` (§10.1) |
| 4.6 | m | busy_timeout, synchronous, migrations ownership, `read_page` miss behaviour | **Accepted.** (§6.1, §11.0) |

## 5. Licensing

| # | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| 5.1 | B | `ebooklib` (AGPL) is a hard dependency; `pymupdf` is pervasive, not just rasterising | **Accepted.** Both made optional extras in the fork or the client/renderer are vendored (S0 decides); CI licence gate over the image (§4.3, §4.4) |
| 5.2 | M | `rmc` pins `rmscene<0.7`, incompatible | **Accepted.** rmc dropped (§4.4) |
| 5.3 | m | resvg-py has no PyPI licence field | **Accepted.** Source of each check recorded (§4.4) |
| 5.4 | m | "Every library must be MIT/BSD/Apache" is wrong for an image | **Accepted.** Rule reworded to Python deps in-process; system packages via SBOM (§4.4) |
| 5.5 | m | Sidecar boundary fine; native converter must be clean-room | **Accepted.** Clean-room mandate in §4.4 and `CLAUDE.md` |
| 5.6 | m | WeasyPrint system packages, fontconfig cache on read-only rootfs | **Accepted.** (§9, §13) |

## 6. Spikes and milestones

| # | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| 6.1 | M | S1 mis-scoped; rmink coupling; reuse-UUID case missing | **Accepted.** S1 rewritten around archived notebooks and the original-UUID restore case (`01-spikes.md`) |
| 6.2 | M | S0 criteria too weak | **Accepted.** No AGPL deps, no env reads, per-user cache, caller UUID, identity claim |
| 6.3 | M | S2 criteria need refresh, roles, Max Clients, Claude Code off-allowlist, mobile, mode comparison | **Accepted.** |
| 6.4 | M | Missing spikes: free tier, storage/SQLite/snapshots, client rendering | **Accepted.** S4, S5, S6 added |
| 6.5 | M | Milestones out of order (tombstones, search/FTS, default-deny, SSRF, cassettes, PATs, policy gate) | **Accepted.** M1 now contains the safety and auth hardening; search moved to M2 with FTS; SSRF/rate limits in M3; policy gate is M1 acceptance #12 |
| 6.6 | m | Golden PNGs from real notes leak content | **Accepted.** Dedicated test notebook; reference photos (S3, §16) |

## 7. Product and spec gaps

| # | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| 7.1 | M | 8 MB inline base64 unrealistic | **Accepted.** `MAX_INLINE_KB=256`; URL is the real path; documented (§9) |
| 7.2 | M | No way to download a PDF on claude.ai from a resource link | **Accepted.** Short-lived signed `/dl/<token>` URLs; the one user-facing browser path (§11.0, §13) |
| 7.3 | m | Render resolution contradictions; Move is portrait; API 2576 px limit | **Accepted.** 1× archive render, bounded `scale`, geometry confirmed in S3 (§8.3) |

## 8. What an autonomous session needs

| # | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| 8.1 | M | Contradictions (jobs, cloud writer, resolution, naming) | **Accepted.** Resolved in §4.1, §6.2, §8.3; tool names are flat (`list`, `search`, …) and the "prefixed by area" claim was removed |
| 8.2 | M | Tool contracts (identifiers, page base, pagination, errors, timestamps, annotations) | **Accepted.** §11.0 conventions; JSON schemas are written in code at M1 and generated into docs |
| 8.3 | M | Framework: SDK version, stateless/stateful, user injection, filtering, blocking client | **Accepted-partial.** Stateless Streamable HTTP; per-request user via ASGI middleware + contextvar; filtered `tools/list`; thread pool for blocking calls (§4.1, §4.3, §11.0). Exact `mcp` version pinned at S0 |
| 8.4 | M | Auth artefacts | **Accepted.** PRM JSON and 401 variants in §5.1; realm export and proxy examples under `docker/` (written in S2) |
| 8.5 | M | Rendering spec | **Accepted-partial.** §8.3 (geometry, layers, blending, compositing, templates); numbers confirmed in S3 |
| 8.6 | M | OCR spec | **Accepted.** Prompt file, `max_tokens`, thinking, retries, cost formula, Batches (§8.1, §8.2) |
| 8.7 | M | Undefined behaviours (quota, API change, account switch, trash, reappearing UUID, forget_me scope) | **Accepted.** §6, §7, §11.1; quota behaviour specified in M5 |
| 8.8 | M | Acceptance criteria not testable | **Accepted.** Measurable checks per milestone (`02-milestones.md`) |
| 8.9 | M | Guardrails for the coding session | **Accepted.** `docs/Plan/README.md` rules of engagement and `CLAUDE.md` |
| 8.10 | m | Infrastructure gaps (`<org>`, CI, registry, uid/gid, schemas, metric names, snapshot naming) | **Accepted-partial.** `ghcr.io/milkyengineer/scriptorium`, GitHub Actions, `PUID`/`PGID`, metric names, snapshot naming (§13, §15, §10.1); `settings.json` schema and DDL are code artefacts at M1 |

## Unverified claims from v0.1

| Claim | Status in v0.2 |
| --- | --- |
| Free-tier aged documents "only on the tablet"; API works without Connect | Moved to S4 |
| "remarkdown proves raw upload" / "runs this polling pattern" | **Removed** (unsubstantiated) |
| Open-source clients only document PDF/EPUB | Removed; upstream's PDF path is the same blob mechanism (§9) |
| Trusted Hosts limited to Anthropic's range | **False**; removed (§5.3) |
| DCR makes loopback automatic | Removed; S2 verifies loopback on the pinned version |
| Entra does not work | Reworded (§5.2) |
| remarkable-mcp instantiable per user | **False as shipped**; S0 + fork plan (§4.3) |
| All deps MIT/BSD/Apache, checked on PyPI | Corrected (§4.4) |
| Move panel 1696 × 954 and rmscene support | S3 |
| SSH restore is a plain copy | Corrected (§6) |
| Re-upload under a deleted UUID accepted | S1 step 3 |
| Account identity derivable from the token | S0 step 6 |
| `disconnect_remarkable` can revoke the device token | Corrected: it cannot (§11.1) |
| claude.ai mobile renders images / resource links | S6 |
| 10-minute poller tolerated | Open question 1; observed in S5 soak |
| OCR ≈ 1 ¢/page | Replaced by a cost formula from `usage` (§8.1) |
| Keycloak refresh config achievable as written | Corrected (§5.3) |
| ZFS/btrfs snapshots via a mounted hook | **False**; `host` strategy (§10.1) |
