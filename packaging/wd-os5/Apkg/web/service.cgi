#!/bin/sh
#
# service.cgi — start / stop / restart / status of the hsync daemon.
#
# Query: ?action=start|stop|restart|status
# Returns: {"ok":true,"action":"...","running":true|false}

# shellcheck disable=SC1091
APKG_PATH="/shares/Volume_1/Nas_Prog/hsync"
. "${APKG_PATH}/env"

ACTION=$(printf '%s' "${QUERY_STRING:-}" | sed -n 's/^.*action=\([a-z]*\).*/\1/p')

is_running() {
    PID=""
    [ -f "${HSYNC_PID_FILE}" ] && PID=$(cat "${HSYNC_PID_FILE}" 2>/dev/null)
    [ -z "${PID}" ] && PID=$(pidof "${BINARY}" 2>/dev/null)
    [ -n "${PID}" ] && kill -0 "${PID}" 2>/dev/null
}

reply() {
    printf 'Content-Type: application/json; charset=utf-8\r\n'
    printf 'Cache-Control: no-store\r\n'
    printf '\r\n'
    printf '%s' "$1"
}

case "${ACTION}" in
    start)
        if ! is_running; then
            "${APKG_PATH}/start.sh" "${APKG_PATH}" >/dev/null 2>&1
        fi
        ;;
    stop)
        if is_running; then
            "${APKG_PATH}/stop.sh" "${APKG_PATH}" >/dev/null 2>&1
        fi
        ;;
    restart)
        "${APKG_PATH}/stop.sh"  "${APKG_PATH}" >/dev/null 2>&1
        "${APKG_PATH}/start.sh" "${APKG_PATH}" >/dev/null 2>&1
        ;;
    status)
        ;;
    *)
        printf 'Status: 400 Bad Request\r\n'
        reply '{"ok":false,"error":"invalid_action"}'
        exit 0
        ;;
esac

if is_running; then RUNNING=true; else RUNNING=false; fi
reply "{\"ok\":true,\"action\":\"${ACTION}\",\"running\":${RUNNING}}"
