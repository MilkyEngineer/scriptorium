#!/bin/sh
# Move the `scriptorium-admin` role scope mapping from the `scriptorium` client scope to
# the two Scriptorium clients, for realms imported from an export older than 2026-10-18.
#
# Why: Keycloak applies a client scope that has role scope mappings only to users who
# hold one of those roles. With the mapping on the `scriptorium` scope, non-admin users
# received access tokens without the audience and Scriptorium answered 401.
#
# Run inside the Keycloak container, which has the bootstrap admin in its environment:
#   docker compose exec -T keycloak sh -s < docker/keycloak/move-admin-scope-mapping.sh
# Idempotent: re-running reports the mappings and changes nothing.
set -eu
KC=/opt/keycloak/bin/kcadm.sh
R=scriptorium
ROLE=scriptorium-admin
$KC config credentials --server http://127.0.0.1:8080 --realm master \
  --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" </dev/null >/dev/null
ROLE_ID=$($KC get "roles/$ROLE" -r $R --fields id --format csv --noquotes </dev/null)
BODY="[{\"id\":\"$ROLE_ID\",\"name\":\"$ROLE\"}]"
# The image has no awk: csv lines are "id,name".
SID=$($KC get client-scopes -r $R --fields id,name --format csv --noquotes </dev/null | grep ',scriptorium$' | cut -d, -f1)
[ -n "$SID" ] || { echo "client scope scriptorium not found"; exit 1; }
if $KC get "client-scopes/$SID/scope-mappings/realm" -r $R --fields name --format csv --noquotes </dev/null | grep -qx "$ROLE"; then
  $KC delete "client-scopes/$SID/scope-mappings/realm" -r $R -b "$BODY" </dev/null
  echo "removed $ROLE from client scope scriptorium"
else
  echo "client scope scriptorium: no $ROLE mapping (ok)"
fi
for C in scriptorium-claude scriptorium-claude-code; do
  CID=$($KC get clients -r $R -q "clientId=$C" --fields id --format csv --noquotes </dev/null)
  if $KC get "clients/$CID/scope-mappings/realm" -r $R --fields name --format csv --noquotes </dev/null | grep -qx "$ROLE"; then
    echo "client $C: $ROLE already mapped (ok)"
  else
    $KC create "clients/$CID/scope-mappings/realm" -r $R -b "$BODY" </dev/null
    echo "client $C: mapped $ROLE"
  fi
done
