#!/usr/bin/env bash
set -euo pipefail

artifact_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${1:?usage: ROLLBACK.sh TARGET_FILE}"
original="$artifact_dir/ORIGINAL_FILE"
expected="A5A9B022B913A3EAA671B57336A80D74CFCEBDBCF76C6210381D12DCEFCE8BC9"

cp -- "$original" "$target"
actual="$(sha256sum "$target" | awk '{print toupper($1)}')"
if [[ "$actual" != "$expected" ]]; then
  printf 'ROLLBACK_FAILED sha256=%s\n' "$actual" >&2
  exit 1
fi
printf 'ROLLBACK_OK sha256=%s\n' "$actual"
