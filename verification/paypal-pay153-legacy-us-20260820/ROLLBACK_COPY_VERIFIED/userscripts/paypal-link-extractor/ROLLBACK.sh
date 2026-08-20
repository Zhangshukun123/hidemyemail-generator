#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET_FILE="${1:-$SCRIPT_DIR/MODIFIED_FILE.user.js}"
ORIGINAL_FILE="${2:-$SCRIPT_DIR/ORIGINAL_FILE_v1.0.0.user.js}"

cp -- "$ORIGINAL_FILE" "$TARGET_FILE"
printf 'ROLLBACK PASS: restored baseline extractor to %s\n' "$TARGET_FILE"
