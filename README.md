# Scriptorium

**Your reMarkable library, on your own server, readable by Claude.**

Scriptorium is a self-hosted [MCP](https://modelcontextprotocol.io) server that mirrors a reMarkable cloud library to local storage, keeps a durable, versioned archive of every notebook (including ones the free tier stops syncing), transcribes handwriting with Claude on demand, pushes documents back to the tablet, and can restore a wiped tablet from the archive — all through tools any Claude client can call.

> **Status: design phase.** Nothing runs yet. The architecture is in [`docs/Architecture.md`](docs/Architecture.md) (v0.2, after an adversarial review); spikes and milestones are in [`docs/Plan/`](docs/Plan/).
>
> Not affiliated with reMarkable AS. The reMarkable cloud API is unofficial and may change without notice.

## What it does

| Area | Tools (abridged) | Notes |
| --- | --- | --- |
| Archive | background sync worker | Polls the reMarkable cloud per user; stores documents in the tablet's own on-disk format with original UUIDs; keeps every superseded version; deletions become tombstones that are never purged automatically |
| Read & search | `list`, `recent`, `search`, `read_page`, `read_document`, `page_image`, `versions` | Works from claude.ai (web, Desktop, mobile) and Claude Code with one identity |
| OCR | `transcribe`, `save_transcript`, `set_auto_ocr` | Three billing modes: a per-user Claude API key, a server-wide key (per-user and global spend caps), or lazy transcription inside your own Claude session |
| Push | `push_markdown`, `push_file`, `push_ink` | Markdown → PDF, PDF/EPUB by URL, images/SVG/PDF → native ink (optional sidecar). Conversion happens on the server, so it works from a phone |
| Restore | `snapshots`, `trash`, `restore`, `export` | Rebuilds a wiped tablet (folders included) through the cloud, resumably and without duplicates; no cable, no developer mode |
| Multi-user | OAuth 2.1 via your own identity provider | Bring your own OIDC issuer; each login gets isolated storage and its own reMarkable link; users are allow-listed; admin tools are role-gated |

## How it works

```
Paper Pro Move ⇄ reMarkable cloud ⇄ Scriptorium (Docker) ⇄ Claude clients
                                        │
                                        ├─ worker: poll · archive · render · OCR
                                        ├─ server: Streamable HTTP MCP at /mcp
                                        └─ your OIDC issuer (Keycloak optional)
```

- **Identity:** Scriptorium is an OAuth 2.1 resource server and ships no identity provider. Point `SCRIPTORIUM_OIDC_ISSUER` at the issuer you already run (Keycloak, Authentik, Zitadel, Auth0, Okta, Entra ID…) and register two public PKCE clients for Claude; the exact requirements are in Architecture §5.2. A Keycloak compose profile with a ready-made realm is included for operators who have none. A Scriptorium user is an identity at that issuer, so two Claude accounts logging in as the same user share one library.
- **Linking a tablet:** call `connect_remarkable(code)` with a one-time code from <https://my.remarkable.com/device/browser/connect>. The device token is encrypted at rest.
- **No web UI:** everything a user or admin does is an MCP tool; server-wide settings are environment variables. Destructive actions are two-step (dry run → confirmation token).
- **Trust model:** the operator can read everything on disk. Note content also passes through your Claude conversation. Both are inherent; read the data-policy note in the architecture before syncing anything sensitive.

## Connecting Claude

Once deployed at `https://mcp.example.com/mcp`:

- **claude.ai / Claude Desktop / mobile:** Settings → Connectors → Add custom connector → paste the URL and the client ID your operator gives you → sign in at your identity provider.
- **Claude Code:**

  ```bash
  claude mcp add --transport http scriptorium https://mcp.example.com/mcp --client-id scriptorium-claude-code
  # then inside Claude Code:
  /mcp   # sign in
  ```

## Deploying

A `docker/compose.example.yml` with the server and worker (plus optional `keycloak` and `ink` profiles) and a reverse-proxy layout is described in the architecture document. Requirements: a public HTTPS hostname, an OIDC issuer reachable by Claude, a local filesystem for `/data` (not a network or FUSE mount), and a master key kept outside `/data`. See `docs/Architecture.md` §12–13.

## Roadmap

1. Spikes (S0–S6): `remarkable-mcp` as an AGPL-free library · native notebook upload and re-upload under the original UUID · issuer + Claude OAuth end to end · Paper Pro Move rendering · free-tier behaviour · SQLite on the target filesystem · what Claude clients render
2. M1 safe archive, single user · M2 OCR modes & search · M3 push · M4 backup/restore · M5 multi-user & admin · M6 hardening and 1.0

Details and acceptance criteria: [`docs/Plan/`](docs/Plan/).

## Licensing

Scriptorium is released under the [MIT License](LICENSE).

Dependency licences were checked so that the Docker image can be distributed under MIT:

- Permissive, included: `remarkable-mcp` (MIT, used as a library via a fork that makes its AGPL extras optional), `rmscene` (MIT), `mcp` (MIT), `resvg-py` (MIT), `pypdfium2` (BSD-3/Apache-2.0), `WeasyPrint` (BSD-3), `markdown-it-py` (MIT), `httpx` (BSD-3), `cryptography` (Apache-2.0/BSD-3), `PyJWT` (MIT). Keycloak (Apache-2.0) is an optional separate container.
- **Deliberately excluded from the image:** `PyMuPDF` (AGPL-3.0 or commercial) and `ebooklib` (AGPL-3.0-or-later), both hard dependencies of upstream `remarkable-mcp` — replaced by `resvg-py` for SVG rasterising, `pypdfium2` for PDF pages, and WeasyPrint for Markdown → PDF; EPUB is pass-through bytes. `rmink` (AGPL-3.0-or-later) — image-to-ink conversion is provided by an optional, separately licensed `scriptorium-ink` sidecar container (AGPL, source published) until a clean-room MIT implementation on `rmscene` replaces it. `rmc` is not used (it pins an incompatible `rmscene`).
- CI runs a licence gate over the built image and fails on any GPL/AGPL Python package.

If you add a dependency, keep it MIT/BSD/Apache-compatible or put it behind the sidecar boundary.

## Acknowledgements

- [SamMorrowDrums/remarkable-mcp](https://github.com/sammorrowdrums/remarkable-mcp) — cloud protocol client, rendering and export
- [ricklupton/rmscene](https://github.com/ricklupton/rmscene) — the `.rm` v6 format
- [ddvk/rmapi](https://github.com/ddvk/rmapi) — reference for the reMarkable sync protocol
