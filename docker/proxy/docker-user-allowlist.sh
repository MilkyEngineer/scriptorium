#!/bin/bash
# Restrict Scriptorium's published ports to the reverse proxy's address when the
# proxy runs on ANOTHER machine (docker/proxy/README.md, "remote proxy" topology).
#
# Docker's published ports bypass the host's INPUT chain; the only hook that sees
# them is the DOCKER-USER chain, and by then the packet has been DNATed to the
# container port, so the match is on the connection's ORIGINAL destination.
# It is not bound to an interface: a VPN or second NIC would otherwise bypass it.
# `--ctdir ORIGINAL` leaves reply packets alone; connections whose original
# destination is not HOST_ADDR:port (container egress, other services) never match.
#
# Run as root, before or after the Docker daemon starts. If DOCKER-USER does not
# exist yet the script creates it: Docker adopts an existing chain and keeps its
# rules, so running this from an early boot hook closes the ports before any
# container is reachable (no wait, no race). Idempotent: re-running replaces the
# rules it owns. Persist it with whatever your host runs at boot (an early boot hook, with a copy of this script on the boot medium). Outcome goes to syslog via logger.
#
#   HOST_ADDR=192.0.2.10 PROXY_ADDR=192.0.2.1 ./docker-user-allowlist.sh
set -euo pipefail

HOST_ADDR="${HOST_ADDR:?this host's LAN address the ports are published on}"
PROXY_ADDR="${PROXY_ADDR:?the reverse proxy's address}"
PORTS="${PORTS:-8480 8481}"
TAG="scriptorium-allowlist"

fail() { logger -t "$TAG" "FAILED: $*"; echo "$TAG: $*" >&2; exit 1; }

# Create the chain if Docker has not yet; Docker keeps it and adds the FORWARD jump.
iptables -nL DOCKER-USER >/dev/null 2>&1 || iptables -N DOCKER-USER || fail "cannot create DOCKER-USER"

# Drop the rules from a previous run (matched by their comment), then re-add.
while read -r num; do
  [ -n "$num" ] && iptables -D DOCKER-USER "$num"
done < <(iptables -nL DOCKER-USER --line-numbers | awk -v t="$TAG" '$0 ~ t {print $1}' | sort -rn)

for port in $PORTS; do
  iptables -I DOCKER-USER -p tcp \
    -m conntrack --ctdir ORIGINAL --ctorigdst "$HOST_ADDR" --ctorigdstport "$port" \
    ! -s "$PROXY_ADDR" \
    -m comment --comment "$TAG" -j DROP || fail "cannot insert rule for port $port"
done
iptables -nL DOCKER-USER --line-numbers | grep "$TAG"
logger -t "$TAG" "installed: ports $PORTS on $HOST_ADDR limited to $PROXY_ADDR"
