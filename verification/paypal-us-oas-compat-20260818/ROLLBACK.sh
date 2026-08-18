#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TARGET_ROOT="${1:-D:/AI/hidemyemail-generator}"
PROJECT="$TARGET_ROOT/paypal-agreement-protocol"

check_hash() {
  file="$1"
  expected="$2"
  actual="$(sha256sum "$file" | awk '{print toupper($1)}')"
  if [ "$actual" != "$expected" ]; then
    printf 'ROLLBACK_RESULT=hash_mismatch file=%s expected=%s actual=%s\n' "$file" "$expected" "$actual" >&2
    exit 2
  fi
}

check_hash "$PROJECT/paypal/flow.py" "0A27C9373A8C83295EC2C63786FEEE08531945BB0ABB544742DB525308F7AAB1"
check_hash "$PROJECT/paypal/manual_browser.py" "58162F69596D831A67DCD46021003ED1D110D95231188D67A920579B7A67C770"
check_hash "$PROJECT/web.py" "64342C792155B40690AA11C117940D931564B726ADFA8081B1220161D45ED5EE"
check_hash "$PROJECT/paypal/onboarding_compat.py" "340CE555A7E1EB269A30CEEFEDE119039D36E144B0CBAE580C98550604EBA730"
check_hash "$PROJECT/tests/test_us_onboarding_compat.py" "F78DF61CD0EAEABCC826EA1D8F1F9150133C51634A988888E75081E9EF9CD020"

cp "$SCRIPT_DIR/original/flow.py" "$PROJECT/paypal/flow.py"
cp "$SCRIPT_DIR/original/manual_browser.py" "$PROJECT/paypal/manual_browser.py"
cp "$SCRIPT_DIR/original/web.py" "$PROJECT/web.py"
rm "$PROJECT/paypal/onboarding_compat.py"
rm "$PROJECT/tests/test_us_onboarding_compat.py"

check_hash "$PROJECT/paypal/flow.py" "47312BB6E6498F925CE3C1694EF8EBD98CBE095ADB5E420C5E210950483D8651"
check_hash "$PROJECT/paypal/manual_browser.py" "1EFC37083F329618464CBF9D4AAA298101DFC023E04C1BFA7403E678F70BFE86"
check_hash "$PROJECT/web.py" "A9DE7089CCC251D8499F3DFA60F9032958C63995692F72668698052A88B8D70C"

printf 'ROLLBACK_RESULT=restored\n'
printf 'ROLLBACK_BRANCH=US\n'
printf 'ROLLBACK_FIELD=createMemberAccount/OAS_ERROR\n'
printf 'ROLLBACK_BEHAVIOR=terminal_error\n'
printf 'ROLLBACK_FLOW_SHA256=47312BB6E6498F925CE3C1694EF8EBD98CBE095ADB5E420C5E210950483D8651\n'
