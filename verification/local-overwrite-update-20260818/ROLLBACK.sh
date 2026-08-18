#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET=${1:?usage: ROLLBACK.sh TARGET_COPY}
cp "$SCRIPT_DIR/ORIGINAL_FILE" "$TARGET"

if command -v sha256sum >/dev/null 2>&1; then
  RESTORED_SHA256=$(sha256sum "$TARGET" | awk "{print \$1}")
  EXPECTED_SHA256=$(sha256sum "$SCRIPT_DIR/ORIGINAL_FILE" | awk "{print \$1}")
else
  RESTORED_SHA256=$(shasum -a 256 "$TARGET" | awk "{print \$1}")
  EXPECTED_SHA256=$(shasum -a 256 "$SCRIPT_DIR/ORIGINAL_FILE" | awk "{print \$1}")
fi

[ "$RESTORED_SHA256" = "$EXPECTED_SHA256" ]
printf "%s\n" "ROLLBACK_RESULT=restored-original"
printf "%s\n" "RESTORED_FILE=$TARGET"
printf "%s\n" "RESTORED_SHA256=$RESTORED_SHA256"
printf "%s\n" "RESTORED_BEHAVIOR=start-web-ui.ps1;LIANDONG_SHOP_MERCHANT_TOKEN=not-forwarded;status=baseline"
