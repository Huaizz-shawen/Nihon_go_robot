#!/usr/bin/env bash
# Manage the Playwright-based X profile monitor as one background process.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${BRIDGE_PYTHON:-${SCRIPT_DIR}/.venv-codex/bin/python}"
LOG_DIR="${BRIDGE_LOG_DIR:-${SCRIPT_DIR}/logs}"
OUT_LOG="${LOG_DIR}/x-monitor.out.log"
PID_FILE="${LOG_DIR}/x-monitor.pid"
USERNAME="${X_MONITOR_USERNAME:-AyAsA_violin}"
INTERVAL="${X_MONITOR_INTERVAL_MINUTES:-30}"
XVFB_RUN="$(command -v xvfb-run 2>/dev/null || true)"
if [ -z "${XVFB_RUN}" ] && [ -x "${SCRIPT_DIR}/.runtime/xvfb/usr/bin/xvfb-run" ]; then
    export PATH="${SCRIPT_DIR}/.runtime/xvfb/usr/bin:${PATH}"
    XVFB_RUN="${SCRIPT_DIR}/.runtime/xvfb/usr/bin/xvfb-run"
fi

mkdir -p "${LOG_DIR}"

if [ ! -x "${PYTHON_BIN}" ]; then
    echo "ERROR: 找不到 ${PYTHON_BIN}；请先运行 ./setup-codex.sh。" >&2
    exit 1
fi

pid_is_monitor() {
    local pid="${1:-}"
    [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
    kill -0 "${pid}" 2>/dev/null || return 1
    local command_line
    command_line="$(ps -p "${pid}" -o command= 2>/dev/null)" || return 2
    [[ "${command_line}" == *"codex_qq_bridge.x_monitor run"* ]] || return 2
}

start_monitor() {
    local pid
    if [ -z "${XVFB_RUN}" ]; then
        echo "ERROR: 找不到 xvfb-run；请安装 xvfb 或准备项目本地运行包。" >&2
        return 1
    fi
    if [ -f "${PID_FILE}" ]; then
        pid="$(<"${PID_FILE}")"
        if pid_is_monitor "${pid}"; then
            echo "X monitor already running (pid ${pid})"
            return 1
        elif [ "$?" -eq 2 ]; then
            echo "ERROR: PID ${pid} 存活但无法确认身份；请先人工检查。" >&2
            return 1
        fi
    fi
    rm -f "${PID_FILE}"
    (
        cd "${SCRIPT_DIR}"
        nohup setsid env BRIDGE_LOG_DIR="${LOG_DIR}" BRIDGE_QUIET_STDOUT=1 \
            "${XVFB_RUN}" -a -s "-screen 0 1280x900x24" \
            "${PYTHON_BIN}" -m codex_qq_bridge.x_monitor run --headed \
            --username "${USERNAME}" --interval-minutes "${INTERVAL}" \
            >> "${OUT_LOG}" 2>&1 &
        echo $! > "${PID_FILE}"
    )
    sleep 2
    if pid_is_monitor "$(<"${PID_FILE}")"; then
        echo "X monitor started (pid $(<"${PID_FILE}")) -> ${LOG_DIR}/x-monitor.log"
    else
        echo "ERROR: X monitor exited; inspect ${OUT_LOG} and ${LOG_DIR}/x-monitor.log" >&2
        return 1
    fi
}

stop_monitor() {
    if [ ! -f "${PID_FILE}" ]; then
        echo "X monitor is not running"
        return 0
    fi
    local pid
    pid="$(<"${PID_FILE}")"
    if pid_is_monitor "${pid}"; then
        local process_group
        process_group="$(ps -p "${pid}" -o pgid= 2>/dev/null | tr -d ' ')"
        if [ "${process_group}" = "${pid}" ]; then
            kill -- "-${process_group}"
        else
            kill "${pid}"
        fi
        for _ in 1 2 3 4 5; do
            kill -0 "${pid}" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "${pid}" 2>/dev/null; then
            if [ "${process_group}" = "${pid}" ]; then
                kill -9 -- "-${process_group}"
            else
                kill -9 "${pid}"
            fi
        fi
    elif [ "$?" -eq 2 ]; then
        echo "ERROR: PID ${pid} 存活但不是可确认的 X monitor；不会终止它。" >&2
        return 1
    fi
    rm -f "${PID_FILE}"
    echo "X monitor stopped"
}

status_monitor() {
    local pid
    if [ -f "${PID_FILE}" ]; then
        pid="$(<"${PID_FILE}")"
        if pid_is_monitor "${pid}"; then
            echo "X monitor running: pid ${pid}, @${USERNAME}, every ${INTERVAL} minutes"
            return 0
        elif [ "$?" -eq 2 ]; then
            echo "X monitor status unknown: pid ${pid} is alive but identity is unverified"
            return 2
        fi
    fi
    rm -f "${PID_FILE}"
    echo "X monitor not running"
}

run_foreground() {
    local command="$1"
    if [ "${command}" != "test-send" ] && [ -f "${PID_FILE}" ] && pid_is_monitor "$(<"${PID_FILE}")"; then
        echo "ERROR: 请先停止后台监控，避免同时使用浏览器配置目录。" >&2
        exit 1
    fi
    cd "${SCRIPT_DIR}"
    if [ "${command}" = "once" ]; then
        if [ -z "${XVFB_RUN}" ]; then
            echo "ERROR: 找不到 xvfb-run；请安装 xvfb 或准备项目本地运行包。" >&2
            exit 1
        fi
        exec "${XVFB_RUN}" -a -s "-screen 0 1280x900x24" \
            "${PYTHON_BIN}" -m codex_qq_bridge.x_monitor once --headed \
            --username "${USERNAME}" --interval-minutes "${INTERVAL}"
    fi
    if [ "${command}" = "test-send" ]; then
        exec "${PYTHON_BIN}" -m codex_qq_bridge.x_monitor test-send \
            --username "${USERNAME}" --interval-minutes "${INTERVAL}"
    fi
    exec "${PYTHON_BIN}" -m codex_qq_bridge.x_monitor login \
        --username "${USERNAME}" --interval-minutes "${INTERVAL}"
}

case "${1:-status}" in
    login) run_foreground login ;;
    once) run_foreground once ;;
    test-send) run_foreground test-send ;;
    start) start_monitor ;;
    stop) stop_monitor ;;
    restart) stop_monitor; start_monitor ;;
    status) status_monitor ;;
    *) echo "Usage: $0 {login|once|test-send|start|stop|restart|status}" >&2; exit 2 ;;
esac
