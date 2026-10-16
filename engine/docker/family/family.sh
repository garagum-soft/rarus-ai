#!/usr/bin/env bash
# Raven for a household: one container per person on one server.
#
# One Raven has no accounts: everyone signed in to it sees every conversation,
# file and model key. So each member gets a Raven of their own -- their own
# conversations, memory, files, keys and sign-in link -- from one shared image.
#
#   ./family.sh build                           build the image from this checkout
#   ./family.sh add NAME [--port N] [--size 20G]
#   ./family.sh up [NAME...]                    start members (all when none named)
#   ./family.sh down [NAME...]                  stop members (data stays)
#   ./family.sh signin NAME                     a one-time sign-in link for NAME
#   ./family.sh list                            members, ports, state, disk use
#   ./family.sh logs NAME
#   ./family.sh exec NAME ARGS...               a raven command in NAME's container
#   ./family.sh remove NAME [--purge]           forget a member (--purge: and their data)
#
# Settings, all optional, from the environment:
#   FAMILY_HOST        the address sign-in links name (default: the server's first IP)
#   FAMILY_BIND        the interface ports listen on (default 0.0.0.0, the home network)
#   FAMILY_FIRST_PORT  the first member's port (default 18801)
#   RAVEN_IMAGE        the shared image's tag (default raven:family)
#   RAVEN_EXTRAS       extras built into it (default channels,tools,claude)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
MEMBERS="${FAMILY_MEMBERS_DIR:-$HERE/members}"
DATA="${FAMILY_DATA_DIR:-$HERE/data}"
RAVEN_IMAGE="${RAVEN_IMAGE:-raven:family}"
FAMILY_BIND="${FAMILY_BIND:-0.0.0.0}"
FIRST_PORT="${FAMILY_FIRST_PORT:-18801}"
EXTRAS="${RAVEN_EXTRAS:-channels,tools,claude}"
PLUGINS="${RAVEN_PLUGINS:-everos-memory}"

die() { printf 'family: %s\n' "$*" >&2; exit 1; }
say() { printf 'family: %s\n' "$*"; }

valid_name() { [[ "$1" =~ ^[a-z][a-z0-9-]{0,19}$ ]]; }

need_member() {
    valid_name "${1:-}" || die "name a member (lower-case letters, digits and dashes): got '${1:-}'"
    [ -f "$MEMBERS/$1.env" ] || die "no member '$1' -- add one with: $0 add $1"
}

port_of() { sed -n 's/^RAVEN_WEB_PORT=//p' "$MEMBERS/$1.env" | head -n 1; }

members() {
    local f
    for f in "$MEMBERS"/*.env; do
        [ -e "$f" ] || continue
        basename "$f" .env
    done
}

next_port() {
    local top=$((FIRST_PORT - 1)) name port
    for name in $(members); do
        port="$(port_of "$name")"
        [ -n "$port" ] && [ "$port" -gt "$top" ] && top="$port"
    done
    printf '%s' $((top + 1))
}

server_host() {
    if [ -n "${FAMILY_HOST:-}" ]; then
        printf '%s' "$FAMILY_HOST"
        return
    fi
    local addr=""
    addr="$(hostname -I 2>/dev/null | awk '{print $1}')" || addr=""
    printf '%s' "${addr:-localhost}"
}

# Compose for one member: its own project name, port, env file and data.
compose() {
    local name="$1"
    shift
    RAVEN_IMAGE="$RAVEN_IMAGE" FAMILY_BIND="$FAMILY_BIND" RAVEN_WEB_PORT="$(port_of "$name")" \
        FAMILY_MEMBER_ENV="$MEMBERS/$name.env" FAMILY_DATA="$DATA/$name" \
        docker compose -p "raven-$name" -f "$HERE/compose.yml" "$@"
}

# A fixed-size disk for one member: a file formatted ext4 and loop-mounted on
# their data directory, so their Raven cannot grow past it. Needs root, and is
# added to /etc/fstab so it is mounted again after a reboot.
make_disk() {
    local name="$1" size="$2" img dir
    [[ "$size" =~ ^[0-9]+[MG]$ ]] || die "--size takes a number and M or G, like 20G: got '$size'"
    [ "$(id -u)" -eq 0 ] || die "--size makes a disk image and mounts it, which needs root: sudo $0 add $name --size $size"
    img="$DATA/$name.img"
    dir="$DATA/$name"
    [ -e "$img" ] && die "$img already exists"
    mkdir -p "$dir"
    fallocate -l "$size" "$img" 2>/dev/null || truncate -s "$size" "$img"
    mkfs.ext4 -q -F "$img"
    mount -o loop "$img" "$dir"
    printf '%s %s ext4 loop,nofail 0 0\n' "$img" "$dir" >>/etc/fstab
    say "$name's data is limited to $size ($img, mounted on $dir)"
}

cmd_build() {
    say "building $RAVEN_IMAGE from $REPO (extras: $EXTRAS)"
    docker build -t "$RAVEN_IMAGE" -f "$REPO/Dockerfile" \
        --build-arg RAVEN_EXTRAS="$EXTRAS" --build-arg RAVEN_PLUGINS="$PLUGINS" "$REPO"
}

cmd_add() {
    local name="${1:-}" port="" size="" other tz
    [ $# -gt 0 ] && shift
    valid_name "$name" || die "a member name is lower-case letters, digits and dashes, starting with a letter: got '$name'"
    [ -f "$MEMBERS/$name.env" ] && die "'$name' already exists"
    while [ $# -gt 0 ]; do
        case "$1" in
            --port) port="${2:-}"; shift 2 || die "--port needs a number" ;;
            --size) size="${2:-}"; shift 2 || die "--size needs a value, like 20G" ;;
            *) die "unknown option '$1'" ;;
        esac
    done
    [ -n "$port" ] || port="$(next_port)"
    [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] || die "a port is a number from 1024 to 65535: got '$port'"
    for other in $(members); do
        [ "$(port_of "$other")" = "$port" ] && die "port $port is already $other's"
    done
    # Everything that can refuse has run; only now is anything written.
    if [ -n "$size" ]; then
        make_disk "$name" "$size"
    else
        mkdir -p "$DATA/$name"
    fi
    tz="${TZ:-$(cat /etc/timezone 2>/dev/null || true)}"
    mkdir -p "$MEMBERS"
    (
        umask 077
        cat >"$MEMBERS/$name.env" <<EOF
# $name's Raven (family.sh). The container reads every line below; a key put
# here is $name's alone -- no other member's Raven reads this file.
RAVEN_WEB_PORT=$port
TZ=${tz:-UTC}

# A model provider from the start. Or leave these and add one in the page's
# Settings > Models after signing in.
# RAVEN_PROVIDER=anthropic
# RAVEN_API_KEY=
EOF
    )
    say "added $name on port $port; start with: $0 up $name"
}

cmd_up() {
    local names name
    names=("$@")
    [ ${#names[@]} -gt 0 ] || mapfile -t names < <(members)
    [ ${#names[@]} -gt 0 ] || die "no members yet -- add one with: $0 add NAME"
    for name in "${names[@]}"; do
        need_member "$name"
        mkdir -p "$DATA/$name"
        compose "$name" up -d
        say "$name: http://$(server_host):$(port_of "$name")  (sign in: $0 signin $name)"
    done
}

cmd_down() {
    local names name
    names=("$@")
    [ ${#names[@]} -gt 0 ] || mapfile -t names < <(members)
    for name in "${names[@]}"; do
        need_member "$name"
        compose "$name" down
    done
}

cmd_signin() {
    local name="${1:-}" url
    need_member "$name"
    url="$(compose "$name" exec -T raven docker-entrypoint.sh signin)" || die "no link: is $name's Raven running? ($0 up $name)"
    url="$(printf '%s' "$url" | tail -n 1 | tr -d '\r')"
    case "$url" in
        http://localhost:*) url="http://$(server_host):${url#http://localhost:}" ;;
    esac
    say "a one-time link for $name, valid 30 minutes; open it on $name's own device:"
    printf '\n    %s\n\n' "$url"
}

cmd_list() {
    local name port state used limit img
    printf '%-14s %-6s %-10s %-8s %s\n' MEMBER PORT STATE USED LIMIT
    for name in $(members); do
        port="$(port_of "$name")"
        state="$(docker inspect -f '{{.State.Status}}' "raven-$name-raven-1" 2>/dev/null)" || state="stopped"
        used="$(du -sh "$DATA/$name" 2>/dev/null | cut -f1)" || used="?"
        img="$DATA/$name.img"
        if [ -f "$img" ]; then
            limit="$(du -h --apparent-size "$img" 2>/dev/null | cut -f1)" || limit="?"
        else
            limit="none"
        fi
        printf '%-14s %-6s %-10s %-8s %s\n' "$name" "$port" "${state:-stopped}" "${used:-0}" "$limit"
    done
}

cmd_logs() {
    need_member "${1:-}"
    compose "$1" logs --tail "${TAIL:-200}"
}

cmd_exec() {
    local name="${1:-}"
    need_member "$name"
    shift
    compose "$name" exec raven docker-entrypoint.sh "$@"
}

cmd_remove() {
    local name="${1:-}" purge="" answer img dir
    need_member "$name"
    [ "${2:-}" = "--purge" ] && purge=1
    if [ -n "$purge" ]; then
        # Asked before anything is touched: a wrong answer leaves the member as they were.
        read -r -p "family: type '$name' to delete all of $name's conversations, memory and files: " answer || answer=""
        answer="${answer%$'\r'}"
        [ "$answer" = "$name" ] || die "not deleted"
    fi
    compose "$name" down
    rm -f "$MEMBERS/$name.env"
    if [ -z "$purge" ]; then
        say "removed $name; their data stays in $DATA/$name (delete it with --purge)"
        return
    fi
    img="$DATA/$name.img"
    dir="$DATA/$name"
    if [ -f "$img" ]; then
        umount "$dir" 2>/dev/null || true
        sed -i "\|^$img |d" /etc/fstab
        rm -f "$img"
    fi
    rm -rf "$dir"
    say "deleted $name and their data"
}

main() {
    local cmd="${1:-help}"
    [ $# -gt 0 ] && shift
    case "$cmd" in
        build) cmd_build "$@" ;;
        add) cmd_add "$@" ;;
        up) cmd_up "$@" ;;
        down) cmd_down "$@" ;;
        signin) cmd_signin "$@" ;;
        list) cmd_list "$@" ;;
        logs) cmd_logs "$@" ;;
        exec) cmd_exec "$@" ;;
        remove) cmd_remove "$@" ;;
        help | -h | --help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//' ;;
        *) die "unknown command '$cmd' (see: $0 help)" ;;
    esac
}

main "$@"
