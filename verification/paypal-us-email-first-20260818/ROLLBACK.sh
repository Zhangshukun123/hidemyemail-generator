#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:?usage: ROLLBACK.sh /path/to/paypal-agreement-protocol}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASELINE="$SCRIPT_DIR/baseline"

test -d "$TARGET/paypal"
test -d "$TARGET/tests"

cp "$BASELINE/paypal-agreement-protocol__paypal__flow.py" "$TARGET/paypal/flow.py"
cp "$BASELINE/paypal-agreement-protocol__paypal__models.py" "$TARGET/paypal/models.py"
cp "$BASELINE/paypal-agreement-protocol__paypal__elevation_flow.py" "$TARGET/paypal/elevation_flow.py"
cp "$BASELINE/paypal-agreement-protocol__paypal__manual_browser.py" "$TARGET/paypal/manual_browser.py"
cp "$BASELINE/paypal-agreement-protocol__web.py" "$TARGET/web.py"
cp "$BASELINE/paypal-agreement-protocol__tests__test_us_onboarding_compat.py" "$TARGET/tests/test_us_onboarding_compat.py"

rm -f "$TARGET/paypal/us_email_first.py"
rm -f "$TARGET/tests/test_us_email_first_flow.py"
rm -f "$TARGET/tests/test_us_email_first_integration.py"

expect_hash() {
    local expected="$1"
    local path="$2"
    local actual
    actual="$(sha256sum "$path" | awk '{print toupper($1)}')"
    if [[ "$actual" != "$expected" ]]; then
        echo "ROLLBACK_HASH_MISMATCH=$path:$actual" >&2
        exit 1
    fi
}

expect_hash "0A27C9373A8C83295EC2C63786FEEE08531945BB0ABB544742DB525308F7AAB1" "$TARGET/paypal/flow.py"
expect_hash "064A9239A28EAB6C2CDBC6CD4C78FEAAE8042E95BBCE8CF2305F7113DC6788F6" "$TARGET/paypal/models.py"
expect_hash "B731679953286B1FE5D6B8AEAF72B62B9BE6B76BE9077509A81A1483BED4AA34" "$TARGET/paypal/elevation_flow.py"
expect_hash "58162F69596D831A67DCD46021003ED1D110D95231188D67A920579B7A67C770" "$TARGET/paypal/manual_browser.py"
expect_hash "64342C792155B40690AA11C117940D931564B726ADFA8081B1220161D45ED5EE" "$TARGET/web.py"
expect_hash "5FE7A2860135729ABB71B98A738A9CE226EE809A9A6BCFF7EF12F9C51B78E41F" "$TARGET/tests/test_us_onboarding_compat.py"

echo "ROLLBACK_RESULT=restored"
echo "ROLLBACK_BRANCH=US"
echo "ROLLBACK_FIELD=onboarding_order"
echo "ROLLBACK_BEHAVIOR=phone_otp_then_combined_signup"
echo "ROLLBACK_FLOW_SHA256=0A27C9373A8C83295EC2C63786FEEE08531945BB0ABB544742DB525308F7AAB1"
