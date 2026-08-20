#!/usr/bin/env bash
set -euo pipefail

target="${1:?target database path is required}"
source="${2:?rollback source database path is required}"

cp "$source" "$target"
printf 'ROLLBACK_TARGET=%s\n' "$target"
printf 'ROLLBACK_SOURCE=%s\n' "$source"
sha256sum "$target" | sed 's/^/ROLLBACK_SHA256=/'
