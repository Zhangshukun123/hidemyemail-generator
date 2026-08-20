#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ $# -gt 0 ]]; then
  target_root="$(cd "$1" && pwd -P)"
else
  target_root="$(cd "$script_dir/../.." && pwd -P)"
fi

files=(
  "src/hidemyemail_generator/web_ui/static/app.js"
  "src/hidemyemail_generator/webapp.py"
  "src/hidemyemail_generator/registration_tasks.py"
  "tests/test_quick_flow_config_ui.py"
  "tests/test_web_ui.py"
  "tests/test_registration_tasks.py"
)

for file in "${files[@]}"; do
  mkdir -p "$target_root/$(dirname "$file")"
  cp "$script_dir/original/$file" "$target_root/$file"
  printf 'RESTORED %s\n' "$file"
done

printf 'ROLLBACK_OK target=%s files=%s\n' "$target_root" "${#files[@]}"
