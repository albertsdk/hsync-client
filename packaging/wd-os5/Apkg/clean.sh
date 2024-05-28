#!/bin/sh
#
# clean.sh — remove only the web UI symlinks (called by the package manager
# when refreshing the apache docroot without a full uninstall).

set -u

LOG=/tmp/debug_apkg
echo "APKG_DEBUG: $0 $*" >> "$LOG"

APKG_PATH=$(readlink -f "$1")
. "${APKG_PATH}/env"

rm -rf "${WEB_ROOT}"

exit 0
