#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DB="${1:-$SCRIPT_DIR/../../hidemyemail.db}"
BACKUP_DB="${2:-$SCRIPT_DIR/ORIGINAL_DB_COPY}"
mkdir -p "$(dirname "$TARGET_DB")"
cp -f "$BACKUP_DB" "$TARGET_DB"
printf 'ROLLBACK_TARGET=%s\n' "$TARGET_DB"
printf 'ROLLBACK_SOURCE=%s\n' "$BACKUP_DB"
printf 'ROLLBACK_SHA256='
sha256sum "$TARGET_DB"
