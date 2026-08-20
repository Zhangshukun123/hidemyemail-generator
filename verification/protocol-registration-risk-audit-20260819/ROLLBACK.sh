#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE="$ROOT/protocol-registration-server/protocol_registration_server/presenter.py"
TARGET="${1:-$ROOT/verification/protocol-registration-risk-audit-20260819/presenter.reviewed.py}"
EXPECTED="1e93c395dc3789bd1af92baa036106eeef0e08f1e8c7c02467bb2fffb5d844ab"
ACTUAL="$(sha256sum "$SOURCE" | awk '{print $1}')"
if [[ "$ACTUAL" != "$EXPECTED" ]]; then
  printf 'ROLLBACK source hash mismatch: expected=%s actual=%s\n' "$EXPECTED" "$ACTUAL" >&2
  exit 2
fi
cp "$SOURCE" "$TARGET"
RESTORED="$(sha256sum "$TARGET" | awk '{print $1}')"
if [[ "$RESTORED" != "$EXPECTED" ]]; then
  printf 'ROLLBACK restore verification failed: expected=%s actual=%s\n' "$EXPECTED" "$RESTORED" >&2
  exit 3
fi
printf 'ROLLBACK restored presenter.py sha256=%s\n' "$RESTORED"
