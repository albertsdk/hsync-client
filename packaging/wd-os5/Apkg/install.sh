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

set -u

LOG=/tmp/debug_apkg
echo "APKG_DEBUG: $0 $*" >> "$LOG"

INSTALL_DIR=$(readlink -f "$1")
NAS_PROG=$(readlink -f "$2")

. "${INSTALL_DIR}/env"

APKG_PATH="${NAS_PROG}/${APKG_NAME}"

echo "move package data to ${APKG_PATH}" >> "$LOG"
mv "${INSTALL_DIR}" "${APKG_PATH}" 2>&1 >> "$LOG"

# Ensure the binary is executable.
chmod 0755 "${APKG_PATH}/bin/${BINARY}" 2>/dev/null || true

# Persistent layout on the data volume.
mkdir -p "${HSYNC_DATA_DIR}" "${HSYNC_CONFIG_DIR}" "${HSYNC_LOG_DIR}" "${HSYNC_RUN_DIR}"
chmod 0775 "${HSYNC_ROOT}" "${HSYNC_DATA_DIR}" "${HSYNC_CONFIG_DIR}" "${HSYNC_LOG_DIR}" "${HSYNC_RUN_DIR}"

# Seed a default configuration on a fresh install. Existing user config is
# preserved (postinst.sh restores from the preinst backup if present).
if [ ! -f "${HSYNC_ENV_FILE}" ]; then
    cat > "${HSYNC_ENV_FILE}" <<EOF
# HSYNC configuration — edited from the web UI at http://<NAS>/${APKG_NAME}/
# Values follow the .env convention; quoted strings are recommended.

# Mode: lan | server
HSYNC_MODE="lan"

# Local path to synchronize.
HSYNC_PATH="${HSYNC_DATA_DIR}"

# Bucket name (alphanumeric, hyphens and underscores).
HSYNC_BUCKET="hsync"

# Role in LAN mode: auto | master | slave
HSYNC_ROLE="auto"

# LAN options
HSYNC_LAN_PERSISTENT="true"
HSYNC_LAN_HTTPS="true"
HSYNC_LAN_GROUP=""
HSYNC_DISCOVERY_TIMEOUT="10"

# Server options (used only when HSYNC_MODE=server)
HSYNC_SERVER=""
HSYNC_API_KEY=""

# Sync behaviour
HSYNC_WATCH="true"
HSYNC_INTERVAL="0"
HSYNC_IGNORE_DELETE="false"
HSYNC_NO_COMPRESSION="false"
HSYNC_MAX_BW_UPLOAD=""
HSYNC_MAX_BW_DOWNLOAD=""
HSYNC_MIN_FREE_DISK_MB=""

# Logging
HSYNC_VERBOSE="false"
EOF
    chmod 0640 "${HSYNC_ENV_FILE}"
fi

exit 0
