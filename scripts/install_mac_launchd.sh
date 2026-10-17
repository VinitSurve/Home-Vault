#!/usr/bin/env bash
# Install HomeVault macOS Sender Background Worker via launchd
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PLIST_LABEL="com.vinit.homevault.sender"
TARGET_DIR="${HOME}/Library/LaunchAgents"
TARGET_PLIST="${TARGET_DIR}/${PLIST_LABEL}.plist"

echo "=== HomeVault macOS launchd Agent Installer ==="

# 1. Resolve Python executable
PYTHON_EXE="$(which python3)"
if [[ -f "${REPO_DIR}/apps/mac-sender/.venv/bin/python3" ]]; then
    PYTHON_EXE="${REPO_DIR}/apps/mac-sender/.venv/bin/python3"
fi
echo "Using Python executable: ${PYTHON_EXE}"

# 2. Ensure log directory exists
mkdir -p "${HOME}/.homevault/logs"
mkdir -p "${TARGET_DIR}"

# 3. Unload existing agent if running
if launchctl list | grep -q "${PLIST_LABEL}" 2>/dev/null; then
    echo "Stopping existing launchd agent..."
    launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || launchctl unload "${TARGET_PLIST}" 2>/dev/null || true
fi

# 4. Generate configured plist
sed \
    -e "s|__PYTHON_PATH__|${PYTHON_EXE}|g" \
    -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    -e "s|__HOME__|${HOME}|g" \
    "${SCRIPT_DIR}/com.vinit.homevault.sender.plist" > "${TARGET_PLIST}"

chmod 644 "${TARGET_PLIST}"
echo "Installed plist to ${TARGET_PLIST}"

# 5. Bootstrap and load agent
launchctl bootstrap "gui/$(id -u)" "${TARGET_PLIST}" 2>/dev/null || launchctl load "${TARGET_PLIST}"

echo "HomeVault sender worker successfully loaded into launchd!"
echo "Status check: launchctl list | grep homevault"
echo "Log file:     tail -f ~/.homevault/logs/worker.log"
