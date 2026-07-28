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

# ---------------------------------------------------------------------------
# Security: a file is only safe to `source` as root when it is owned by root
# (uid 0) and carries no write bit for group or other. This prevents a local
# NAS user from injecting shell commands into hsync.env that would otherwise
# run with root privileges at service start. Fails closed.
# ---------------------------------------------------------------------------
env_file_is_root_safe() {
    _f=$1
    _owner=$(stat -c '%u' "${_f}" 2>/dev/null) || _owner=""
    [ "${_owner}" = "0" ] || return 1
    _mode=$(stat -c '%a' "${_f}" 2>/dev/null) || _mode=""
    [ -n "${_mode}" ] || return 1
    while [ "${#_mode}" -gt 3 ]; do _mode=${_mode#?}; done
    while [ "${#_mode}" -lt 3 ]; do _mode="0${_mode}"; done
    _grp=$(printf '%s' "${_mode}" | cut -c2)
    _oth=$(printf '%s' "${_mode}" | cut -c3)
    case "${_grp}${_oth}" in
        *[2367]*) return 1 ;;
    esac
    return 0
}

HSYNC_BIN="${APKG_PATH}/bin/${BINARY}/${BINARY}"

if [ ! -x "${HSYNC_BIN}" ]; then
    echo "hsync executable missing or not executable: ${HSYNC_BIN}" >> "$LOG"
    exit 1
fi

# Load user configuration; fall back to a minimal default profile.
#
# hsync.env is sourced into this root shell, so sourcing an attacker-writable
# file would execute arbitrary commands as root. Only source it when it is
# root-owned and not writable by group or other; otherwise refuse to start.
if [ -f "${HSYNC_ENV_FILE}" ]; then
    if ! env_file_is_root_safe "${HSYNC_ENV_FILE}"; then
        echo "REFUSING to source unsafe ${HSYNC_ENV_FILE}: must be root-owned and not group/other-writable" >> "$LOG"
        exit 1
    fi
    # shellcheck disable=SC1090
    . "${HSYNC_ENV_FILE}"
fi

# Backup-oriented defaults: the NAS is the authoritative master that keeps a
# safe copy of the clients' data. Deletions on the clients are NOT propagated
# (HSYNC_IGNORE_DELETE=true), so the backup never loses files because a source
# host removed them.
: "${HSYNC_MODE:=lan}"
: "${HSYNC_PATH:=${HSYNC_DATA_DIR}}"
: "${HSYNC_BUCKET:=hsync}"
: "${HSYNC_ROLE:=master}"
: "${HSYNC_LAN_PERSISTENT:=true}"
: "${HSYNC_LAN_GROUP:=}"
: "${HSYNC_LAN_KEY:=}"
: "${HSYNC_LAN_REQUIRE_PAIRING:=false}"
: "${HSYNC_DISCOVERY_TIMEOUT:=10}"
: "${HSYNC_SERVER:=}"
: "${HSYNC_API_KEY:=}"
: "${HSYNC_WATCH:=true}"
: "${HSYNC_INTERVAL:=0}"
: "${HSYNC_IGNORE_DELETE:=true}"
: "${HSYNC_NO_COMPRESSION:=false}"
: "${HSYNC_DELTA_ENABLED:=true}"
: "${HSYNC_DELTA_MIN_FILE_SIZE:=}"
: "${HSYNC_DELTA_MIN_FILE_SIZE_LAN:=}"
: "${HSYNC_MAX_BW_UPLOAD:=}"
: "${HSYNC_MAX_BW_DOWNLOAD:=}"
: "${HSYNC_MIN_FREE_DISK_MB:=}"
: "${HSYNC_MAX_DOWNLOAD_SIZE_MB:=}"
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
        # LAN transport is HTTPS/WSS-only (protocol lan/1): the master always
        # serves over TLS with a persistent self-signed identity and advertises
        # its certificate fingerprint, which slaves pin (TOFU). There is no
        # plain-HTTP option.
        set -- "$@" --lan-mode
        [ "${HSYNC_LAN_PERSISTENT}" = "false" ]       && set -- "$@" --no-lan-persistent
        [ "${HSYNC_LAN_REQUIRE_PAIRING}" = "true" ]   && set -- "$@" --require-pairing
        [ -n "${HSYNC_LAN_GROUP}" ]                   && set -- "$@" --lan-group "${HSYNC_LAN_GROUP}"
        set -- "$@" --discovery-timeout "${HSYNC_DISCOVERY_TIMEOUT}"
        case "${HSYNC_ROLE}" in
            master)       set -- "$@" --force-master ;;
            slave)        set -- "$@" --sync-direction download-only ;;
            never-master) set -- "$@" --never-master ;;
            *)            : ;;   # auto — no extra flag
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
[ "${HSYNC_DELTA_ENABLED}" = "false" ] && set -- "$@" --no-delta
[ -n "${HSYNC_DELTA_MIN_FILE_SIZE}" ]     && set -- "$@" --delta-min-file-size "${HSYNC_DELTA_MIN_FILE_SIZE}"
[ -n "${HSYNC_DELTA_MIN_FILE_SIZE_LAN}" ] && set -- "$@" --delta-min-file-size-lan "${HSYNC_DELTA_MIN_FILE_SIZE_LAN}"
[ -n "${HSYNC_MAX_BW_UPLOAD}" ]        && set -- "$@" --max-bandwidth-upload "${HSYNC_MAX_BW_UPLOAD}"
[ -n "${HSYNC_MAX_BW_DOWNLOAD}" ]      && set -- "$@" --max-bandwidth-download "${HSYNC_MAX_BW_DOWNLOAD}"
[ -n "${HSYNC_MIN_FREE_DISK_MB}" ]     && set -- "$@" --min-free-disk-space "${HSYNC_MIN_FREE_DISK_MB}"
[ -n "${HSYNC_MAX_DOWNLOAD_SIZE_MB}" ] && set -- "$@" --max-download-size "${HSYNC_MAX_DOWNLOAD_SIZE_MB}"
[ "${HSYNC_VERBOSE}" = "true" ]        && set -- "$@" --verbose

# ---------------------------------------------------------------------------
# Launch under nohup. Secrets are exported (not passed as flags) so the binary
# picks them up via HSYNC_API_KEY / HSYNC_LAN_KEY without leaking them through
# ``ps``.
# ---------------------------------------------------------------------------
export HSYNC_API_KEY
export HSYNC_LAN_KEY

cd "${APKG_PATH}"
nohup "${HSYNC_BIN}" "$@" >> "${HSYNC_LOG_FILE}" 2>&1 &
echo $! > "${HSYNC_PID_FILE}"

exit 0
