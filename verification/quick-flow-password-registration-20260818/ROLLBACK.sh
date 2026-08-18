#!/usr/bin/env bash
set -euo pipefail

artifact_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target_root="${1:-$(cd "$artifact_dir/../.." && pwd)}"
patch_file="$artifact_dir/DIFF_FILE.patch"

cd "$target_root"
git apply --reverse --check -- "$patch_file"
git apply --reverse -- "$patch_file"
printf '%s\n' 'ROLLBACK_RESULT=restored quick-flow protocolSetupCredentials to fixed setup_credentials=false'
