#!/usr/bin/env bash
# Creates a standalone executable wrapper for HomeVault at ~/.homevault/bin/homevault
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BIN_DIR="${HOME}/.homevault/bin"
WRAPPER="${BIN_DIR}/homevault"

# Prefer the verified ARM virtual environment (.venv-arm) containing cryptography
PYTHON_EXE=""
if [[ -x "${REPO_DIR}/apps/mac-sender/.venv-arm/bin/python" ]]; then
    PYTHON_EXE="${REPO_DIR}/apps/mac-sender/.venv-arm/bin/python"
elif [[ -x "${REPO_DIR}/apps/mac-sender/.venv-arm/bin/python3" ]]; then
    PYTHON_EXE="${REPO_DIR}/apps/mac-sender/.venv-arm/bin/python3"
elif [[ -x "${REPO_DIR}/apps/mac-sender/.venv/bin/python" ]]; then
    PYTHON_EXE="${REPO_DIR}/apps/mac-sender/.venv/bin/python"
elif [[ -x "${REPO_DIR}/apps/mac-sender/.venv/bin/python3" ]]; then
    PYTHON_EXE="${REPO_DIR}/apps/mac-sender/.venv/bin/python3"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    PYTHON_EXE="${VIRTUAL_ENV}/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_EXE="$(command -v python3)"
fi

if [[ -z "${PYTHON_EXE}" || ! -x "${PYTHON_EXE}" ]]; then
    echo "Error: Python executable not found or not executable." >&2
    echo "Expected virtual environment at '${REPO_DIR}/apps/mac-sender/.venv-arm/bin/python'" >&2
    exit 1
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
