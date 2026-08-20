#!/usr/bin/env bash
set -euo pipefail

artifact_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target_root="${1:-$(cd "$artifact_dir/../.." && pwd)}"

git -C "$target_root" apply -R "$artifact_dir/DIFF_FILE/tracked.patch"
rm -f "$target_root/tests/test_paypal_checkout_sentinel.py"

echo "ROLLBACK_OK: PayPal Checkout SEN+SO and create-time promotion changes removed"
