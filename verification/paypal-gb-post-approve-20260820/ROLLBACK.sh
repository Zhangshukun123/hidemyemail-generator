#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASELINE_DIR="$SCRIPT_DIR/before"
TARGET_DIR="${1:-}"

if [[ -z "$TARGET_DIR" ]]; then
  echo "usage: ROLLBACK.sh TARGET_WORKTREE" >&2
  exit 64
fi
if [[ ! -d "$TARGET_DIR" ]]; then
  echo "rollback target is not a directory: $TARGET_DIR" >&2
  exit 66
fi
if [[ ! -d "$BASELINE_DIR" ]]; then
  echo "rollback baseline snapshots are missing: $BASELINE_DIR" >&2
  exit 66
fi
TARGET_DIR="$(cd -- "$TARGET_DIR" && pwd)"

check_hash() {
  local expected="$1"
  local relative_path="$2"
  local actual
  if [[ ! -f "$TARGET_DIR/$relative_path" ]]; then
    echo "rollback verification failed: missing $relative_path" >&2
    exit 1
  fi
  actual="$(sha256sum "$TARGET_DIR/$relative_path" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then
    echo "rollback verification failed: $relative_path hash=$actual" >&2
    exit 1
  fi
}

# Refuse a partial or unrelated target before changing any file.
check_hash "0727763ff2fabc8ef85c2224d0b322c8a06655c4b3d32e3a95e9e90454830da3" "src/hidemyemail_generator/_card_link_payment_modes.py"
check_hash "a5a9b022b913a3eaa671b57336a80d74cfcebdbcf76c6210381d12dcefce8bc9" "src/hidemyemail_generator/card_link_runtime.py"
check_hash "42c2417c68f568ea4cb020d2b37288a0ecaab5f90f7ab0842b837fd49364f9da" "src/hidemyemail_generator/openai_card_link_bridge.py"
check_hash "b638f1a5833964c3637f9ac31e37a2a1fac7d23740824dcd21cdd61799861a9c" "src/hidemyemail_generator/paypal_protocol_profile.py"
check_hash "82d74fe903e65f55f85df4710144abf60f03a09edcfcb2ea06e6a5d98e73707d" "src/hidemyemail_generator/webapp.py"
check_hash "43567834dc9df02c2cdcd4e7a804c7e14391a457c8e79746d28cd164f1cf7c93" "src/hidemyemail_generator/web_ui/static/app.js"
check_hash "87ebff4d5c6068390fd807f258199d8b5d10cd6cbb896ac1e55ba004bef26e48" "tests/test_pay153_two_proxy_protocol.py"
check_hash "6443368621c3e20a5e6a9d9b54f2c4541b7c3d3c22920a5b7d940f5220dcc645" "tests/test_paypal_checkout_sentinel.py"
check_hash "3d203973cfbf584772b08bd1306c4881ac3f24ffd35d11c82240ad41017d7644" "tests/test_paypal_two_proxy_flow.py"
check_hash "4ce2fe4f88c7bd452c348de3588ab772526c87599c107ba5bba153a648d380aa" "tests/test_quick_flow_config_ui.py"
check_hash "610ba9553cd8472ee21c98e2356c6f5aad41c71e74f40d3d9549178fbdb42104" "tests/test_card_link_bridge.py"
check_hash "66da427e0cb3304a14264be5d1a9b45b7ef5f5667371812ef12a826cb4fa1fab" "src/hidemyemail_generator/paypal_gb_post_approve_flow.py"
check_hash "5e2e20f3c935913bdc6f4cf4062f7bb31fb0056897e4ced146039e381397914a" "tests/test_paypal_gb_post_approve_flow.py"
check_hash "500ac4250990b33662dc6ab4426f0d828331d5a68beaa012aea079b2760413ae" "tests/test_card_link_browser_http.py"
check_hash "a99fdad7d9959f52ae345c2d4104c5bd03e2e1ff9847e3b206cefc51d8bcd4e9" "tests/test_paypal_gb_runtime_helpers.py"

existing_files=(
  "src/hidemyemail_generator/_card_link_payment_modes.py"
  "src/hidemyemail_generator/card_link_runtime.py"
  "src/hidemyemail_generator/openai_card_link_bridge.py"
  "src/hidemyemail_generator/paypal_protocol_profile.py"
  "src/hidemyemail_generator/webapp.py"
  "src/hidemyemail_generator/web_ui/static/app.js"
  "tests/test_pay153_two_proxy_protocol.py"
  "tests/test_paypal_checkout_sentinel.py"
  "tests/test_paypal_two_proxy_flow.py"
  "tests/test_quick_flow_config_ui.py"
  "tests/test_card_link_bridge.py"
)
for relative_path in "${existing_files[@]}"; do
  cp -- "$BASELINE_DIR/$relative_path" "$TARGET_DIR/$relative_path"
done

added_files=(
  "src/hidemyemail_generator/paypal_gb_post_approve_flow.py"
  "tests/test_paypal_gb_post_approve_flow.py"
  "tests/test_card_link_browser_http.py"
  "tests/test_paypal_gb_runtime_helpers.py"
)
for relative_path in "${added_files[@]}"; do
  rm -f -- "$TARGET_DIR/$relative_path"
done

check_hash "a46d3dbd25534647b497251043174f7921665f813fc9df3f27e5253e8231250f" "src/hidemyemail_generator/_card_link_payment_modes.py"
check_hash "3c9a882cca05a890798d4aa3d1391b65ad5e866b082e523e2c9bb413332e4c90" "src/hidemyemail_generator/card_link_runtime.py"
check_hash "46a3fdccb487fcc3c95b98b4332153167f52dbf626afcffba2c1a6fe194afbf5" "src/hidemyemail_generator/openai_card_link_bridge.py"
check_hash "29af3598faa79a8eb24c66906a467ee64f928558ce0cf8fec53cd70568102bfe" "src/hidemyemail_generator/paypal_protocol_profile.py"
check_hash "29a0cf35b3365948ad8bb9b321da50c1111fec409646a927964d5e7094a3df12" "src/hidemyemail_generator/webapp.py"
check_hash "548c1270c91e6dbe3ed381f7d0b51ab5a8c804020f0450ea225ea109b57ac898" "src/hidemyemail_generator/web_ui/static/app.js"
check_hash "2dcf22c538cf2d852023f1642c0dcd9268a6481b6ae22a8c052a9fa69e273332" "tests/test_pay153_two_proxy_protocol.py"
check_hash "9568371ae12fcb07944e0ca7a50818c62c1d7e53ac7a51f153f976217c3e4174" "tests/test_paypal_checkout_sentinel.py"
check_hash "761cab7ba50627b52dc2dbe662b22e8458d7d95fa70b0eaa5c40b76478d54124" "tests/test_paypal_two_proxy_flow.py"
check_hash "597297ad7482318b5dbbf1fb168483a14f646ecdb854cf61a7ebb8f3c9bb01b6" "tests/test_quick_flow_config_ui.py"
check_hash "0db23b8c9b06738245af26163bfe10145b242bd1b5aacc0debe003b969eefe23" "tests/test_card_link_bridge.py"

for relative_path in "${added_files[@]}"; do
  if [[ -e "$TARGET_DIR/$relative_path" ]]; then
    echo "rollback verification failed: added file remains: $relative_path" >&2
    exit 1
  fi
done

echo "ROLLBACK_RESULT=restored"
echo "ROLLBACK_STATUS=baseline-hashes-match;added-files-absent"
echo "RESTORED_GB_BEHAVIOR=pool2-pre-confirm-update-taxes-confirm-approve"
echo "RESTORED_US_BEHAVIOR=pay153-legacy-order-preserved"
echo "ROLLBACK_TARGET=$TARGET_DIR"
