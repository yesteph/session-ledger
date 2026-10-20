#!/usr/bin/env bash
# Manage the hourly launchd agent that refreshes the ledger as a fallback to the SessionEnd hook.
# Usage: bin/schedule.sh install|uninstall|status|run
set -euo pipefail

readonly LABEL="local.session-ledger.refresh"
readonly PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
readonly INTERVAL_SECONDS=3600
readonly MIN_IDLE_MINUTES=30
readonly DOMAIN="gui/$(id -u)"

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

write_plist() {
  local mise_bin
  mise_bin="$(command -v mise)" || { echo "mise not found on PATH" >&2; exit 1; }
  mkdir -p "$(dirname "${PLIST}")" "${repo}/ledger/data"
  cat > "${PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${mise_bin}</string><string>-C</string><string>${repo}</string>
    <string>exec</string><string>--</string>
    <string>python</string><string>${repo}/ledger/refresh.py</string>
    <string>--min-idle</string><string>${MIN_IDLE_MINUTES}</string>
    <string>--trigger</string><string>schedule</string>
  </array>
  <key>StartInterval</key><integer>${INTERVAL_SECONDS}</integer>
  <key>RunAtLoad</key><false/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>${repo}/ledger/data/refresh.log</string>
  <key>StandardErrorPath</key><string>${repo}/ledger/data/refresh.log</string>
</dict>
</plist>
PLIST
  plutil -lint "${PLIST}" > /dev/null
}

case "${1:-}" in
  install)
    launchctl bootout "${DOMAIN}/${LABEL}" 2> /dev/null || true
    write_plist
    launchctl bootstrap "${DOMAIN}" "${PLIST}"
    echo "installed ${LABEL}: every $((INTERVAL_SECONDS / 60)) min, skipping sessions active in the last ${MIN_IDLE_MINUTES} min"
    ;;
  uninstall)
    launchctl bootout "${DOMAIN}/${LABEL}" 2> /dev/null || true
    rm -f "${PLIST}"
    echo "uninstalled ${LABEL}"
    ;;
  status)
    launchctl print "${DOMAIN}/${LABEL}" 2> /dev/null | grep -E '^\s*(state|runs|last exit code|run interval)' \
      || echo "${LABEL} is not installed"
    ;;
  run)
    launchctl kickstart "${DOMAIN}/${LABEL}"
    echo "started ${LABEL}; follow ${repo}/ledger/data/refresh.log"
    ;;
  *)
    echo "usage: $0 install|uninstall|status|run" >&2
    exit 2
    ;;
esac
