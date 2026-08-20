#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="$SCRIPT_DIR/DIFF_FILE"
TARGET_DIR="${1:-}"

if [[ -z "$TARGET_DIR" ]]; then
  echo "usage: ROLLBACK.sh TARGET_WORKTREE" >&2
  exit 64
fi
if [[ ! -d "$TARGET_DIR" ]]; then
  echo "rollback target is not a directory: $TARGET_DIR" >&2
  exit 66
fi
if [[ ! -f "$PATCH_FILE" ]]; then
  echo "rollback patch is missing: $PATCH_FILE" >&2
  exit 66
fi

tracked_files=(
  src/hidemyemail_generator/_card_link_payment_modes.py
  src/hidemyemail_generator/card_link_runtime.py
  src/hidemyemail_generator/openai_card_link_bridge.py
  src/hidemyemail_generator/webapp.py
  src/hidemyemail_generator/web_ui/static/app.js
  src/hidemyemail_generator/web_ui/static/account_actions.js
  tests/test_account_actions.py
  tests/test_card_link_bridge.py
  tests/test_card_link_bridge_service.py
  tests/test_paypal_link_web_ui.py
  tests/test_quick_flow_config_ui.py
  tests/test_web_ui.py
  tests/test_webapp_stdio.py
)
added_files=(
  src/hidemyemail_generator/paypal_protocol_profile.py
  src/hidemyemail_generator/paypal_two_proxy_flow.py
  tests/test_pay153_two_proxy_protocol.py
  tests/test_paypal_two_proxy_flow.py
)

git -C "$TARGET_DIR" apply --check --reverse "$PATCH_FILE"
git -C "$TARGET_DIR" apply --reverse "$PATCH_FILE"

if ! git -C "$TARGET_DIR" diff --quiet -- "${tracked_files[@]}"; then
  echo "rollback verification failed: tracked files differ from HEAD" >&2
  exit 1
fi
for path in "${added_files[@]}"; do
  if [[ -e "$TARGET_DIR/$path" ]]; then
    echo "rollback verification failed: added file remains: $path" >&2
    exit 1
  fi
done

echo "ROLLBACK_RESULT=restored"
echo "ROLLBACK_STATUS=tracked-files-match-HEAD; added-files-absent"
echo "ROLLBACK_TARGET=$TARGET_DIR"
