#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BASELINE_COMMIT="2e926b8f15cde78b823c3f25aa3e6c9a6cc73983"
DEFAULT_TARGET="$REPO_ROOT/src/hidemyemail_generator/vendor/gptfree_register/core/chatgpt_register.py"
TARGET="${1:-$DEFAULT_TARGET}"

if [[ "$TARGET" != /* && ! "$TARGET" =~ ^[A-Za-z]:[/\\] ]]; then
  TARGET="$REPO_ROOT/$TARGET"
fi

case "$TARGET" in
  "$REPO_ROOT"/*) ;;
  *)
    echo "rollback refused: target is outside repository" >&2
    exit 64
    ;;
esac

if [[ -d "$TARGET" ]]; then
  paths=(
    "src/hidemyemail_generator/vendor/gptfree_register/core/chatgpt_register.py"
    "src/hidemyemail_generator/protocol_credentials.py"
    "src/hidemyemail_generator/protocol_registration_worker.py"
  )
  expected_blobs=(
    "8fa67134968b0cf3a0fbc48c605e087c2dbdcfaa"
    "cc61e669ba5516d61999c6dc17384e722f741ebf"
    "26745da5ad9515250ef9bf91f89e3a4a6167986b"
  )
  for index in "${!paths[@]}"; do
    relative_path="${paths[$index]}"
    restored_file="$TARGET/$relative_path"
    mkdir -p "$(dirname "$restored_file")"
    git -C "$REPO_ROOT" show "$BASELINE_COMMIT:$relative_path" > "$restored_file"
    actual_blob="$(git hash-object "$restored_file")"
    if [[ "$actual_blob" != "${expected_blobs[$index]}" ]]; then
      echo "rollback hash mismatch: $relative_path" >&2
      exit 65
    fi
    echo "restored=$restored_file"
    echo "blob_sha1=$actual_blob"
  done
  echo "ROLLBACK_RESULT=restored-original-bundle"
  echo "RESTORED_BEHAVIOR=password-add baseline; old-session token refresh baseline; reauth retry state baseline"
  exit 0
fi

cp "$SCRIPT_DIR/ORIGINAL_FILE" "$TARGET"
echo "restored=$TARGET"
sha256sum "$TARGET"
