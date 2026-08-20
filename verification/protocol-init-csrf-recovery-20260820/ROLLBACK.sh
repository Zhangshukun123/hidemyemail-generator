#!/usr/bin/env bash
set -euo pipefail

artifact_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${1:?usage: ROLLBACK.sh TARGET_FILE}"
original="$artifact_dir/ORIGINAL_FILE"
expected="B32D6F402A8BAA55F1ED898FF6DFDCFC1C4AC46B5AA609E5E0B553035169BAD8"

cp -- "$original" "$target"
actual="$(sha256sum "$target" | awk '{print toupper($1)}')"
if [[ "$actual" != "$expected" ]]; then
  printf 'ROLLBACK_FAILED sha256=%s\n' "$actual" >&2
  exit 1
fi
printf 'ROLLBACK_OK sha256=%s\n' "$actual"
