#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target=${1:-"$script_dir/ROLLBACK_COPY"}

cp "$script_dir/ORIGINAL_FILE" "$target"
expected=$(sha256sum "$script_dir/ORIGINAL_FILE" | awk '{print $1}')
actual=$(sha256sum "$target" | awk '{print $1}')

if [[ "$actual" != "$expected" ]]; then
  printf 'ROLLBACK_RESULT=hash_mismatch\n' >&2
  exit 1
fi

printf 'ROLLBACK_TARGET=%s\n' "$target"
printf 'ROLLBACK_SHA256=%s\n' "$actual"
printf 'ROLLBACK_RESULT=restored\n'
