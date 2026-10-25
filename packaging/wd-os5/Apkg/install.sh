#!/bin/sh
#
# install.sh — invoked by the OS5 package manager at install time.
#
# Arguments (positional, set by the OS):
#   $1  staging directory (where the .bin was unpacked)
#   $2  Nas_Prog root (typically /shares/Volume_1/Nas_Prog)
#
# Moves the unpacked package into its permanent location, ensures the
# persistent data directories exist on the data volume, and seeds a default
# hsync.env if the user does not already have one.

LOG=/tmp/debug_apkg
[ -f "$LOG" ] && echo "APKG_DEBUG: $0 $*" >> "$LOG"

INSTALL_DIR=$(readlink -f "$1")
NAS_PROG=$(readlink -f "$2")

. "${INSTALL_DIR}/env"

APKG_PATH="${NAS_PROG}/${APKG_NAME}"

echo "merge package data into ${APKG_PATH}" >> "$LOG"
# The OS5 package manager pre-creates ${APKG_PATH} and drops its own
# ``apkg.xml`` inside before invoking install.sh, so we cannot simply
# rename ``${INSTALL_DIR}`` over it (would yield ${APKG_PATH}/${APKG_NAME}/)
# nor wipe it (would clobber the PM-managed apkg.xml). Merge the staged
# tree into the destination instead.
mkdir -p "${APKG_PATH}"
( cd "${INSTALL_DIR}" && tar cf - . ) | ( cd "${APKG_PATH}" && tar xf - ) >> "$LOG" 2>&1
rm -rf "${INSTALL_DIR}"

# Normalise owner/mode of the installed tree. mksapkg-OS5 preserves the
# uid/gid present in the build host's staging directory; on the NAS those
# numeric ids typically map to "UNKNOWN:share" and the dashboard's nasAdmin
# proxy returns 403 for /apps/<pkg>/ assets that are not owned by root.
# Mirror the layout of the stock OS5 apps (see /var/www/apps/Transmission/).
chown -R 0:0 "${APKG_PATH}" 2>/dev/null || true
find "${APKG_PATH}" -type d -exec chmod 0755 {} + 2>/dev/null || true
find "${APKG_PATH}" -type f -exec chmod 0644 {} + 2>/dev/null || true

# Lifecycle hook scripts must be executable or the OS5 package manager
# silently skips them (install/remove/start/stop all fail without +x).
chmod 0755 "${APKG_PATH}"/*.sh 2>/dev/null || true

# Ensure the binary is executable (--onedir layout: bin/${BINARY}/${BINARY}).
chmod 0755 "${APKG_PATH}/bin/${BINARY}/${BINARY}" 2>/dev/null || true

# Expose the web UI under /apps/${APKG_NAME}/ — the path the dashboard uses
# to fetch the icon and to open the "Configure" page. Mirrors the layout of
# the stock OS5 apps (see /var/www/apps/Transmission/).
mkdir -p "${WEB_ROOT}"
chown 0:0 "${WEB_ROOT}" 2>/dev/null || true
chmod 0755 "${WEB_ROOT}" 2>/dev/null || true
rm -f "${WEB_ROOT}"/* 2>/dev/null || true
for asset in "${APKG_PATH}"/web/*; do
    [ -e "${asset}" ] && ln -sf "${asset}" "${WEB_ROOT}/$(basename "${asset}")"
done
chmod 0755 "${APKG_PATH}"/web/*.php 2>/dev/null || true

# Persistent layout on the data volume. Ownership is locked to root and the
# config directory is not writable by group/other: hsync.env is sourced as
# root by start.sh, so a config writable by a local NAS user (e.g. the
# "share" group) would let that user inject shell commands and escalate to
# root at the next service start (local privilege escalation).
mkdir -p "${HSYNC_DATA_DIR}" "${HSYNC_CONFIG_DIR}" "${HSYNC_LOG_DIR}" "${HSYNC_RUN_DIR}"
chown 0:0 "${HSYNC_ROOT}" "${HSYNC_DATA_DIR}" "${HSYNC_CONFIG_DIR}" "${HSYNC_LOG_DIR}" "${HSYNC_RUN_DIR}" 2>/dev/null || true
chmod 0755 "${HSYNC_ROOT}" "${HSYNC_DATA_DIR}" "${HSYNC_LOG_DIR}" "${HSYNC_RUN_DIR}"
chmod 0750 "${HSYNC_CONFIG_DIR}"

# Seed a default configuration on a fresh install. Existing user config is
# preserved (postinst.sh restores from the preinst backup if present).
if [ ! -f "${HSYNC_ENV_FILE}" ]; then
    # 24 characters from the desktop client's unambiguous alphabet: a shared
    # password the user can type on another device (12+ is required; 24 gives
    # ~139 bits). The client derives the 32-byte group key with scrypt.
    P2P_PASSPHRASE=$(LC_ALL=C tr -dc 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789' < /dev/urandom | dd bs=1 count=24 2>/dev/null)
    if [ "${#P2P_PASSPHRASE}" -ne 24 ]; then
        echo "cannot generate a secure p2p password" >> "$LOG"
        exit 1
    fi
    cat > "${HSYNC_ENV_FILE}" <<EOF
# HSYNC configuration — edited from the web UI at http://<NAS>/${APKG_NAME}/
# Values follow the .env convention; quoted strings are recommended.
#
# Defaults match the HSYNC desktop application, with one deliberate exception:
# the NAS is a BACKUP target, so deletions made on the clients are never
# propagated here (HSYNC_IGNORE_DELETE=true) and the backup cannot lose files
# because a source host removed them.

# Mode: p2p | server
HSYNC_MODE="p2p"

# Local folder to synchronize.
HSYNC_PATH="${HSYNC_DATA_DIR}"

# Share name (alphanumeric, hyphens and underscores).
HSYNC_BUCKET="my-bucket"

# Sync type: bidirectional | upload-only | download-only
HSYNC_SYNC_DIRECTION="bidirectional"

# Restrict the sync to one subfolder of the shared folder (relative; empty = all).
HSYNC_FILTER_PATH=""

# Extra gitignore-style patterns, comma-separated (layered on .hsyncignore).
HSYNC_IGNORE_PATTERNS=""

# p2p mode. The transport is HTTPS/WSS-only (protocol p2p/1): the master keeps
# a persistent identity and slaves pin its fingerprint (TOFU).
HSYNC_P2P_GROUP="my-group"
# Random shared password generated at install time (at least 12 characters);
# required for p2p writes. Use the same value on every device of the group.
HSYNC_P2P_PASSPHRASE="${P2P_PASSPHRASE}"
# Master TCP listen port (0 = auto-assign).
HSYNC_P2P_PORT="0"
# Role: an equal peer unless one of the two overrides below is set.
HSYNC_PREFER_MASTER="false"
HSYNC_NEVER_MASTER="false"
# Strict pairing: refuse a first-seen master fingerprint until confirmed
# out of band (disables Trust-On-First-Use auto-pinning).
HSYNC_P2P_REQUIRE_PAIRING="false"
# Peer-assisted reads and the caps that bound them.
HSYNC_P2P_PEER_DOWNLOAD="true"
HSYNC_P2P_PEER_READ_CAP="2"
HSYNC_P2P_PEER_SOURCES_MAX="3"
# Writes without the p2p key stay blocked unless these dangerous opt-ins are set.
HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES="false"
HSYNC_P2P_ALLOW_UNAUTHENTICATED="false"
HSYNC_ALLOW_DELETIONS="false"
# Seconds to wait for peer discovery.
HSYNC_DISCOVERY_TIMEOUT="10"
# Optional discovery server for cross-network discovery (bare host, no scheme,
# no port; empty = discovery stays on the local network). Set the same host on
# every device of the group.
HSYNC_BOOTNODE=""
# Extra UDP ports to try for the same discovery server (comma-separated, ordered),
# e.g. "53" so a client whose egress firewall blocks UDP 443 reaches the node
# on 53. Empty = single-port. The discovery server must listen on the same set.
HSYNC_BOOTNODE_PORTS=""
# Pin the discovery server certificate fingerprint ('sha256:<hex>' or bare hex,
# comma-separated up to two for a rotation window); empty = no pin (fail closed).
HSYNC_BOOTNODE_FINGERPRINT=""
# Trust the discovery server certificate on first use (TOFU).
HSYNC_BOOTNODE_TOFU="false"
# Discovery server identity check: ask | accept-new | strict | none
HSYNC_BOOTNODE_TRUST="ask"
# Map the master TCP port with UPnP so peers on other networks can dial this
# device. Only effective with a discovery server.
HSYNC_UPNP_ENABLED="true"

# Server mode (used only when HSYNC_MODE=server)
HSYNC_SERVER=""
HSYNC_API_KEY=""
# Skip the certificate check (insecure; mutually exclusive with a pin or TOFU).
HSYNC_INSECURE="false"
# Certificate authority file used to validate the server certificate.
HSYNC_CA_CERT=""
# Server identity check: chain (standard CA validation) | ask | accept-new | strict | none
HSYNC_SERVER_TRUST="chain"
# Pin the server certificate fingerprint ('sha256:<hex>' or bare hex,
# comma-separated up to two for a rotation window); empty = no pin.
HSYNC_SERVER_FINGERPRINT=""
# Trust the server certificate on first use (TOFU).
HSYNC_SERVER_TOFU="false"

# Sync behaviour
# Periodic full check in seconds (0 = disabled).
HSYNC_INTERVAL="0"
HSYNC_IGNORE_DELETE="true"
HSYNC_COMPRESSION_ENABLED="true"
HSYNC_DELTA_ENABLED="true"
# Delta floors in KB; files at least this large use block-level sync.
HSYNC_DELTA_MIN_FILE_SIZE="256"
HSYNC_DELTA_MIN_FILE_SIZE_P2P="1024"
HSYNC_MAX_BW_UPLOAD=""
HSYNC_MAX_BW_DOWNLOAD=""
HSYNC_MIN_FREE_DISK="500MB"
HSYNC_MAX_INBOUND_SIZE_MB="10240"

# Deletion protection: pause a cycle that would delete too many local files.
HSYNC_MASS_DELETE_GUARD="true"
HSYNC_MASS_DELETE_THRESHOLD="50"
HSYNC_MASS_DELETE_PERCENT="50"

# Timing
HSYNC_HTTP_TIMEOUT="30"
HSYNC_RECONNECT_DELAY="5"
HSYNC_STABILITY_DELAY="5"
HSYNC_MAX_WAIT_TIMEOUT="120"
# Per-file retry waits (seconds); empty = engine default.
HSYNC_TRANSFER_RETRY_MIN_WAIT=""
HSYNC_TRANSFER_RETRY_MAX_WAIT=""

# Performance
HSYNC_AUTOTUNE="true"
# Simultaneous file transfers (0 = auto).
HSYNC_MAX_CONCURRENT_TRANSFERS="0"
# SNTP servers used for clock-offset correction in p2p mode.
HSYNC_NTP_HOSTS=""

# Logging
HSYNC_VERBOSE="false"
HSYNC_QUIET="false"
EOF
    chown 0:0 "${HSYNC_ENV_FILE}" 2>/dev/null || true
    chmod 0640 "${HSYNC_ENV_FILE}"
fi

exit 0
