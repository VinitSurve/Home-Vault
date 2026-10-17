#!/usr/bin/env bash
# Creates a standalone executable wrapper for HomeVault at ~/.homevault/bin/homevault
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BIN_DIR="${HOME}/.homevault/bin"
WRAPPER="${BIN_DIR}/homevault"

PYTHON_EXE="$(which python3)"
if [[ -f "${REPO_DIR}/apps/mac-sender/.venv/bin/python3" ]]; then
    PYTHON_EXE="${REPO_DIR}/apps/mac-sender/.venv/bin/python3"
fi

mkdir -p "${BIN_DIR}"

cat <<EOF > "${WRAPPER}"
#!/usr/bin/env bash
export PYTHONPATH="${REPO_DIR}/packages/protocol/src:${REPO_DIR}/apps/mac-sender/src"
exec "${PYTHON_EXE}" -m homevault_sender.cli "\$@"
EOF

chmod +x "${WRAPPER}"

echo "HomeVault CLI wrapper created at ${WRAPPER}"
echo "To add to your PATH, add this to your ~/.zshrc or ~/.bash_profile:"
echo '  export PATH="$HOME/.homevault/bin:$PATH"'
