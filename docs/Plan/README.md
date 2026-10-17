# Scriptorium — Implementation plan

This folder turns `docs/Architecture.md` (v0.2) into an ordered, gated plan that an autonomous coding session can execute. Read in order:

1. `01-spikes.md` — seven spikes (S0–S6). Each has a procedure, a pass criterion and a write-up location. **No milestone work starts until its gating spikes have a written result**, even a negative one.
2. `02-milestones.md` — M1–M6 with contents and measurable acceptance criteria.
3. `03-review-dispositions.md` — every finding from the adversarial review of v0.1 and what was done with it. Use it to understand *why* the architecture says what it says before changing it.

## Rules of engagement

- **Spike before feature.** If a task depends on behaviour marked "verify" or "to confirm" in the architecture, it is a spike first. Write `docs/Spikes/<id>.md` with: question, procedure, evidence (scrubbed), result, decision.
- **Scratch account, or a scratch folder with a kill switch.** All cloud writes during development go to a dedicated scratch reMarkable account. If the operator has no second account, the operator may instead designate one folder in their own library as the scratch folder: every write names that folder as parent, every written UUID is minted by the test run and recorded, the run aborts if the root it read back differs from the root it wrote in anything other than its own UUIDs, and the operator removes the folder from the tablet afterwards. Read-only spikes (listing, download, token decoding) may run against a real account. Never run `restore(overwrite)`, `push_*`, or `admin_purge_*` against anyone's real library until M4 acceptance.
- **No production secrets in the coding session.** The session gets a development master key, a development issuer realm, a scratch device token. The operator keeps production keys.
- **Public repo hygiene.** Cassettes and fixtures are scrubbed of tokens, UUIDs that map to real documents, and note content before commit. Fixture notebooks are written for the purpose.
- **Licence gate is non-negotiable.** `uv sync` must never pull `pymupdf`, `ebooklib`, `rmink` or any GPL/AGPL package into the image. CI fails otherwise; so should you.
- **Clean room for ink.** Anyone writing the native image-to-ink converter must not have rmink's source in their context. The sidecar repo is separate.

## Human hand-off points

Things the coding session must stop and ask the operator for (also listed in `CLAUDE.md`):

| When | What |
| --- | --- |
| S0/S4 | A one-time code (read-only use for S0; scratch account or scratch folder for S4) and whether the account has Connect |
| S1, S3, M3, M4 | Checking a document on the physical tablet; exporting the test notebook |
| S2 | The issuer: existing OIDC provider details, or permission to run the Keycloak profile; public hostnames; TLS; a test from claude.ai (hosted, Anthropic egress) |
| S5 | What filesystem backs `/data`; whether host snapshots are available |
| S6, M1 | A manual run from claude.ai mobile |
| M1 | Confirmation that any organisation whose notes will be synced permits it under its data policy (open question 5) |
| M2 | A Claude API key and the monthly cap |
| Any | Anything that costs money, touches a real account, or is exposed to the internet for the first time |

## Order of work

```
S0 ─┬─ S4 ─┐
S2 ─┤      ├─▶ M1 ─▶ M2 ─▶ M3 ─▶ M4 ─▶ M5 ─▶ M6
S3 ─┤      │          ▲      ▲
S5 ─┤      │          │      │
S6 ─┘      │        (S1 may land any time before M3; M4 needs it)
S1 ────────┘
```

S0, S2, S3, S4, S5, S6 gate M1. S1 gates `push_ink` (M3) and all of M4. Spikes are independent of each other and can run in parallel sessions, except that S4 should reuse S0's client.
