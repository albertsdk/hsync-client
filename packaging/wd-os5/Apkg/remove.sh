#!/bin/sh
#
# remove.sh — uninstall hook. Stops the daemon, removes the package tree
# and the web UI symlinks. **User data and configuration under
# /mnt/HD/HD_a2/hsync/ are intentionally kept** so the user can re-install
# without losing the synchronized files; a manual ``rm -rf`` of that
# directory is required to fully wipe HSYNC from the device.

set -u

LOG=/tmp/debug_apkg
echo "APKG_DEBUG: $0 $*" >> "$LOG"

APKG_PATH=$(readlink -f "$1")
. "${APKG_PATH}/env"

# Stop the daemon if running.
if [ -x "${APKG_PATH}/stop.sh" ]; then
    "${APKG_PATH}/stop.sh" "${APKG_PATH}" >> "$LOG" 2>&1 || true
fi

# Remove the package tree and the web UI symlinks.
rm -rf "${APKG_PATH}"
rm -rf "${WEB_ROOT}"

exit 0
