#!/bin/sh
#
# stop.sh — terminate the running hsync daemon.

set -u

LOG=/tmp/debug_apkg
echo "APKG_DEBUG: $0 $*" >> "$LOG"

APKG_PATH=$(readlink -f "$1")
. "${APKG_PATH}/env"

# Prefer the recorded pid; fall back to ``pidof`` for safety.
PID=""
if [ -f "${HSYNC_PID_FILE}" ]; then
    PID=$(cat "${HSYNC_PID_FILE}" 2>/dev/null || true)
fi
[ -z "${PID}" ] && PID=$(pidof "${BINARY}" 2>/dev/null || true)

if [ -n "${PID}" ]; then
    kill ${PID} 2>/dev/null || true
    # Wait up to 15 s for a clean shutdown, then escalate.
    i=0
    while [ ${i} -lt 15 ] && kill -0 ${PID} 2>/dev/null; do
        sleep 1
        i=$((i + 1))
    done
    kill -0 ${PID} 2>/dev/null && kill -9 ${PID} 2>/dev/null || true
fi

rm -f "${HSYNC_PID_FILE}" 2>/dev/null || true
exit 0
