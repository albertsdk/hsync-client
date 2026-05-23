#!/bin/sh
#
# postinst.sh — runs after install.sh.
#
# Persistent configuration is stored under /mnt/HD/HD_a2/hsync/config/ and
# is not affected by package re-install, so nothing needs to be restored
# here. The hook is kept for symmetry with the OS5 lifecycle and to make
# future migration logic (config schema bumps) trivial to add.

set -u

LOG=/tmp/debug_apkg
echo "APKG_DEBUG: $0 $*" >> "$LOG"

exit 0
