#!/usr/bin/env bash
# Create or update a Scriptorium administrator in the `scriptorium` Keycloak realm.
#
# Runs INSIDE the keycloak container (the compose example mounts it read-only at
# /opt/keycloak/scriptorium/create-admin.sh):
#
#   docker compose -f compose.yml --profile keycloak exec keycloak \
#     bash /opt/keycloak/scriptorium/create-admin.sh [--reset-password] [--email ADDRESS] USERNAME
#
# What it does:
#   * new user: creates USERNAME (enabled, email_verified=true, required action
#     UPDATE_PASSWORD), grants realm role `scriptorium-admin`, sets a generated
#     temporary password and prints it ONCE on stdout;
#   * existing user: re-grants `scriptorium-admin` and marks the email verified;
#     the password is left alone unless --reset-password is given, in which case a
#     new temporary password is generated and printed once.
#
# It authenticates to the master realm with $KCADM_USER / $KCADM_PASSWORD, falling
# back to the bootstrap admin from the container environment
# (KC_BOOTSTRAP_ADMIN_USERNAME / KC_BOOTSTRAP_ADMIN_PASSWORD). If no password is
# available, kcadm.sh prompts for it (needs a TTY, which `docker compose exec` has).
#
# The generated password is never written to a file, a log or a process argument:
# it travels to Keycloak on kcadm.sh's stdin and is printed once to your terminal.
# The script does not re-enable a user an operator has disabled.
# Remember to add the user's email (or `sub`) to SCRIPTORIUM_OIDC_ALLOWED_USERS.

set -euo pipefail
set +x

readonly REALM="scriptorium"
readonly ROLE="scriptorium-admin"
readonly KCADM="/opt/keycloak/bin/kcadm.sh"
readonly SERVER="${KCADM_SERVER:-http://127.0.0.1:8080}"

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 64
}

reset_password=0
email=""
username=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --reset-password) reset_password=1 ;;
        --email)
            [ "$#" -ge 2 ] || usage
            email="$2"
            shift
            ;;
        -h | --help) usage ;;
        -*) echo "unknown option: $1" >&2; usage ;;
        *)
            [ -z "$username" ] || usage
            username="$1"
            ;;
    esac
    shift
done
[ -n "$username" ] || usage

# Keep identifiers boring: they end up in kcadm query strings.
if ! [[ "$username" =~ ^[A-Za-z0-9._@+-]{1,128}$ ]]; then
    echo "username must match [A-Za-z0-9._@+-]{1,128}" >&2
    exit 65
fi
if [ -z "$email" ] && [[ "$username" == *@* ]]; then
    email="$username"
fi
if [ -n "$email" ] && ! [[ "$email" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
    echo "not an email address: $email" >&2
    exit 65
fi

config="$(mktemp /tmp/kcadm.XXXXXXXX)"
trap 'rm -f "$config"' EXIT

kc() {
    "$KCADM" "$@" --config "$config"
}

gen_password() {
    # 48 random bytes -> base64 -> alphanumerics only; 24 characters (~142 bits).
    local raw
    raw="$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
    if [ "${#raw}" -lt 24 ]; then
        echo "could not generate a password" >&2
        exit 70
    fi
    printf '%s' "${raw:0:24}"
}

set_temporary_password() {
    local user_id="$1" password="$2"
    printf '{"type":"password","temporary":true,"value":"%s"}' "$password" |
        kc update "users/${user_id}/reset-password" -r "$REALM" -f - >/dev/null
}

admin_user="${KCADM_USER:-${KC_BOOTSTRAP_ADMIN_USERNAME:-}}"
admin_password="${KCADM_PASSWORD:-${KC_BOOTSTRAP_ADMIN_PASSWORD:-}}"
if [ -z "$admin_user" ]; then
    echo "set KCADM_USER (and optionally KCADM_PASSWORD) to a master-realm admin" >&2
    exit 64
fi
if [ -n "$admin_password" ]; then
    # kcadm.sh only takes the password as an argument. It is the master admin's own
    # password, already present in this container's environment, so the argument
    # reveals nothing a process in the container could not read anyway.
    kc config credentials --server "$SERVER" --realm master --user "$admin_user" \
        --password "$admin_password" >/dev/null
else
    kc config credentials --server "$SERVER" --realm master --user "$admin_user" >/dev/null
fi
unset admin_password KCADM_PASSWORD KC_BOOTSTRAP_ADMIN_PASSWORD

user_id="$(kc get users -r "$REALM" -q "username=${username}" -q exact=true \
    --fields id --format csv --noquotes | head -n 1)"

new_password=""
if [ -z "$user_id" ]; then
    create_args=(-s "username=${username}" -s enabled=true -s emailVerified=true
        -s 'requiredActions=["UPDATE_PASSWORD"]')
    if [ -n "$email" ]; then
        create_args+=(-s "email=${email}")
    fi
    user_id="$(kc create users -r "$REALM" "${create_args[@]}" -i)"
    new_password="$(gen_password)"
    set_temporary_password "$user_id" "$new_password"
    echo "created user ${username} (id ${user_id})" >&2
else
    update_args=(-s emailVerified=true)
    if [ -n "$email" ]; then
        update_args+=(-s "email=${email}")
    fi
    kc update "users/${user_id}" -r "$REALM" "${update_args[@]}"
    if [ "$reset_password" -eq 1 ]; then
        new_password="$(gen_password)"
        set_temporary_password "$user_id" "$new_password"
        echo "reset the password of ${username} (id ${user_id})" >&2
    else
        echo "user ${username} exists (id ${user_id}); password unchanged" >&2
    fi
fi

kc add-roles -r "$REALM" --uid "$user_id" --rolename "$ROLE"
echo "granted ${ROLE} to ${username}" >&2

if [ -n "$new_password" ]; then
    # The only place the password is ever shown. It is temporary: Keycloak forces a
    # change at first login.
    printf 'Temporary password for %s: %s\n' "$username" "$new_password"
fi
