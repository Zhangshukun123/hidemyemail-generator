#!/usr/bin/env bash
set -euo pipefail

artifact_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${1:?usage: ROLLBACK.sh TARGET_FILE}"
original="$artifact_dir/ORIGINAL_FILE"
expected="39E9825124C601B34DD67927FB777D1E1298F2D0C43B109D60F5378641DDBD9D"

cp -- "$original" "$target"
actual="$(sha256sum "$target" | awk '{print toupper($1)}')"
if [[ "$actual" != "$expected" ]]; then
  printf 'ROLLBACK_FAILED sha256=%s\n' "$actual" >&2
  exit 1
fi
printf 'ROLLBACK_OK sha256=%s\n' "$actual"
