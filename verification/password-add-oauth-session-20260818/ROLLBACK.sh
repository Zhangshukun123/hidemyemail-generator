#!/usr/bin/env bash
set -euo pipefail

artifact_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="${1:-$(cd "${artifact_dir}/../.." && pwd)}"

cp "${artifact_dir}/chatgpt_register.py.baseline" \
  "${workspace_root}/src/hidemyemail_generator/vendor/gptfree_register/core/chatgpt_register.py"
cp "${artifact_dir}/protocol_registration_worker.py.baseline" \
  "${workspace_root}/src/hidemyemail_generator/protocol_registration_worker.py"

echo "ROLLBACK_OK=${workspace_root}"
