#!/usr/bin/env bash
# Claude Code SessionEnd hook: refresh the ledger in the background so session exit is never delayed.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "${repo}/ledger/data"
# The refresh is incremental; the hook payload (session_id, reason) is only recorded for monitoring.
payload="$(head -c 4096)"
nohup mise -C "${repo}" exec -- python "${repo}/ledger/refresh.py" --trigger hook --hook-input "${payload}" \
  >> "${repo}/ledger/data/refresh.log" 2>&1 < /dev/null &
disown
