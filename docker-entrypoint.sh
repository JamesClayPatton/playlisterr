#!/bin/sh
# PUID/PGID/TZ handling, the way every *arr image does it: the person running
# the container should not have to think about which uid owns the bind mount.
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

if [ "$(id -u)" = "0" ]; then
    if [ "$(id -u playlisterr)" != "$PUID" ] || [ "$(id -g playlisterr)" != "$PGID" ]; then
        groupmod -o -g "$PGID" playlisterr 2>/dev/null || true
        usermod -o -u "$PUID" playlisterr 2>/dev/null || true
    fi
    if [ -n "$TZ" ] && [ -f "/usr/share/zoneinfo/$TZ" ]; then
        ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
        echo "$TZ" > /etc/timezone
    fi
    mkdir -p "${PLAYLISTERR_CONFIG_DIR:-/config}"
    # Only the config volume is chowned; the application code stays read-only.
    chown -R "$PUID:$PGID" "${PLAYLISTERR_CONFIG_DIR:-/config}" 2>/dev/null || true
    echo "Playlisterr starting as uid=$PUID gid=$PGID tz=${TZ:-unset}"
    # Drop privileges. setpriv (util-linux) is preferred; fall back to su if
    # the image ever lacks it, and to running as root as a last resort so the
    # container still starts rather than crash-looping.
    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid "$PUID" --regid "$PGID" --init-groups "$@"
    elif command -v su >/dev/null 2>&1; then
        exec su -s /bin/sh -c 'exec "$0" "$@"' playlisterr -- "$@"
    else
        echo "WARNING: no setpriv/su; running as root" >&2
        exec "$@"
    fi
fi

exec "$@"
