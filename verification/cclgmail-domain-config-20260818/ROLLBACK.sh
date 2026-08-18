#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
TARGET_RELATIVE="verification/cclgmail-domain-config-20260818/ROLLBACK_COPY"
TARGET_ROOT="$WORKSPACE_ROOT/$TARGET_RELATIVE"
TARGET_FILE="$TARGET_ROOT/src/hidemyemail_generator/zkgmail.py"
EXPECTED_ORIGINAL_BLOB="15fa7950e43615f640f57249d8eddf912c594cec"

mkdir -p "$(dirname "$TARGET_FILE")"
cp "$SCRIPT_DIR/MODIFIED_FILE" "$TARGET_FILE"
git -C "$WORKSPACE_ROOT" apply \
  --reverse \
  --directory="$TARGET_RELATIVE" \
  "$SCRIPT_DIR/DIFF_FILE"

ACTUAL_BLOB="$(git hash-object "$TARGET_FILE")"
if [[ "$ACTUAL_BLOB" != "$EXPECTED_ORIGINAL_BLOB" ]]; then
  echo "ROLLBACK hash mismatch: expected=$EXPECTED_ORIGINAL_BLOB actual=$ACTUAL_BLOB" >&2
  exit 1
fi

echo "ROLLBACK restored src/hidemyemail_generator/zkgmail.py blob=$ACTUAL_BLOB"
