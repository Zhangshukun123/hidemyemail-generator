#!/bin/sh
set -eu

source_config=/tmp/Caddyfile.public.code-portal
live_config=/opt/caddy/Caddyfile
replacement=/tmp/Caddyfile.code-portal.block
candidate=/tmp/Caddyfile.code-portal.candidate
backup="/opt/caddy/Caddyfile.bak-code-portal-$(date +%Y%m%d%H%M%S)"
site='icloud-code.8-208-13-52.sslip.io {'

extract_block() {
    awk -v site="$site" '
        $0 == site { active = 1; depth = 0 }
        active {
            print
            line = $0
            opens = gsub(/\{/, "{", line)
            line = $0
            closes = gsub(/\}/, "}", line)
            depth += opens - closes
            if (depth == 0) exit
        }
    ' "$1"
}

extract_block "$source_config" > "$replacement"
test -s "$replacement"

awk -v site="$site" -v replacement="$replacement" '
    BEGIN {
        while ((getline line < replacement) > 0) desired = desired line ORS
        close(replacement)
    }
    $0 == site {
        printf "%s", desired
        skipping = 1
        depth = 1
        next
    }
    skipping {
        line = $0
        opens = gsub(/\{/, "{", line)
        line = $0
        closes = gsub(/\}/, "}", line)
        depth += opens - closes
        if (depth == 0) skipping = 0
        next
    }
    { print }
' "$live_config" > "$candidate"

cp "$live_config" "$backup"
cp "$candidate" "$live_config"
if ! docker exec caddy caddy validate --config /etc/caddy/Caddyfile; then
    cp "$backup" "$live_config"
    exit 1
fi
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
rm -f "$source_config" "$replacement" "$candidate"
printf 'CADDY_PORTAL_OK\n'
