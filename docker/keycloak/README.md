# Keycloak reference realm (optional `keycloak` profile)

This directory is the worked example of Architecture §5.3. You need it only if you
have no OIDC issuer. Scriptorium does not depend on Keycloak (§5.2 lists what any
issuer must provide).

| File | Purpose |
| --- | --- |
| `realm-scriptorium.json` | Realm export loaded once by `kc.sh start --import-realm` |
| `create-admin.sh` | Creates or updates a Scriptorium admin user inside the container |

## Pinned version

**Keycloak 26.4.7** (`quay.io/keycloak/keycloak:26.4.7`), the newest 26.4 patch
release. It was checked against the quay.io tag list (`26.4.0` … `26.4.7`) and the
GitHub releases (26.4.7 was published 2025-12-01). Upstream has since moved on to
26.5–26.8. S2 verifies this exact version and records the result in
`docs/Spikes/S2.md`; moving to another version means repeating S2's checks.
26.4 is the first line with RFC 8414 metadata at
`/.well-known/oauth-authorization-server/realms/scriptorium` (26.4.0 release
notes, "MCP").

PostgreSQL is pinned to `postgres:16.15-alpine`, the newest 16.x on Docker Hub.

## What the realm contains

| Setting | Value |
| --- | --- |
| Realm | `scriptorium`; `sslRequired: external`; frontend URL `${KC_HOSTNAME}` |
| Self-registration | off; login with email allowed; verify email on; usernames not editable |
| Brute-force protection | on (lockout after 10 failures, temporary, max 15 min) |
| Password policy | `length(12) and notUsername and notEmail` |
| Signature | RS256 (Scriptorium accepts RS256/ES256) |
| Access Token Lifespan | 900 s (15 min) |
| SSO Session Idle / Max | 43200 s (12 h) / 604800 s (7 d) |
| Offline Session Idle | 2592000 s (30 d); Offline Session Max Limited **off** |
| Revoke Refresh Token | **on**, Refresh Token Max Reuse **1** |
| Realm role | `scriptorium-admin` |
| Users / identity providers | none (users come from `create-admin.sh`; brokers are added by hand, below) |

Clients (both public, standard flow only, PKCE S256 required, consent off,
implicit/direct-access/service-account/device/CIBA off, web origins `+`,
Full Scope Allowed off):

| Client ID | Redirect URIs | Used by |
| --- | --- | --- |
| `scriptorium-claude` | `https://claude.ai/api/mcp/auth_callback` | claude.ai web, Desktop, mobile (enter the client ID in the custom-connector dialog) |
| `scriptorium-claude-code` | `http://localhost/callback`, `http://127.0.0.1/callback` | `claude mcp add --transport http scriptorium https://mcp.example.com/mcp --client-id scriptorium-claude-code` |

Client scopes. Because the export defines `clientScopes`, Keycloak creates **only**
the scopes listed in it (plus `offline_access`, which it always creates itself):

| Scope | Realm default? | Carries |
| --- | --- | --- |
| `scriptorium` | **default** | audience mapper (`aud` += `${SCRIPTORIUM_AUDIENCE}`), realm-roles mapper (`realm_access.roles`, multivalued, access token), `email`, `email_verified`; scope mapping for `scriptorium-admin` |
| `basic` | default | `sub`, `auth_time` (without it access tokens have no `sub`) |
| `profile`, `email`, `roles`, `web-origins`, `acr` | default | Keycloak's built-in mappers |
| `offline_access` | optional | created by Keycloak at import; request `scope=offline_access` for refresh tokens that survive the 12 h SSO idle |

The scope mapping matters because Full Scope Allowed is off: a role reaches
`realm_access.roles` only if some scope maps it, so `scriptorium-admin` is mapped by
the `scriptorium` scope and appears regardless of the client's setting.

The absent built-ins (`address`, `phone`, `microprofile-jwt`, `organization`,
`service_account`) are left out on purpose: nothing here needs them.

## Placeholders: how `${...}` is filled in

`realm-scriptorium.json` contains two placeholders:

- `${SCRIPTORIUM_AUDIENCE}`: the custom audience of the `scriptorium` scope. Set it
  in `docker/.env` to the same value as `SCRIPTORIUM_OIDC_AUDIENCE` (by default
  `SCRIPTORIUM_PUBLIC_URL`), for example `https://mcp.example.com/mcp`.
- `${KC_HOSTNAME}`: the realm's frontend URL, the same variable that configures
  the server's hostname. The issuer is therefore `${KC_HOSTNAME}/realms/scriptorium`,
  which must equal `SCRIPTORIUM_OIDC_ISSUER`.

Keycloak 26.4 substitutes these itself at **startup import**. No script or
entrypoint step is involved. Evidence, all at tag `26.4.7`:

- `docs/guides/server/importExport.adoc`, "Using Environment Variables within the
  Realm Configuration Files": `"realm": "${MY_REALM_NAME}"` is resolved from the
  environment variable `MY_REALM_NAME`.
- `services/.../exportimport/ExportImportManager.java`: with `--import-realm`
  ("import at startup") it sets `ExportImportConfig.setReplacePlaceholders(true)` and
  imports every `*.json` in `/opt/keycloak/data/import` through the `singleFile`
  provider. Strategy is `IGNORE_EXISTING`.
- `model/storage-services/.../AbstractFileBasedImportProvider.java`: when
  replace-placeholders is on, the file is streamed through
  `StringPropertyReplacer.replaceProperties(..., System::getenv)`.
- `common/.../StringPropertyReplacer.java`: `${NAME}` → `getenv("NAME")`;
  `${NAME:default}` falls back to `default`; an unresolved `${NAME}` is left as is.

The syntax is `${NAME}` with the bare variable name. Do **not** use `${env.NAME}`:
in this code path that would look up a variable literally called `env.NAME`.

What follows from that:

1. **Only the startup import substitutes.** A partial import in the admin console,
   or `kc.sh import` without the startup path, takes the text literally.
2. **The import runs once.** If the realm already exists it is skipped, so
   editing the JSON or the variables later changes nothing (see "Changing the
   realm").
3. **Unset variables stay literal.** The audience would then be the string
   `${SCRIPTORIUM_AUDIENCE}` and every token would be rejected. The compose
   entrypoint guard therefore refuses to start Keycloak while `SCRIPTORIUM_AUDIENCE`,
   `KC_HOSTNAME` or `KC_DB_PASSWORD` is empty.
4. The replacer scans the **whole file**. The only other `${...}` in the export is
   Keycloak's own `resource_access.${client_id}.roles` claim template. It survives
   because no environment variable called `client_id` exists. Do not add one.

## Bring-up

From `docker/`, after following `compose.example.yml`'s header:

1. In `.env`, set `KC_HOSTNAME`, `SCRIPTORIUM_AUDIENCE`, `KEYCLOAK_DB_PASSWORD` and
   `KC_BOOTSTRAP_ADMIN_PASSWORD`. Generate the secrets on the host, for example
   `openssl rand -base64 32 | tr -d '/+='`.
2. `docker compose --profile keycloak up -d`, then wait for `keycloak` to become
   healthy. The first start re-augments the server for PostgreSQL, which takes
   about a minute.
3. Open the admin console at `KC_HOSTNAME_ADMIN` (default `http://127.0.0.1:8481`;
   over SSH use `ssh -L 8481:127.0.0.1:8481 docker-host`). Sign in as the bootstrap
   admin, create a **permanent** master-realm admin, delete the temporary one, and
   empty `KC_BOOTSTRAP_ADMIN_PASSWORD` in `.env`.
4. Create the Scriptorium admin in the `scriptorium` realm:

   ```sh
   docker compose --profile keycloak exec \
     -e KCADM_USER=<your-master-admin> keycloak \
     bash /opt/keycloak/scriptorium/create-admin.sh alice@example.com
   ```

   With no `KCADM_PASSWORD`, kcadm prompts for it. Right after first start you can
   leave out `-e KCADM_USER=...`, and the script then uses the bootstrap admin from
   the container environment. The script prints a temporary password once, and
   Keycloak forces a change at first login.
   Run it again to re-grant the role. Add `--reset-password` to issue a new
   temporary password, and `--email` to set an address different from the username.
5. Add the user's email (or `sub`) to `SCRIPTORIUM_OIDC_ALLOWED_USERS` in
   `scriptorium.env`, then restart `scriptorium`.

**Email.** The realm has "verify email" on but no SMTP server. Users created by
the script are already verified. For brokered users, password reset, or anything
else that sends mail, configure SMTP under *Realm settings → Email*. You may then
also turn on *Forgot password*.

## Changing the realm

The JSON is imported only while the realm does not exist. To apply a changed
export to a **fresh** installation, either:

- change the setting in the admin console (and in the JSON, so the two stay in
  step), or
- delete the realm in the admin console and restart Keycloak so `--import-realm`
  recreates it. This also deletes its users and sessions: re-run `create-admin.sh`,
  and every Claude client has to log in again.

## Port-agnostic loopback redirects (Claude Code)

Claude Code listens on a random loopback port. The client registers
`http://localhost/callback` and `http://127.0.0.1/callback` **without a port**,
and Keycloak 26.4 accepts any port for these:

- `services/.../protocol/oidc/utils/RedirectUtils.java` (26.4.7, lines 103–106): if
  the redirect does not match, the scheme is `http` and the host is one of
  `localhost`, `127.0.0.1` or `[::1]`, Keycloak rebuilds the URI with port 80.
  `KeycloakUriBuilder` drops the default port, so
  `http://127.0.0.1:53712/callback` is matched as `http://127.0.0.1/callback`.
- `testsuite/.../oauth/OAuthRedirectUriTest.java#testLoopback` (26.4.7): with a
  registered URI of `http://127.0.0.1/myapp`, `http://127.0.0.1:8280/myapp` is
  accepted and `http://127.0.0.1/myapp2` is rejected.

The path must still match exactly. S2 step 3 records which port and host Claude
Code actually used.

## Identity brokers (Google, Microsoft): optional, manual linking only

The export has no identity providers. To add one (§5.3):

1. *Authentication → Flows*: duplicate **first broker login** as
   `scriptorium first broker login`. In the copy, set **Create User If Unique** to
   *Disabled* and **Confirm Link Existing Account** to *Disabled*. Keep **Verify
   Existing Account By Re-authentication** in place. This is Keycloak's
   "Disabling automatic user creation" recipe. A brokered login can then never
   create a new user. It can only link to an existing user after that user
   re-enters their own username and password. Do **not** use the "Detect existing
   broker user" / "Automatically Set Existing User" recipe: it links by email
   without verification, so it would let a stranger's Google account take over an
   existing user.
2. *Identity providers → Add*: **Google**, or **Microsoft** (for a single work
   tenant use **OpenID Connect v1.0** with
   `https://login.microsoftonline.com/<tenant-id>/v2.0` as discovery). Register
   the provider's redirect URI as
   `https://auth.example.com/realms/scriptorium/broker/<alias>/endpoint`. Set
   *First login flow override* to `scriptorium first broker login`, and leave
   *Trust Email* off.
3. The proxy examples already pass `/realms/scriptorium/broker/<alias>/login` and
   `/endpoint`.

`SCRIPTORIUM_OIDC_ALLOWED_USERS` remains the second gate either way.

## Client registration alternatives (S2 step 6)

Off by default. Pre-registered clients ship unless S2 shows Claude cannot finish
the flow with them.

**(a) Anonymous Dynamic Client Registration (RFC 7591).** Keycloak creates
anonymous DCR policies when it imports the realm. With the defaults, the Trusted
Hosts list is empty, so every anonymous request is refused. To evaluate it, open
*Clients → Client registration → Anonymous access policies*:

- **Trusted Hosts** (`trusted-hosts`): turn *Host Sending Client Registration
  Request Must Match* **off** (Anthropic's egress range cannot be expressed: no
  CIDR support). Turn *Client URIs Must Match* **on** and add `claude.ai`,
  `localhost` and `127.0.0.1` to *Trusted Hosts*.
- **Max Clients Limit** (`max-clients`): raise it from 200. Every new Claude
  connection registers a client, so you also need a job that deletes stale
  dynamically registered clients.
- **Allowed Client Scopes**: check that `offline_access` and `scriptorium` are
  allowed for registered clients.
- **Consent Required**: anonymous clients get consent forced on. Leave it unless
  S2 shows a reason to change it.
- At the proxy, replace the `clients-registrations` deny with an allow for
  `/realms/scriptorium/clients-registrations/openid-connect` from the operator's
  chosen sources only.

**(b) Client ID Metadata Documents (CIMD).** **Not available on 26.4.7.** The
`cimd` feature (`Profile.Feature.CIMD`, experimental) first appears in Keycloak
**26.6.0**. Evaluating CIMD therefore means S2 pins 26.6 or later. On that version:
add `KC_FEATURES=cimd`, then add a client policy whose condition is
`client-id-uri` (scheme `https`, permitted domain `claude.ai`) and whose executor
is `client-id-metadata-document` (permitted domain `claude.ai`). Check the RFC
8414 metadata for `client_id_metadata_document_supported`, which Claude looks for.
Treat those provider IDs as a starting point and confirm them against that
version's server-admin guide.

## Exposure

The proxy must expose only `/realms/scriptorium/{protocol/openid-connect,
login-actions, broker/*/endpoint|login, .well-known}/…`, `/resources/…` and
`/.well-known/…`. It must deny `/admin` and `/realms/master` from the internet
and deny `clients-registrations`. See `docker/proxy/`. The admin console uses
`KC_HOSTNAME_ADMIN` (§5.3). Keycloak's management port (9000: health, metrics)
is not published.
