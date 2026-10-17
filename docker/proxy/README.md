# Reverse-proxy examples

Two examples implement the per-path table in Architecture §13 for
`mcp.example.com` (Scriptorium) and `auth.example.com` (Keycloak, `keycloak`
profile only):

- `nginx.conf.example`: include it in nginx's `http {}` block. TLS certificates
  come from certbot (webroot); the paths are in comments.
- `Caddyfile.example`: Caddy ≥ 2.8, which gets its TLS certificates over ACME
  automatically. Checked with `caddy validate` (Caddy 2.11.7), both as shipped and
  with the Cloudflare and allowlist options turned on.

Replace the `example.com` names with your own, and the RFC 5737 documentation
addresses (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) with your own
networks. If you already run an issuer, drop the `auth.example.com` part: your
issuer's exposure rules are its own.

## The table

| Host / path | Example behaviour | Why |
| --- | --- | --- |
| `mcp…/mcp` | proxied and streamed (no buffering, read timeout 3600 s in nginx; Caddy has no upstream read timeout), rate-limited; optional IP allowlist | Streamable HTTP + SSE. Bearer auth is the primary control. An allowlist must include Anthropic's egress `160.79.104.0/21` **and** every network Claude Code runs from. |
| `mcp…/.well-known/*` | proxied | Protected-resource metadata (RFC 9728) |
| `mcp…/dl/*` | proxied, unbuffered, rate-limited (nginx; Caddy needs the `caddy-ratelimit` plugin) | Signed 10-minute download links opened in users' browsers |
| `mcp…/healthz` | monitoring CIDR only | |
| `mcp…` anything else | 404 | Metrics (`:9100`) are never proxied or published |
| `auth…/realms/scriptorium/{protocol/openid-connect/, login-actions/, broker/<alias>/endpoint, broker/<alias>/login, .well-known/}` | proxied, rate-limited | Discovery and token calls (Anthropic's egress), login pages (users' browsers), everything (Claude Code) |
| `auth…/resources/*`, `auth…/.well-known/*` | proxied | Theme assets; RFC 8414 metadata at `/.well-known/oauth-authorization-server/realms/scriptorium` |
| `auth…/realms/scriptorium/clients-registrations/*` | **403** | Clients are pre-registered (§5.3) |
| `auth…/admin/*`, `auth…/realms/master/*` | **403** except from an admin CIDR | The admin console lives on `KC_HOSTNAME_ADMIN`, not the public host |
| `auth…` anything else | 404 | Including other realms and the account console (an example to enable it is in a comment) |

`broker/<alias>/login` is not in §13's table. Keycloak's login page sends brokered
logins through it, so without it an identity broker (if you add one) cannot start
a login.

Both examples send `Host`, `X-Forwarded-For`, `X-Forwarded-Proto` and
`X-Forwarded-Host` to the upstream. The proxy is the edge, so it **sets**
`X-Forwarded-For` to the connecting address and never appends to whatever a
client sent. nginx sets the headers once per `server`, because a location that
defines any `proxy_set_header` of its own silently drops all the inherited ones.
The Keycloak host gets larger header buffers for Keycloak's cookies and redirect
URLs. HSTS is on for both hosts.

## Topology A: proxy on the Docker host

- Run the proxy as a container attached to the compose `proxy` network (named
  `scriptorium-proxy` by default; set `external: true` in `compose.yml` if the
  proxy's own project owns the network). It reaches `scriptorium:8000` and
  `keycloak:8080` by name.
- The nginx example resolves those names through Docker's DNS at runtime
  (`resolve`, nginx ≥ 1.27.3). nginx therefore follows recreated containers, and
  it starts while the `keycloak` profile is down.
- Leave `SCRIPTORIUM_PUBLISH_ADDR=127.0.0.1`. The published ports are then only a
  loopback convenience, for the Keycloak admin console over an SSH tunnel.
- `SCRIPTORIUM_TRUSTED_PROXIES` and `KC_PROXY_TRUSTED_ADDRESSES` can stay at their
  defaults (Docker bridge ranges).

## Topology B: proxy on another machine

- In `docker/.env`, set `SCRIPTORIUM_PUBLISH_ADDR` to an address of the Docker
  host that **only the proxy** can reach: a dedicated LAN/VPN interface, or
  firewall ports 8480/8481 to the proxy's address. The examples use
  `192.0.2.10:8480` (Scriptorium) and `192.0.2.10:8481` (Keycloak). Switch the
  upstream lines marked "(b)".
- Set `SCRIPTORIUM_TRUSTED_PROXIES` (in `scriptorium.env`) and
  `KC_PROXY_TRUSTED_ADDRESSES` (in `.env`) to the proxy's address, e.g.
  `192.0.2.20`, so `X-Forwarded-*` is believed from it alone. Docker usually
  preserves the source address of traffic to published ports. If your setup
  rewrites it (the userland proxy, rootless Docker), the containers see the
  bridge gateway instead, so trust that address and keep the firewall rule.
- The hop from proxy to Docker host is plain HTTP, so keep it on a network you
  trust. Otherwise run it over WireGuard/Tailscale.
- Never publish `9100` (Scriptorium metrics) or `9000` (Keycloak management).

## Cloudflare (optional)

If the hostnames are proxied by Cloudflare, the connecting address is Cloudflare's.
Both examples include a commented block that takes the client address from
`CF-Connecting-IP`. It does so **only** when the connection itself proves it came
from Cloudflare: Authenticated Origin Pulls, i.e. a client certificate verified
against Cloudflare's origin-pull CA (nginx: `ssl_verify_client on` and a `map` on
`$ssl_client_verify`; Caddy: `client_auth require_and_verify`, plus
`trusted_proxies` limited to Cloudflare's ranges). Without that proof, anyone who
can reach the origin directly could spoof the header and get past the allowlists
and rate limits.

Cloudflare terminates TLS, so note content passes through it. §13 prefers the
direct route. With the zone proxied, ACME http-01 still works as long as
`/.well-known/acme-challenge/` reaches the origin; otherwise use DNS-01.

## Firewalling the published ports (remote proxy)

When the proxy is on another machine, every device on that LAN can reach the
published ports directly, and Keycloak serves its admin console on the
proxy-facing port too. A rule on the proxy or router cannot stop that, because
LAN-to-LAN traffic never passes through it: the rule has to live on the Docker
host, in Docker's `DOCKER-USER` chain. `docker-user-allowlist.sh` installs it:

    LAN_IF=br0 HOST_ADDR=<this host> PROXY_ADDR=<proxy> ./docker-user-allowlist.sh

Run it as root after Docker starts and persist it with a post-Docker boot hook.
