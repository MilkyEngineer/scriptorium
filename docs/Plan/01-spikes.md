# Spikes

Each spike produces `docs/Spikes/<id>.md` with sections: **Question · Procedure · Evidence · Result · Decision · Follow-ups**. Evidence is scrubbed (no tokens, no real document names or UUIDs, no note content). A spike that fails is still "done" when the decision is written; the architecture is then amended before milestone work continues.

Time boxes are guidance for a focused session, not deadlines.

---

## S0 — `remarkable-mcp` as a per-user library without AGPL dependencies

**Gates:** M1. **Time box:** 1 day.

**Question.** Can the fork of `sammorrowdrums/remarkable-mcp` be installed without `pymupdf` and `ebooklib`, instantiated per user, and used for upload with a caller-supplied UUID? If not, what exactly do we vendor?

**Procedure.**

1. Fork to `MilkyEngineer/remarkable-mcp`. Read `pyproject.toml`, `api.py`, `sync.py`, `exporters.py`, `extract.py`, `markdown_pdf.py`, `tools.py`. Map every import of `fitz`/`pymupdf`/`ebooklib` and every `os.environ` read.
2. Branch `optional-extras`: move `pymupdf` and `ebooklib` to extras (`[pdf]`, `[epub]`); guard their imports; make sure the sync client and the `.rm` renderer import cleanly without them.
3. Branch `explicit-config`: replace `REMARKABLE_TOKEN`, `REMARKABLE_CACHE_DIR`, `REMARKABLE_SYNC_WORKERS` reads and the module-level client in `api.py` with a `ClientConfig`/`CloudClient` object.
4. Branch `caller-uuid`: `upload_document(..., document_id=None)` upserts by UUID and never appends a duplicate root entry.
5. In one Python process: create two clients with different (scratch) tokens and cache dirs; list both; confirm no shared state (`gc.get_referrers` on module globals, or simply grep for module-level mutables).
6. Decode a device token (it is a JWT) and record which claims could identify the reMarkable account stably across re-registrations. Do not commit the token.
7. Measure the diff size of the three branches combined.

**Pass criteria.**

- `uv add remarkable-mcp @ git+…@<tag>` followed by `uv pip list` shows neither `pymupdf` nor `ebooklib`; `scriptorium licence-check` (or `pip-licenses`) passes.
- Two clients with different tokens and cache dirs coexist in one process; no env reads at call time.
- Upload with a caller-supplied UUID is idempotent across two runs.
- Combined fork diff ≤ ~500 lines and upstreamable as 2–3 PRs. **Otherwise: vendor** `sync.py` (protocol client) and the stroke renderer into `scriptorium/cloud` and `scriptorium/library` with attribution, and drop the package dependency.
- A stable account identity claim is identified, or its absence is documented and §5.1's degraded rule is adopted.

**Write-up must include** the fork tag used, the list of modules Scriptorium imports, and the identity-claim decision.

---

## S1 — Native notebook upload through the cloud, including under a reused UUID

**Gates:** `push_ink` (M3), all of M4. **Time box:** 2 days. Needs the physical tablet.

**Question.** Can Scriptorium write a notebook (`.content` + `.metadata` + `.pagedata` + `*.rm`) into the cloud such that the Paper Pro Move opens it, and can it re-create a deleted document under its **original** UUID?

**Procedure.** (Scratch account, scratch folder.)

1. Using S0's client, download a small notebook written on the Move (one page, two layers, colour).
2. Upload its files unchanged under a **new** UUID into the scratch folder, using the generation-gated root commit with `broadcast: true`. Sync the tablet. Open it. Write on it. Sync. Download again and confirm the edit round-trips (new `.rm` hash, same UUID).
3. Delete the original notebook on the tablet; sync; confirm it leaves the root. Re-upload the archived files under the **original** UUID. Sync the tablet. Confirm it opens and lands in the right folder.
4. Run step 3's upload job twice; confirm exactly one root entry.
5. Simulate a generation conflict: edit a different document on the tablet between reading the root and committing; confirm the mutation is re-applied, not lost or duplicated.
6. Vary `.content` fields (template name, orientation, `fileType`, page geometry fields) to find the minimum the Move requires. Record which fields the Move rewrites after opening.
7. Repeat step 2 with an `.rm` produced by rmink (in the sidecar container, from an SVG) **only if** rmink is available locally; otherwise defer the ink-specific check to M3.

**Pass criteria.** Steps 2–5 all succeed as described; step 6 yields a documented minimal `.content`. Any failure in step 3 downgrades G5 to "restore under new UUIDs" and the architecture is amended.

---

## S2 — Issuer + Claude OAuth end to end (pre-registered clients vs DCR vs CIMD)

**Gates:** M1. **Time box:** 2 days. Needs public DNS/TLS and a run from claude.ai.

**Question.** Does the §5.3 reference realm let claude.ai (web and mobile) and Claude Code log in and stay logged in, and which client-registration mode should ship?

**Procedure.**

1. Bring up the `keycloak` compose profile with `realm-scriptorium.json` (write the realm export as part of this spike; pin the Keycloak version). Put a stub resource server behind the proxy that serves the PRM, returns the 401 contract, and validates JWTs exactly as §5.1 specifies.
2. claude.ai web: add the custom connector with client ID `scriptorium-claude`; log in; call a tool. Repeat on claude.ai mobile and Claude Desktop.
3. Claude Code: `claude mcp add --transport http scriptorium <url> --client-id scriptorium-claude-code`; `/mcp`; log in via loopback from a machine on a network **not** in any allowlist. Note which redirect URI and port Claude Code used.
4. Decode the access token: confirm `aud` contains the resource, `realm_access.roles` contains `scriptorium-admin` for the admin user, `email_verified` is present.
5. Lifetimes: wait for access expiry (15 min) and confirm silent refresh; leave a client idle > 24 h and confirm it still refreshes (Offline Session Idle 30 d); trigger two refreshes within a second from two surfaces and confirm neither session dies (Max Reuse 1); revoke the session in Keycloak and confirm the client sees `invalid_grant` and re-prompts.
6. Alternatives: enable anonymous DCR with "Host Sending Request Must Match" off / "Client URIs Must Match" on, and separately `--features=cimd`; repeat steps 2–3; count the clients created per connection; note Max Clients behaviour.
7. Lock down `/admin` and `/realms/master` at the proxy and confirm the console is still reachable via `KC_HOSTNAME_ADMIN` internally.

**Pass criteria.** Steps 2–5 succeed with pre-registered clients. The write-up compares the three registration modes and states which ships (default: pre-registered). The realm JSON, proxy rules and pinned version are committed under `docker/`.

---

## S3 — Paper Pro Move `.rm` rendering with rmscene

**Gates:** M1. **Time box:** 1 day. Needs the tablet once.

**Procedure.**

1. On the Move, create a **dedicated test notebook** (no real content): ≥ 20 pages covering each pen type and colour, highlighter over text, eraser use, typed text, a page with extra layers, an extended-height page, and an annotated PDF. Photograph or screenshot each page as the reference.
2. Export via the cloud (S0 client) and via `.rmdoc`; commit the export (it contains no private content) under `tests/fixtures/move-test-notebook/`.
3. Render every page with rmscene → SVG → resvg-py at 1× and 2×; composite the PDF page via pypdfium2. Compare against the references.
4. Confirm the panel geometry (expected 954 × 1696 portrait) and the `.rm` coordinate transform; record the exact numbers.
5. Note anything rmscene cannot parse (block types, pen ids) and open upstream issues.

**Pass criteria.** All pages render without exceptions; strokes, colours, highlighter blending and typed-text position match the references by eye; geometry documented; golden PNGs committed for regression.

---

## S4 — Free-tier behaviour and the 50-day cut-off

**Gates:** M1 (tombstone heuristic, G1 premise). **Time box:** 1 day of work, elapsed time longer.

**Question.** Does the cloud API work at all on a free (non-Connect) account, and what does an aged (> 50 days unopened) document look like through the API?

**Procedure.**

1. On a free scratch account (or by checking the real account's subscription state): list, download and upload via S0's client. Record HTTP statuses.
2. Find or create a document whose `lastOpened` is > 50 days old. Observe: is it in the root? Do its blobs download or 404? Is anything flagged in its metadata? Does opening it on the tablet bring it back?
3. If no aged document exists yet, record the plan: a scratch document created now, revisited after 50 days, with the heuristic shipped as "unknown" until then.

**Pass criteria.** A written statement of (a) which operations work without Connect, (b) the observable signature of an aged document, (c) the resulting `deletion_cause` heuristic for §7 step 4. If the API is Connect-only, the README and G1 are amended to say so.

---

## S5 — Two containers + SQLite WAL on the target filesystem; host snapshots

**Gates:** M1. **Time box:** 1 day plus a 24 h soak.

**Procedure.**

1. Ask the operator what filesystem backs `/data` (ext4/xfs/ZFS/btrfs vs. a FUSE or network mount). Implement the `statfs` refusal and verify it on a FUSE mount (e.g. `sshfs` or `bindfs` locally).
2. Run `server` + `worker` from the same image against one `/data` with a synthetic load: worker writing per-document transactions every second, server reading and enqueuing jobs, for 24 h. Count `SQLITE_BUSY` after `busy_timeout`, check `PRAGMA integrity_check` hourly.
3. If the host offers ZFS/btrfs: take a snapshot on the host, mount the snapshot directory read-only into the container, list it and read an index from it. Otherwise run restic from the container with `VACUUM INTO` and restore to a temp dir.

**Pass criteria.** Zero corruption, no unresolved `SQLITE_BUSY`; refusal works; at least one snapshot strategy proven end to end on the operator's host.

---

## S6 — What Claude clients render and accept

**Gates:** M1 (tool contracts). **Time box:** half a day, on top of S2's stack.

**Procedure.** With the S2 stub server, return from test tools: an image content block (PNG, 200 KB and 1.5 MB); an MCP resource link; a plain `https://…/dl/<token>` URL in text; a 64 KB text result; and accept a tool argument of 64 KB, 256 KB and 1 MB. Try from claude.ai web, mobile, Desktop and Claude Code. Also confirm whether `tools/list` filtering per user (admin tools) is honoured without a reconnect.

**Pass criteria.** A table in the write-up of what each client shows/accepts, and resulting defaults for `MAX_INLINE_KB`, text caps, `page_image` size, and whether `/dl/` URLs are clickable on mobile.

---

## S7 — Cloud change notifications as a poll trigger

**Gates:** the M2 "poll on notification" item (not M1). **Time box:** half a day, read-only, with the dev device token.

**Why.** The tablet and the official apps do not poll: they hold a websocket open to the cloud's notifications service and learn of another device's sync within seconds. Scriptorium polls every `POLL_SECONDS` (default 600), so a note written on the tablet is reachable only after up to ten minutes. The common case this spike serves is "write on the tablet, sit down at the desk, ask about it straight away".

**Procedure.**

1. Discover the notifications host the way the apps do (the service-manager lookup the sync library already performs for the document storage host, with the notifications service name) and open the websocket with the device's user token. Record the handshake requirements (headers, token kind, whether the URL carries the device id) and whether the connection is accepted for a *browser* device token.
2. Keep it open for an hour while the operator edits, creates, moves, trashes and deletes documents on the tablet and in the desktop app. Record every message shape with identifiers scrubbed: event names, which carry the new root generation or a document UUID, and what arrives for a device's own change versus another device's.
3. Measure the lag from the tablet's sync indicator to the event, and from the event to a changed root hash being readable (the root may lag the event; note whether a poll fired immediately on the event sees the change or needs a short delay or one retry).
4. Disconnect behaviour: idle timeouts, server-initiated closes, pings expected, behaviour across a token refresh, and whether two concurrent connections for one account are allowed (server and worker restarts overlap).
5. Rate: count messages per hour on an idle account and during the edit session.

**Pass criteria.** A scrubbed table of event shapes and lags, and a decision on the trigger design: the websocket is only a wake-up (on any event the account's loop runs a poll now, subject to a minimum gap of about 10 s and the existing byte and time budgets); polling stays the source of truth, because the two-complete-listings rule, the circuit breaker and the generation check all need full listings and a push channel can drop events or close silently; a lost connection falls back to the plain interval with backoff and is logged once. Settings proposed in the write-up (`NOTIFICATIONS=on|off`, minimum gap) go to Architecture §7 and §12.

**Fail outcome.** If the service refuses browser-device tokens, needs an undocumented client identity, or delivers events for only some change kinds, record that and fall back to a shorter adaptive interval instead (poll faster for a while after a change is seen, slower when the root has not moved).
