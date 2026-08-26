#!/usr/bin/env bash
# Start/stop the Codex QQ Bridge as one background process.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${BRIDGE_PYTHON:-${SCRIPT_DIR}/.venv-codex/bin/python}"
LOG_DIR="${BRIDGE_LOG_DIR:-${SCRIPT_DIR}/logs}"
LOG_FILE="${LOG_DIR}/codex-bridge.out.log"
PID_FILE="${LOG_DIR}/codex-bridge.pid"

mkdir -p "${LOG_DIR}"

if [ ! -x "${PYTHON_BIN}" ]; then
    echo "ERROR: 找不到 ${PYTHON_BIN}；请先运行 ./setup-codex.sh。" >&2
    exit 1
fi

pid_is_bridge() {
    local pid="${1:-}"
    [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
    kill -0 "${pid}" 2>/dev/null || return 1
    local command_line
    command_line="$(ps -p "${pid}" -o command= 2>/dev/null)" || return 2
    [[ "${command_line}" == *"codex_qq_bridge"* ]] || return 2
    return 0
}

start_bridge() {
    local pid
    if [ -f "${PID_FILE}" ]; then
        pid="$(<"${PID_FILE}")"
        if pid_is_bridge "${pid}"; then
            echo "Codex bridge already running (pid ${pid})"
            return 1
        elif [ "$?" -eq 2 ]; then
            echo "ERROR: PID ${pid} 存活但无法确认身份；为避免重复启动，请先人工检查。" >&2
            return 1
        fi
    fi
    rm -f "${PID_FILE}"
    (
        cd "${SCRIPT_DIR}"
        nohup env BRIDGE_LOG_DIR="${LOG_DIR}" BRIDGE_QUIET_STDOUT=1 \
            "${PYTHON_BIN}" -m codex_qq_bridge \
            >> "${LOG_FILE}" 2>&1 &
        echo $! > "${PID_FILE}"
    )
    sleep 1
    if kill -0 "$(<"${PID_FILE}")" 2>/dev/null; then
        echo "Codex bridge started (pid $(<"${PID_FILE}")) -> ${LOG_FILE}"
    else
        echo "ERROR: Codex bridge exited; inspect ${LOG_FILE}" >&2
        return 1
    fi
}

stop_bridge() {
    if [ ! -f "${PID_FILE}" ]; then
        echo "Codex bridge is not running"
        return 0
    fi
    pid="$(<"${PID_FILE}")"
    if pid_is_bridge "${pid}"; then
        kill "${pid}"
        for _ in 1 2 3 4 5; do
            kill -0 "${pid}" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "${pid}" 2>/dev/null; then
            kill -9 "${pid}"
        fi
    elif [ "$?" -eq 2 ]; then
        echo "ERROR: PID ${pid} 存活但不是可确认的 Codex bridge；不会终止它。" >&2
        return 1
    fi
    rm -f "${PID_FILE}"
    echo "Codex bridge stopped"
}

status_bridge() {
    local pid
    if [ -f "${PID_FILE}" ]; then
        pid="$(<"${PID_FILE}")"
        if pid_is_bridge "${pid}"; then
            echo "Codex bridge running: pid ${pid}"
            return 0
        elif [ "$?" -eq 2 ]; then
            echo "Codex bridge status unknown: pid ${pid} is alive but identity is unverified"
            return 2
        fi
    fi
    rm -f "${PID_FILE}"
    echo "Codex bridge not running"
}

case "${1:-start}" in
    start) start_bridge ;;
    stop) stop_bridge ;;
    restart) stop_bridge; start_bridge ;;
    status) status_bridge ;;
    *) echo "Usage: $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
