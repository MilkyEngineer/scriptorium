#!/bin/bash
# Restrict Scriptorium's published ports to the reverse proxy's address when the
# proxy runs on ANOTHER machine (docker/proxy/README.md, "remote proxy" topology).
#
# Docker's published ports bypass the host's INPUT chain; the only hook that sees
# them is the DOCKER-USER chain, and by then the packet has been DNATed to the
# container port, so the match is on the connection's ORIGINAL destination.
# Replies and container-originated traffic are untouched (-i restricts the rule
# to packets arriving on the LAN interface).
#
# Run as root after the Docker daemon is up (it creates DOCKER-USER). Idempotent:
# re-running replaces the rules it owns. Persist it with whatever your host runs
# after Docker starts (an early boot hook, with a copy of this script on the boot medium).
#
#   LAN_IF=br0 HOST_ADDR=192.0.2.10 PROXY_ADDR=192.0.2.1 ./docker-user-allowlist.sh
set -euo pipefail

LAN_IF="${LAN_IF:?LAN interface, e.g. br0 or eth0}"
HOST_ADDR="${HOST_ADDR:?this host's LAN address the ports are published on}"
PROXY_ADDR="${PROXY_ADDR:?the reverse proxy's address}"
PORTS="${PORTS:-8480 8481}"
TAG="scriptorium-allowlist"

for _ in $(seq 1 60); do
  iptables -nL DOCKER-USER >/dev/null 2>&1 && break
  sleep 2
done
iptables -nL DOCKER-USER >/dev/null 2>&1 || { echo "DOCKER-USER chain missing; is Docker running?" >&2; exit 1; }

# Drop the rules from a previous run (matched by their comment), then re-add.
while read -r num; do
  [ -n "$num" ] && iptables -D DOCKER-USER "$num"
done < <(iptables -nL DOCKER-USER --line-numbers | awk -v t="$TAG" '$0 ~ t {print $1}' | sort -rn)

for port in $PORTS; do
  iptables -I DOCKER-USER -i "$LAN_IF" -p tcp \
    -m conntrack --ctorigdst "$HOST_ADDR" --ctorigdstport "$port" \
    ! -s "$PROXY_ADDR" \
    -m comment --comment "$TAG" -j DROP
done
iptables -nL DOCKER-USER --line-numbers | grep "$TAG"
