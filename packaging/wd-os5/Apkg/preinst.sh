#!/bin/sh
#
# preinst.sh — runs before install.sh on package (re)install.
#
# Stops a running daemon and snapshots the existing on-disk configuration so
# postinst.sh can restore it after the new package layout is moved into
# place. Persistent user data and config live under /mnt/HD/HD_a2/hsync/ and
# are never touched here.

set -u

LOG=/tmp/debug_apkg
echo "APKG_DEBUG: $0 $*" >> "$LOG"

NAS_PROG="/shares/Volume_1/Nas_Prog"
APKG_MODULE="hsync"
APKG_PATH="${NAS_PROG}/${APKG_MODULE}"

# Stop any running instance from the previous install.
if [ -x "${APKG_PATH}/stop.sh" ]; then
    "${APKG_PATH}/stop.sh" "${APKG_PATH}" >> "$LOG" 2>&1 || true
fi

# Nothing else to back up — configuration lives outside ${APKG_PATH}.
exit 0
