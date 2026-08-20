#!/usr/bin/env bash
set -euo pipefail
TARGET_ROOT="${1:-.}"
if command -v cygpath >/dev/null 2>&1; then
  TARGET_ROOT="$(cygpath -u "$TARGET_ROOT")"
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
patch --batch --dry-run --reverse -p1 -d "$TARGET_ROOT" < "$SCRIPT_DIR/DIFF_FILE.patch" >/dev/null
patch --batch --reverse -p1 -d "$TARGET_ROOT" < "$SCRIPT_DIR/DIFF_FILE.patch"
for RELATIVE_PATH in src/hidemyemail_generator/zkgmail.py tests/test_zkgmail.py; do
  FILE_PATH="$TARGET_ROOT/$RELATIVE_PATH"
  awk '{ sub(/\r$/, ""); printf "%s\r\n", $0 }' "$FILE_PATH" > "$FILE_PATH.rollback-tmp"
  mv "$FILE_PATH.rollback-tmp" "$FILE_PATH"
done
echo "ROLLBACK_RESULT=restored"
echo "ROLLBACK_TARGET=$TARGET_ROOT"
echo "ROLLBACK_SOURCE_SHA256=$(sha256sum "$TARGET_ROOT/src/hidemyemail_generator/zkgmail.py" | awk '{print $1}')"
