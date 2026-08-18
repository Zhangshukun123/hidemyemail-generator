#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TARGET="${1:-$SCRIPT_DIR/MODIFIED_FILE.json}"
EXPECTED_MODIFIED="18A8119CEF8542DB24177F4BD985661DF5932261085024075C5813AF839FAE69"
EXPECTED_ORIGINAL="4CA4D04F56800E837314C15DA40C9B7D24D122DF53116A442C5A3EB9C07ABFFC"

ACTUAL_MODIFIED="$(sha256sum "$TARGET" | awk '{print toupper($1)}')"
if [ "$ACTUAL_MODIFIED" != "$EXPECTED_MODIFIED" ]; then
  printf 'ROLLBACK_RESULT=hash_mismatch expected=%s actual=%s\n' "$EXPECTED_MODIFIED" "$ACTUAL_MODIFIED" >&2
  exit 2
fi

cp "$SCRIPT_DIR/ORIGINAL_FILE.json" "$TARGET"
ACTUAL_ORIGINAL="$(sha256sum "$TARGET" | awk '{print toupper($1)}')"
if [ "$ACTUAL_ORIGINAL" != "$EXPECTED_ORIGINAL" ]; then
  printf 'ROLLBACK_RESULT=restore_hash_mismatch expected=%s actual=%s\n' "$EXPECTED_ORIGINAL" "$ACTUAL_ORIGINAL" >&2
  exit 3
fi

printf 'ROLLBACK_INPUT=%s\n' "$TARGET"
printf 'ROLLBACK_RESULT=restored\n'
printf 'ROLLBACK_BRANCH=US\n'
printf 'ROLLBACK_FIELD=upstream_update_detected\n'
printf 'ROLLBACK_VALUE=null\n'
printf 'ROLLBACK_SHA256=%s\n' "$ACTUAL_ORIGINAL"
