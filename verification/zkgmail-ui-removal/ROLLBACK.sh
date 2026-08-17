#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target_dir=${1:-"$script_dir/ROLLBACK_COPY"}

mkdir -p "$target_dir"
cp "$script_dir/ORIGINAL_INDEX.html" "$target_dir/index.html"
cp "$script_dir/ORIGINAL_APP.js" "$target_dir/app.js"

expected_index=$(sha256sum "$script_dir/ORIGINAL_INDEX.html" | awk '{print $1}')
expected_app=$(sha256sum "$script_dir/ORIGINAL_APP.js" | awk '{print $1}')
actual_index=$(sha256sum "$target_dir/index.html" | awk '{print $1}')
actual_app=$(sha256sum "$target_dir/app.js" | awk '{print $1}')

if [[ "$actual_index" != "$expected_index" || "$actual_app" != "$expected_app" ]]; then
  printf 'ROLLBACK_RESULT=hash_mismatch\n' >&2
  exit 1
fi

printf 'ROLLBACK_TARGET=%s\n' "$target_dir"
printf 'ROLLBACK_INDEX_SHA256=%s\n' "$actual_index"
printf 'ROLLBACK_APP_SHA256=%s\n' "$actual_app"
printf 'ROLLBACK_RESULT=restored\n'
