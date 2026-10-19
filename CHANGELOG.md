# Changelog

All notable changes to Scriptorium are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/); until 1.0 a minor release may change
configuration or tool behaviour, and this file says so when it does.

## 0.1.0 — 2026-10-19

First release: a safe, versioned archive of a reMarkable library with OCR and search,
readable from any Claude client over MCP.

**Image:** `ghcr.io/milkyengineer/scriptorium:0.1.0`, also tagged `0.1`, `0` and
`latest`, for `linux/amd64` and `linux/arm64`. One image, two entrypoints: `serve` (the
MCP server) and `worker` (the sync worker). Start from `docker/compose.example.yml`;
the bundled Keycloak profile is optional and only for operators without an OIDC issuer.

### Added

#### Archive and sync

- Background worker that mirrors each linked reMarkable cloud library to `/data` in the
  tablet's own on-disk format, with original document UUIDs.
- Every superseded version is kept; `versions` lists them and `read_page(version=…)`
  reads an old one.
- Deletions become tombstones only after two consecutive complete listings, and are
  never purged automatically. A partial listing never tombstones anything.
- Mass-delete circuit breaker: when too many documents vanish at once, deletions halt
  and `account_status` reports the anomaly; an admin clears it with
  `admin_clear_anomaly`, optionally approving the deletions it held back.
- Staged, verified, atomic commits: a worker killed mid-download leaves no partial
  document, and staging is swept on restart.
- Token-endpoint outages back off without forcing a relink; a repeatedly rejected device
  token marks the account `needs_relink`.
- Wake-up on cloud change notifications (`SCRIPTORIUM_NOTIFICATIONS`, on by default): a
  tablet edit is archived within seconds instead of waiting for the next poll.
- Sync scope with `set_sync_scope`: archive everything, or allow or deny by reMarkable
  tags on documents and folders. Narrowing the scope never deletes anything already
  archived; excluded documents are reported as `excluded`.
- Job priorities: renders of deleted or excluded documents yield to live documents.
- Host snapshot scripts for btrfs and ZFS (`docker/host-snapshots/`).

#### Reading and search

- Tools: `list`, `recent`, `search`, `read_page`, `read_document` (Markdown),
  `page_image`, `versions`, `trash`.
- Page rendering for the Paper Pro Move, including colour pens, highlighter, typed
  text and annotated PDFs.
- Tags indexed from documents, pages and folders; `list` and `search` filter by tag,
  and folder tags apply to everything inside the folder.
- Full-text `search` over transcripts, with snippets.

#### OCR

- Three modes, resolved per user: the user's own Claude API key
  (`set_claude_api_key`, `clear_claude_api_key`), a server-wide key for users the
  operator admits (`SCRIPTORIUM_SERVER_KEY_USERS`, `admin_allow_server_key`), or lazy
  transcription inside the user's own Claude session (`save_transcript`).
- Monthly spend caps, global and per user, on the server key, and an optional cap on a
  user's own key; at a cap OCR degrades to lazy instead of failing.
- On-demand `transcribe`, and auto-OCR with `set_auto_ocr` by folder or tag once a page
  has stopped changing; large backlogs go through the Message Batches API at half price.
- A page is transcribed once per version; `transcribe(force=true)` re-transcribes.
- `ocr_status` reports mode, queue and spend from the API's reported usage.

#### Accounts, identity and administration

- OAuth 2.1 resource server: bring your own OIDC issuer and register two public PKCE
  clients for Claude. Users are allow-listed (`SCRIPTORIUM_OIDC_ALLOWED_USERS`); the
  server refuses to start without the allowlist or master keys.
- Each user gets isolated storage keyed by issuer and subject.
- `connect_remarkable`, `disconnect_remarkable`, `account_status`. Device tokens and
  API keys are encrypted at rest and never returned by any tool.
- Destructive actions are two-step: a dry run returns a single-use confirmation token
  bound to the user and arguments (`disconnect_remarkable(keep_archive=false)`,
  `admin_clear_anomaly`).
- Admin tools gated by an issuer role.
- Audit log and structured logs with identifiers only, never note content, tokens or
  tool arguments.

#### Deployment

- Non-root image (uid 1000) with a read-only root filesystem; `/data` must be a local
  POSIX filesystem and the server refuses network or FUSE mounts.
- Compose example with health checks, an optional Keycloak profile with a ready-made
  realm, and reverse-proxy examples.

### Not yet included

Push to the tablet, the backup and restore tools (`snapshots`, `restore`, `export`),
storage quotas, personal access tokens and the wider admin tool set are planned for
later 0.x releases.

### Licence

MIT. No GPL or AGPL Python package is shipped in the image; CI enforces this with a
licence gate.
