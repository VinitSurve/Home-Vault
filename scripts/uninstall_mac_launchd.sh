#!/usr/bin/env bash
# Uninstall HomeVault macOS Sender Background Worker from launchd
set -euo pipefail

PLIST_LABEL="com.vinit.homevault.sender"
TARGET_PLIST="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"

echo "=== HomeVault macOS launchd Agent Uninstaller ==="

if [[ -f "${TARGET_PLIST}" ]]; then
    echo "Stopping and unloading launchd agent..."
    launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || launchctl unload "${TARGET_PLIST}" 2>/dev/null || true
    rm -f "${TARGET_PLIST}"
    echo "Removed ${TARGET_PLIST}."
else
    echo "No agent plist found at ${TARGET_PLIST}."
fi

echo "HomeVault sender worker successfully uninstalled."
