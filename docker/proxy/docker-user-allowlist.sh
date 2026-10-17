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
# Run as root. It waits up to WAIT_SECONDS (default 1800) for the Docker daemon
# to create DOCKER-USER, so it can be started from an early boot hook in the
# background. Idempotent: re-running replaces the rules it owns. Persist it with
# whatever your host runs at boot (an early boot hook, with a copy of this script on the boot medium).
#
#   HOST_ADDR=192.0.2.10 PROXY_ADDR=192.0.2.1 ./docker-user-allowlist.sh
set -euo pipefail

HOST_ADDR="${HOST_ADDR:?this host's LAN address the ports are published on}"
PROXY_ADDR="${PROXY_ADDR:?the reverse proxy's address}"
PORTS="${PORTS:-8480 8481}"
WAIT_SECONDS="${WAIT_SECONDS:-1800}"
TAG="scriptorium-allowlist"

deadline=$(( $(date +%s) + WAIT_SECONDS ))
until iptables -nL DOCKER-USER >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "DOCKER-USER chain missing after ${WAIT_SECONDS}s; is Docker running?" >&2
    exit 1
  fi
  sleep 5
done

# Drop the rules from a previous run (matched by their comment), then re-add.
while read -r num; do
  [ -n "$num" ] && iptables -D DOCKER-USER "$num"
done < <(iptables -nL DOCKER-USER --line-numbers | awk -v t="$TAG" '$0 ~ t {print $1}' | sort -rn)

for port in $PORTS; do
  iptables -I DOCKER-USER -p tcp \
    -m conntrack --ctdir ORIGINAL --ctorigdst "$HOST_ADDR" --ctorigdstport "$port" \
    ! -s "$PROXY_ADDR" \
    -m comment --comment "$TAG" -j DROP
done
iptables -nL DOCKER-USER --line-numbers | grep "$TAG"
