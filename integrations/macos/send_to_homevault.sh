#!/usr/bin/env bash
# HomeVault Finder Quick Action Bridge Script
# Accepts files passed from Finder / Apple Shortcuts / Automator,
# prompts for Copy or Move if mode is not specified, and enqueues immediately.

set -euo pipefail

MODE="${1:-}"
if [[ -z "${MODE}" || ("${MODE}" != "copy" && "${MODE}" != "move") ]]; then
    # Prompt user via AppleScript dialog if mode not provided
    CHOICE=$(osascript -e '
        tell application "System Events"
            activate
            set dialogResult to button returned of (display dialog "Select HomeVault transfer action for selected file(s):" buttons {"Cancel", "Move to Windows", "Copy to Windows"} default button "Copy to Windows" with title "HomeVault")
            if dialogResult is "Copy to Windows" then
                return "copy"
            else if dialogResult is "Move to Windows" then
                return "move"
            else
                return "cancel"
            end if
        end tell
    ')

    if [[ "${CHOICE}" == "cancel" || -z "${CHOICE}" ]]; then
        exit 0
    fi
    MODE="${CHOICE}"
else
    # First argument was mode, shift it so remaining arguments are file paths
    shift
fi

if [[ $# -eq 0 ]]; then
    osascript -e 'display alert "HomeVault" message "No files were selected."'
    exit 1
fi

# Locate HomeVault CLI wrapper or python module
HOMEVAULT_CLI="${HOME}/.homevault/bin/homevault"
if [[ ! -x "${HOMEVAULT_CLI}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
    export PYTHONPATH="${REPO_DIR}/packages/protocol/src:${REPO_DIR}/apps/mac-sender/src"
    HOMEVAULT_CLI="python3 -m homevault_sender.cli"
fi

# Submit files to CLI
${HOMEVAULT_CLI} send --mode "${MODE}" "$@"
