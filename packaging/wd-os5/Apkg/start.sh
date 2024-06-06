#!/bin/sh
#
# start.sh — translate hsync.env into CLI flags and launch the daemon.
#
# The script is invoked by the OS5 service manager (and re-invoked by the
# web UI's service.php). It reads the user configuration from
# /mnt/HD/HD_a2/hsync/config/hsync.env, builds the appropriate argument
# vector for the hsync binary, and runs it under nohup with output going to
# /mnt/HD/HD_a2/hsync/log/hsync.log.

set -u

LOG=/tmp/debug_apkg
echo "APKG_DEBUG: $0 $*" >> "$LOG"

APKG_PATH=$(readlink -f "$1")
. "${APKG_PATH}/env"

HSYNC_BIN="${APKG_PATH}/bin/${BINARY}/${BINARY}"

if [ ! -x "${HSYNC_BIN}" ]; then
    echo "hsync executable missing or not executable: ${HSYNC_BIN}" >> "$LOG"
    exit 1
fi

# Load user configuration; fall back to a minimal default profile.
if [ -f "${HSYNC_ENV_FILE}" ]; then
    # shellcheck disable=SC1090
    . "${HSYNC_ENV_FILE}"
fi

: "${HSYNC_MODE:=lan}"
: "${HSYNC_PATH:=${HSYNC_DATA_DIR}}"
: "${HSYNC_BUCKET:=hsync}"
: "${HSYNC_ROLE:=auto}"
: "${HSYNC_LAN_PERSISTENT:=true}"
: "${HSYNC_LAN_HTTPS:=true}"
: "${HSYNC_LAN_GROUP:=}"
: "${HSYNC_DISCOVERY_TIMEOUT:=10}"
: "${HSYNC_SERVER:=}"
: "${HSYNC_API_KEY:=}"
: "${HSYNC_WATCH:=true}"
: "${HSYNC_INTERVAL:=0}"
: "${HSYNC_IGNORE_DELETE:=false}"
: "${HSYNC_NO_COMPRESSION:=false}"
: "${HSYNC_MAX_BW_UPLOAD:=}"
: "${HSYNC_MAX_BW_DOWNLOAD:=}"
: "${HSYNC_MIN_FREE_DISK_MB:=}"
: "${HSYNC_VERBOSE:=false}"

mkdir -p "${HSYNC_LOG_DIR}" "${HSYNC_RUN_DIR}" "${HSYNC_PATH}"

# ---------------------------------------------------------------------------
# Build the argument vector.
# ---------------------------------------------------------------------------
set -- \
    --bucket "${HSYNC_BUCKET}" \
    --path "${HSYNC_PATH}" \
    --yes

case "${HSYNC_MODE}" in
    lan)
        set -- "$@" --lan-mode
        [ "${HSYNC_LAN_PERSISTENT}" = "false" ] && set -- "$@" --no-lan-persistent
        [ "${HSYNC_LAN_HTTPS}" = "false" ]      && set -- "$@" --no-lan-https
        [ -n "${HSYNC_LAN_GROUP}" ]             && set -- "$@" --lan-group "${HSYNC_LAN_GROUP}"
        set -- "$@" --discovery-timeout "${HSYNC_DISCOVERY_TIMEOUT}"
        case "${HSYNC_ROLE}" in
            master) set -- "$@" --force-master ;;
            slave)  set -- "$@" --sync-direction download-only ;;
            *)      : ;;   # auto — no extra flag
        esac
        ;;
    server)
        if [ -z "${HSYNC_SERVER}" ]; then
            echo "HSYNC_MODE=server requires HSYNC_SERVER" >> "$LOG"
            exit 1
        fi
        set -- "$@" --server "${HSYNC_SERVER}"
        ;;
    *)
        echo "Unknown HSYNC_MODE=${HSYNC_MODE}" >> "$LOG"
        exit 1
        ;;
esac

[ "${HSYNC_WATCH}" = "true" ]          && set -- "$@" --watch
[ "${HSYNC_INTERVAL}" -gt 0 ] 2>/dev/null && set -- "$@" --interval "${HSYNC_INTERVAL}"
[ "${HSYNC_IGNORE_DELETE}" = "true" ]  && set -- "$@" --ignore-delete
[ "${HSYNC_NO_COMPRESSION}" = "true" ] && set -- "$@" --no-compression
[ -n "${HSYNC_MAX_BW_UPLOAD}" ]        && set -- "$@" --max-bandwidth-upload "${HSYNC_MAX_BW_UPLOAD}"
[ -n "${HSYNC_MAX_BW_DOWNLOAD}" ]      && set -- "$@" --max-bandwidth-download "${HSYNC_MAX_BW_DOWNLOAD}"
[ -n "${HSYNC_MIN_FREE_DISK_MB}" ]     && set -- "$@" --min-free-disk-space "${HSYNC_MIN_FREE_DISK_MB}"
[ "${HSYNC_VERBOSE}" = "true" ]        && set -- "$@" --verbose

# ---------------------------------------------------------------------------
# Launch under nohup. The API key is exported so the binary picks it up via
# HSYNC_API_KEY without leaking it through ``ps``.
# ---------------------------------------------------------------------------
export HSYNC_API_KEY

cd "${APKG_PATH}"
nohup "${HSYNC_BIN}" "$@" >> "${HSYNC_LOG_FILE}" 2>&1 &
echo $! > "${HSYNC_PID_FILE}"

exit 0
