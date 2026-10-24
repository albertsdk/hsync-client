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

# These values are the defaults the web UI ships and match the desktop
# application, with one deliberate exception: the NAS is a backup target, so
# deletions from the clients are never propagated (HSYNC_IGNORE_DELETE=true)
# and the backup cannot lose files because a source host removed them.
: "${HSYNC_MODE:=p2p}"
: "${HSYNC_PATH:=${HSYNC_DATA_DIR}}"
: "${HSYNC_BUCKET:=my-bucket}"
: "${HSYNC_SYNC_DIRECTION:=bidirectional}"
: "${HSYNC_FILTER_PATH:=}"
: "${HSYNC_IGNORE_PATTERNS:=}"
: "${HSYNC_P2P_GROUP:=my-group}"
: "${HSYNC_P2P_KEY:=}"
: "${HSYNC_P2P_PORT:=0}"
: "${HSYNC_PREFER_MASTER:=false}"
: "${HSYNC_NEVER_MASTER:=false}"
: "${HSYNC_P2P_REQUIRE_PAIRING:=false}"
: "${HSYNC_P2P_PEER_DOWNLOAD:=true}"
: "${HSYNC_P2P_PEER_READ_CAP:=2}"
: "${HSYNC_P2P_PEER_SOURCES_MAX:=3}"
: "${HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES:=false}"
: "${HSYNC_P2P_ALLOW_UNAUTHENTICATED:=false}"
: "${HSYNC_ALLOW_DELETIONS:=false}"
: "${HSYNC_DISCOVERY_TIMEOUT:=10}"
: "${HSYNC_BOOTNODE:=}"
: "${HSYNC_BOOTNODE_FINGERPRINT:=}"
: "${HSYNC_BOOTNODE_TOFU:=false}"
: "${HSYNC_BOOTNODE_TRUST:=ask}"
: "${HSYNC_BOOTNODE_PORTS:=}"
: "${HSYNC_UPNP_ENABLED:=true}"
: "${HSYNC_SERVER:=}"
: "${HSYNC_API_KEY:=}"
: "${HSYNC_INSECURE:=false}"
: "${HSYNC_CA_CERT:=}"
: "${HSYNC_SERVER_TRUST:=chain}"
: "${HSYNC_SERVER_FINGERPRINT:=}"
: "${HSYNC_SERVER_TOFU:=false}"
: "${HSYNC_INTERVAL:=0}"
: "${HSYNC_IGNORE_DELETE:=true}"
: "${HSYNC_COMPRESSION_ENABLED:=true}"
: "${HSYNC_DELTA_ENABLED:=true}"
: "${HSYNC_DELTA_MIN_FILE_SIZE:=256}"
: "${HSYNC_DELTA_MIN_FILE_SIZE_P2P:=1024}"
: "${HSYNC_MAX_BW_UPLOAD:=}"
: "${HSYNC_MAX_BW_DOWNLOAD:=}"
: "${HSYNC_MIN_FREE_DISK:=500MB}"
: "${HSYNC_MAX_INBOUND_SIZE_MB:=10240}"
: "${HSYNC_VERBOSE:=false}"
: "${HSYNC_QUIET:=false}"
: "${HSYNC_DRY_RUN:=false}"
: "${HSYNC_AUTOTUNE:=true}"
: "${HSYNC_MAX_CONCURRENT_TRANSFERS:=0}"
: "${HSYNC_MASS_DELETE_GUARD:=true}"
: "${HSYNC_MASS_DELETE_THRESHOLD:=50}"
: "${HSYNC_MASS_DELETE_PERCENT:=50}"
: "${HSYNC_HTTP_TIMEOUT:=30}"
: "${HSYNC_RECONNECT_DELAY:=5}"
: "${HSYNC_STABILITY_DELAY:=5}"
: "${HSYNC_MAX_WAIT_TIMEOUT:=120}"
: "${HSYNC_TRANSFER_RETRY_MIN_WAIT:=}"
: "${HSYNC_TRANSFER_RETRY_MAX_WAIT:=}"
: "${HSYNC_NTP_HOSTS:=}"

# Numeric guard: a hand-edited hsync.env must never make the launcher pass a
# non-numeric token to a numeric CLI flag.
is_uint() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }
is_uint_gt0() { is_uint "$1" && [ "$1" -gt 0 ]; }

# Enum guard for the hand-editable policy/direction fields.
case "${HSYNC_SYNC_DIRECTION}" in
    bidirectional|upload-only|download-only) ;;
    *) HSYNC_SYNC_DIRECTION=bidirectional ;;
esac
case "${HSYNC_SERVER_TRUST}" in
    chain|ask|accept-new|strict|none) ;;
    *) HSYNC_SERVER_TRUST=chain ;;
esac
case "${HSYNC_BOOTNODE_TRUST}" in
    ask|accept-new|strict|none) ;;
    *) HSYNC_BOOTNODE_TRUST=ask ;;
esac

mkdir -p "${HSYNC_LOG_DIR}" "${HSYNC_RUN_DIR}" "${HSYNC_PATH}"

# ---------------------------------------------------------------------------
# Build the argument vector. The subcommand is always the first token: ``p2p``
# for peer-to-peer and ``server`` for the central server.
# ---------------------------------------------------------------------------
case "${HSYNC_MODE}" in
    p2p)    SUBCOMMAND="p2p" ;;
    server) SUBCOMMAND="server" ;;
    *)
        echo "Unknown HSYNC_MODE=${HSYNC_MODE}" >> "$LOG"
        exit 1
        ;;
esac

set -- "${SUBCOMMAND}" \
    --bucket "${HSYNC_BUCKET}" \
    --path "${HSYNC_PATH}"

[ "${HSYNC_ALLOW_DELETIONS}" = "true" ] && set -- "$@" --allow-deletions

case "${HSYNC_MODE}" in
    p2p)
        # p2p transport is HTTPS/WSS-only (protocol p2p/1): the master always
        # serves over TLS with a persistent self-signed identity and advertises
        # its certificate fingerprint, which slaves pin (TOFU). There is no
        # plain-HTTP option.
        [ "${HSYNC_P2P_REQUIRE_PAIRING}" = "true" ]   && set -- "$@" --require-pairing
        [ "${HSYNC_P2P_PEER_DOWNLOAD}" = "false" ]    && set -- "$@" --no-peer-download
        [ "${HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES}" = "true" ] && set -- "$@" --allow-unauthenticated-writes
        [ "${HSYNC_P2P_ALLOW_UNAUTHENTICATED}" = "true" ] && set -- "$@" --allow-unauthenticated-p2p
        [ -n "${HSYNC_P2P_GROUP}" ]                   && set -- "$@" --group "${HSYNC_P2P_GROUP}"
        is_uint_gt0 "${HSYNC_P2P_PORT}"               && set -- "$@" --listen-port "${HSYNC_P2P_PORT}"
        # A non-numeric or empty timeout must never reach the CLI: it would
        # abort the startup, so the client's own default applies instead.
        is_uint_gt0 "${HSYNC_DISCOVERY_TIMEOUT}"      && set -- "$@" --discovery-timeout "${HSYNC_DISCOVERY_TIMEOUT}"
        # A p2p master is always bidirectional, so "prefer master" cannot be
        # combined with a one-way direction (the client rejects that pair).
        if [ "${HSYNC_PREFER_MASTER}" = "true" ] && [ "${HSYNC_SYNC_DIRECTION}" = "bidirectional" ]; then
            set -- "$@" --prefer-master
        fi
        [ "${HSYNC_NEVER_MASTER}" = "true" ]          && set -- "$@" --never-master
        ;;
    server)
        if [ -z "${HSYNC_SERVER}" ]; then
            echo "HSYNC_MODE=server requires HSYNC_SERVER" >> "$LOG"
            exit 1
        fi
        # The full server URL is exported below: the bare-hostname form is
        # only for interactive command lines, the NAS wrapper uses the env
        # fallback.
        ;;
esac

# Periodic full check: 0 disables it. The client has no CLI "off" for
# --interval (absence means its own default), so the off state travels through
# the environment and overrides an ambient HSYNC_RESYNC.
if is_uint_gt0 "${HSYNC_INTERVAL}"; then
    set -- "$@" --interval "${HSYNC_INTERVAL}"
else
    export HSYNC_RESYNC=0
fi
[ "${HSYNC_IGNORE_DELETE}" = "true" ]  && set -- "$@" --ignore-delete
[ "${HSYNC_COMPRESSION_ENABLED}" = "false" ] && set -- "$@" --no-compression
[ "${HSYNC_DELTA_ENABLED}" = "false" ] && set -- "$@" --no-delta
[ "${HSYNC_DRY_RUN}" = "true" ]        && set -- "$@" --dry-run
set -- "$@" --sync-direction "${HSYNC_SYNC_DIRECTION}"
# Relative subfolder only: an absolute path would escape the bucket root.
case "${HSYNC_FILTER_PATH}" in
    ''|/*) ;;
    *) set -- "$@" --subdir "${HSYNC_FILTER_PATH}" ;;
esac
if [ -n "${HSYNC_IGNORE_PATTERNS}" ]; then
    _old_ifs=$IFS
    IFS=','
    set -f
    for _pattern in ${HSYNC_IGNORE_PATTERNS}; do
        [ -n "${_pattern}" ] && set -- "$@" --ignore-pattern "${_pattern}"
    done
    set +f
    IFS=$_old_ifs
fi
# Delta floors are edited in KB (the desktop unit) and passed in bytes.
is_uint "${HSYNC_DELTA_MIN_FILE_SIZE}" \
    && set -- "$@" --delta-min-file-size "$(( HSYNC_DELTA_MIN_FILE_SIZE * 1024 ))"
is_uint "${HSYNC_DELTA_MIN_FILE_SIZE_P2P}" \
    && set -- "$@" --delta-min-file-size-p2p "$(( HSYNC_DELTA_MIN_FILE_SIZE_P2P * 1024 ))"
[ -n "${HSYNC_MAX_BW_UPLOAD}" ]        && set -- "$@" --limit-upload "${HSYNC_MAX_BW_UPLOAD}"
[ -n "${HSYNC_MAX_BW_DOWNLOAD}" ]      && set -- "$@" --limit-download "${HSYNC_MAX_BW_DOWNLOAD}"
[ -n "${HSYNC_MIN_FREE_DISK}" ]        && set -- "$@" --min-free-disk-space "${HSYNC_MIN_FREE_DISK}"
is_uint "${HSYNC_MAX_INBOUND_SIZE_MB}" && set -- "$@" --max-inbound-size "${HSYNC_MAX_INBOUND_SIZE_MB}"
is_uint_gt0 "${HSYNC_MAX_CONCURRENT_TRANSFERS}" \
    && set -- "$@" --max-concurrent-transfers "${HSYNC_MAX_CONCURRENT_TRANSFERS}"
[ "${HSYNC_MASS_DELETE_GUARD}" = "false" ] && set -- "$@" --no-mass-delete-guard
is_uint "${HSYNC_MASS_DELETE_THRESHOLD}" \
    && set -- "$@" --mass-delete-threshold "${HSYNC_MASS_DELETE_THRESHOLD}"
is_uint "${HSYNC_MASS_DELETE_PERCENT}" \
    && set -- "$@" --mass-delete-percent "${HSYNC_MASS_DELETE_PERCENT}"
[ "${HSYNC_VERBOSE}" = "true" ]        && set -- "$@" --verbose
[ "${HSYNC_QUIET}" = "true" ]          && set -- "$@" --quiet
[ "${HSYNC_AUTOTUNE}" = "false" ]      && set -- "$@" --no-autotune
is_uint "${HSYNC_HTTP_TIMEOUT}"        && set -- "$@" --http-timeout "${HSYNC_HTTP_TIMEOUT}"
is_uint "${HSYNC_RECONNECT_DELAY}"     && set -- "$@" --retry-delay "${HSYNC_RECONNECT_DELAY}"
is_uint "${HSYNC_STABILITY_DELAY}"     && set -- "$@" --stability-delay "${HSYNC_STABILITY_DELAY}"
is_uint "${HSYNC_MAX_WAIT_TIMEOUT}"    && set -- "$@" --max-wait-timeout "${HSYNC_MAX_WAIT_TIMEOUT}"
if [ "${HSYNC_MODE}" = "server" ]; then
    [ "${HSYNC_INSECURE}" = "true" ] && set -- "$@" --insecure
    [ -n "${HSYNC_CA_CERT}" ]        && set -- "$@" --ca-cert "${HSYNC_CA_CERT}"
fi

# ---------------------------------------------------------------------------
# Launch under nohup. Secrets are exported (not passed as flags) so the binary
# picks them up via HSYNC_API_KEY / HSYNC_P2P_KEY without leaking them through
# ``ps``. HSYNC_NTP_HOSTS is exported too because it is read from the process
# environment (never a CLI flag) by the client's SNTP probe (ADR-0023).
#
# NOTE: start.sh is deliberately "allexport-free": every variable the daemon
# must see is exported *explicitly* here. If you add an env-driven feature,
# add its export below as well — a sourced variable that is never exported is
# invisible to the nohup'd binary.
# ---------------------------------------------------------------------------
export HSYNC_API_KEY
export HSYNC_P2P_KEY
export HSYNC_P2P_PEER_DOWNLOAD
export HSYNC_P2P_PEER_READ_CAP
export HSYNC_P2P_PEER_SOURCES_MAX
export HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES
export HSYNC_NTP_HOSTS
# Cross-network discovery: the bootnode host and the UPnP choice are
# env-driven (the client also accepts a positional, but the service exports
# them so no argument ordering is involved).
export HSYNC_BOOTNODE
export HSYNC_BOOTNODE_FINGERPRINT
export HSYNC_BOOTNODE_TOFU
export HSYNC_BOOTNODE_TRUST
export HSYNC_BOOTNODE_PORTS
export HSYNC_UPNP_ENABLED
# Server-mode endpoint: exported like the keys above — the client reads it
# from the environment (no CLI flag carries the URL anymore).
export HSYNC_SERVER
# Server TLS trust (ADR-0036): the pin/tofu/policy are env-driven too.
export HSYNC_SERVER_TRUST
export HSYNC_SERVER_FINGERPRINT
export HSYNC_SERVER_TOFU
# Per-file retry waits have no CLI flags; EnvSettings reads them from the
# process environment. Empty values stay unset so autotune/defaults apply.
[ -n "${HSYNC_TRANSFER_RETRY_MIN_WAIT}" ] && export HSYNC_TRANSFER_RETRY_MIN_WAIT
[ -n "${HSYNC_TRANSFER_RETRY_MAX_WAIT}" ] && export HSYNC_TRANSFER_RETRY_MAX_WAIT
# Causal state lives outside the synced folder (ADR-0025). Default next to
# the NAS config/log tree so it survives package upgrades.
HSYNC_STATE_DIR="${HSYNC_STATE_DIR:-/mnt/HD/HD_a2/hsync/state}"
export HSYNC_STATE_DIR

cd "${APKG_PATH}"
nohup "${HSYNC_BIN}" "$@" >> "${HSYNC_LOG_FILE}" 2>&1 &
echo $! > "${HSYNC_PID_FILE}"

exit 0
