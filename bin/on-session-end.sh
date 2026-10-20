#!/usr/bin/env bash
# Claude Code SessionEnd hook: refresh the ledger in the background so session exit is never delayed.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "${repo}/ledger/data"
cat > /dev/null  # hook input (session_id, transcript_path...) is not needed: refresh is incremental
nohup mise -C "${repo}" run refresh >> "${repo}/ledger/data/refresh.log" 2>&1 < /dev/null &
disown
