#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="$SCRIPT_DIR/DIFF_FILE"
BASELINE_DIR="$SCRIPT_DIR/BASELINE"
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
if [[ ! -d "$BASELINE_DIR" ]]; then
  echo "rollback baseline snapshots are missing: $BASELINE_DIR" >&2
  exit 66
fi
TARGET_DIR="$(cd -- "$TARGET_DIR" && pwd)"

check_hash() {
  local expected="$1"
  local relative_path="$2"
  local actual
  actual="$(sha256sum "$TARGET_DIR/$relative_path" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then
    echo "rollback verification failed: $relative_path hash=$actual" >&2
    exit 1
  fi
}

# Refuse a partial or unrelated target before changing any file.
check_hash "29a0cf35b3365948ad8bb9b321da50c1111fec409646a927964d5e7094a3df12" "src/hidemyemail_generator/webapp.py"
check_hash "b8b55ab739498ba0c05dd16209d89783e2749e71813f8144ce0f4801762cd6f0" "paypal-agreement-protocol/web.py"
check_hash "b5c5723215c20fdc284a27225fc6d28c3721026196647a47d9c2b49712b3a108" "paypal-agreement-protocol/paypal/flow.py"
check_hash "09d08f55b085bcb706b2b327e86e9be8560d0903db1b4f5d01fc3e1d187ead1f" "paypal-agreement-protocol/paypal/elevation_flow.py"
check_hash "52d33e43ce6d8514d7b2eb3fe140608c74f73c0f538b3db7b979a41641cc5e3e" "tests/test_webapp_stdio.py"
check_hash "df138975f122e8e00e6ff36fd7c3786e980fd13352f228da53cc3edf4d4d3561" "paypal-agreement-protocol/tests/test_us_email_first_integration.py"
check_hash "cba81bbc3683351541bf93c09224ee12344109cf385e379cafbf7deda9c93b97" "paypal-agreement-protocol/tests/test_us_onboarding_compat.py"
check_hash "85f9ccb46bbd2f957025ec29dd4f6bca6064b769bdc5d9b8dd88a3afe8f1ccf0" "src/hidemyemail_generator/paypal_payment_protocol.py"
check_hash "fc6e9268a8b17a6a073b0c8698266b333cbeb2df90ba83c1d53c450869c04961" "paypal-agreement-protocol/paypal/payment_protocol.py"
check_hash "2d6134dea81156e6c9e2a0fe0e5c8d051f04464205ff792d0f341e30d4aff994" "tests/test_paypal_payment_protocol.py"

cp -- "$BASELINE_DIR/src__hidemyemail_generator__webapp.py" \
  "$TARGET_DIR/src/hidemyemail_generator/webapp.py"
cp -- "$BASELINE_DIR/paypal-agreement-protocol__web.py" \
  "$TARGET_DIR/paypal-agreement-protocol/web.py"
cp -- "$BASELINE_DIR/paypal-agreement-protocol__paypal__flow.py" \
  "$TARGET_DIR/paypal-agreement-protocol/paypal/flow.py"
cp -- "$BASELINE_DIR/paypal-agreement-protocol__paypal__elevation_flow.py" \
  "$TARGET_DIR/paypal-agreement-protocol/paypal/elevation_flow.py"
cp -- "$BASELINE_DIR/tests__test_webapp_stdio.py" \
  "$TARGET_DIR/tests/test_webapp_stdio.py"
cp -- "$BASELINE_DIR/paypal-agreement-protocol__tests__test_us_email_first_integration.py" \
  "$TARGET_DIR/paypal-agreement-protocol/tests/test_us_email_first_integration.py"
cp -- "$BASELINE_DIR/paypal-agreement-protocol__tests__test_us_onboarding_compat.py" \
  "$TARGET_DIR/paypal-agreement-protocol/tests/test_us_onboarding_compat.py"

rm -f -- \
  "$TARGET_DIR/src/hidemyemail_generator/paypal_payment_protocol.py" \
  "$TARGET_DIR/paypal-agreement-protocol/paypal/payment_protocol.py" \
  "$TARGET_DIR/tests/test_paypal_payment_protocol.py"

check_hash "08051dfa24b9374d10aed7d37f08b6625b518258c561b775dbc0ab758440451c" "src/hidemyemail_generator/webapp.py"
check_hash "ccd138ad9ca08650b46a45adbb3e2ea2ece3d7ec0dd3a796660c5fcf11b59877" "paypal-agreement-protocol/web.py"
check_hash "6f78ec6e804679b54cdf537833dc982eb398d954669010da5352b8e5fea832df" "paypal-agreement-protocol/paypal/flow.py"
check_hash "8f51f6f855ed65c92dc06c9995eb9833c55e87c2421850d09cca3fa937c31df5" "paypal-agreement-protocol/paypal/elevation_flow.py"
check_hash "3f5f9a7668071274faa71f9e6625813f6c2c7c96819252e88a0f16f4db8bc903" "tests/test_webapp_stdio.py"
check_hash "ef911bcfc297e64fd39ddb8f2bd3440eac8f087ade5dafd225adf2de2f8627cd" "paypal-agreement-protocol/tests/test_us_email_first_integration.py"
check_hash "14ab08817532615e2d04ab104bd189a020b9375aebd550c7a93e437da303b114" "paypal-agreement-protocol/tests/test_us_onboarding_compat.py"

added_files=(
  "src/hidemyemail_generator/paypal_payment_protocol.py"
  "paypal-agreement-protocol/paypal/payment_protocol.py"
  "tests/test_paypal_payment_protocol.py"
)
for relative_path in "${added_files[@]}"; do
  if [[ -e "$TARGET_DIR/$relative_path" ]]; then
    echo "rollback verification failed: added file remains: $relative_path" >&2
    exit 1
  fi
done

echo "ROLLBACK_RESULT=restored"
echo "ROLLBACK_STATUS=baseline-hashes-match;added-files-absent"
echo "RESTORED_PAYMENT_PROTOCOL=current"
echo "RESTORED_US_BEHAVIOR=email-first"
echo "ROLLBACK_TARGET=$TARGET_DIR"
