#!/usr/bin/env bash
# Install Codex QQ Bridge into an isolated local virtual environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv-codex"
PACKAGE_DIR="${SCRIPT_DIR}/packages/codex-qq-bridge"
TUTOR_DIR="${SCRIPT_DIR}/../japanese-tutor"

if ! command -v codex >/dev/null 2>&1; then
    echo "ERROR: 找不到 codex；请先安装并登录 Codex CLI。" >&2
    exit 1
fi

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "${candidate}" >/dev/null 2>&1 && \
       "${candidate}" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
        PYTHON_BIN="$(command -v "${candidate}")"
        break
    fi
done
if [ -z "${PYTHON_BIN}" ]; then
    echo "ERROR: 需要 Python 3.10 或更高版本。" >&2
    exit 1
fi

if command -v uv >/dev/null 2>&1; then
    uv venv --python "${PYTHON_BIN}" "${VENV_DIR}"
    uv pip install --python "${VENV_DIR}/bin/python" -e "${PACKAGE_DIR}"
    if [ -f "${TUTOR_DIR}/pyproject.toml" ]; then
        uv pip install --python "${VENV_DIR}/bin/python" -e "${TUTOR_DIR}"
    fi
else
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
    "${VENV_DIR}/bin/python" -m pip install -e "${PACKAGE_DIR}"
    if [ -f "${TUTOR_DIR}/pyproject.toml" ]; then
        "${VENV_DIR}/bin/python" -m pip install -e "${TUTOR_DIR}"
    fi
fi

echo "Codex QQ Bridge 安装完成。"
if [ ! -f "${SCRIPT_DIR}/.env" ]; then
    echo "下一步运行：${VENV_DIR}/bin/codex-qq-bridge --init"
else
    echo "配置文件已存在；可运行：${SCRIPT_DIR}/start-codex.sh start"
fi
