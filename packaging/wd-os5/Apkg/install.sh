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
    P2P_KEY=$(dd if=/dev/urandom bs=24 count=1 2>/dev/null | od -An -tx1 | tr -d ' \n')
    if [ "${#P2P_KEY}" -ne 48 ]; then
        echo "cannot generate a secure p2p key" >> "$LOG"
        exit 1
    fi
    cat > "${HSYNC_ENV_FILE}" <<EOF
# HSYNC configuration — edited from the web UI at http://<NAS>/${APKG_NAME}/
# Values follow the .env convention; quoted strings are recommended.

# Defaults tuned for using the NAS as a BACKUP target: it runs as the LAN
# master (coordinating the sync group) and never loses files when a client
# deletes them (HSYNC_IGNORE_DELETE=true). Adjust from the web UI for other
# use cases.

# Mode: p2p | server
HSYNC_MODE="p2p"

# Local path to synchronize.
HSYNC_PATH="${HSYNC_DATA_DIR}"

# Bucket name (alphanumeric, hyphens and underscores).
HSYNC_BUCKET="hsync"

# Role in p2p mode: auto | master | slave | never-master
HSYNC_ROLE="master"

# LAN options. The LAN transport is HTTPS/WSS-only (protocol p2p/1); the master
# uses a persistent self-signed identity and slaves pin its fingerprint (TOFU).
HSYNC_P2P_GROUP=""
# Random shared secret generated at install time; required for LAN writes.
HSYNC_P2P_KEY="${P2P_KEY}"
# Strict pairing: refuse a first-seen master fingerprint until confirmed
# out of band (disables Trust-On-First-Use auto-pinning).
HSYNC_P2P_REQUIRE_PAIRING="false"
# Peer-assisted reads are enabled by default. Writes without the p2p key remain
# blocked unless the dangerous explicit opt-in is enabled.
HSYNC_P2P_PEER_DOWNLOAD="true"
HSYNC_P2P_PEER_READ_CAP="2"
HSYNC_P2P_PEER_SOURCES_MAX="3"
HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES="false"
HSYNC_P2P_ALLOW_UNAUTHENTICATED="false"
HSYNC_ALLOW_DELETIONS="false"
HSYNC_DISCOVERY_TIMEOUT="0"
# Optional bootnode host for cross-network discovery (bare hostname, no scheme
# and no port; empty = discovery stays on the local network). Set the same host
# on every device of the group.
HSYNC_BOOTNODE=""
# Pin the bootnode certificate fingerprint ('sha256:<hex>' or bare hex,
# comma-separated up to two for a rotation window); empty = no pin (fail closed).
HSYNC_BOOTNODE_FINGERPRINT=""
# Trust the bootnode certificate on first use (TOFU).
HSYNC_BOOTNODE_TOFU="false"
# Extra UDP ports to try for the same bootnode (comma-separated, ordered),
# e.g. "53" so a client whose egress firewall blocks UDP 443 reaches the node
# on 53. Empty = single-port. The bootnode must listen on the same set.
HSYNC_BOOTNODE_PORTS=""
# Map the master TCP port with UPnP so peers on other networks can dial this
# device. Only effective with HSYNC_BOOTNODE set. Opt-in: set to "true" to
# enable (default "false").
HSYNC_UPNP_ENABLED="false"

# Server options (used only when HSYNC_MODE=server)
HSYNC_SERVER=""
HSYNC_API_KEY=""
# Sync-server TLS trust policy (ADR-0036): chain (default, standard CA
# validation) or the SSH-style ask/accept-new/strict/none for a self-signed
# server. Empty = the client default (chain).
HSYNC_SERVER_TRUST=""
# Pin the sync-server certificate fingerprint ('sha256:<hex>' or bare hex,
# comma-separated up to two for a rotation window); empty = no pin.
HSYNC_SERVER_FINGERPRINT=""
# Trust the sync-server certificate on first use (TOFU).
HSYNC_SERVER_TOFU="false"

# Sync behaviour (continuous/daemon mode is the client default)
HSYNC_INTERVAL="0"
HSYNC_IGNORE_DELETE="true"
HSYNC_NO_COMPRESSION="false"
HSYNC_MAX_BW_UPLOAD=""
HSYNC_MAX_BW_DOWNLOAD=""
HSYNC_MIN_FREE_DISK_MB=""
HSYNC_MAX_INBOUND_SIZE_MB=""

# Logging
HSYNC_VERBOSE="false"
EOF
    chown 0:0 "${HSYNC_ENV_FILE}" 2>/dev/null || true
    chmod 0640 "${HSYNC_ENV_FILE}"
fi

exit 0
