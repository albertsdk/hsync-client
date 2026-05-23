#!/bin/sh
#
# init.sh — runs at install time (after install.sh) and on every boot.
#
# Wires the package web UI into the stock apache docroot so the user can
# reach it at http://<NAS>/${APKG_NAME}/ from the My Cloud dashboard.

set -u

LOG=/tmp/debug_apkg
echo "APKG_DEBUG: $0 $*" >> "$LOG"

APKG_PATH=$(readlink -f "$1")
. "${APKG_PATH}/env"

mkdir -p "${WEB_ROOT}"
# Re-create symlinks idempotently so updates pick up new assets.
rm -f "${WEB_ROOT}"/* 2>/dev/null || true
ln -sf "${APKG_PATH}"/web/* "${WEB_ROOT}/"

# CGI scripts need to be executable for the OS5 apache mod_cgi.
chmod 0755 "${APKG_PATH}"/web/*.cgi 2>/dev/null || true

exit 0
