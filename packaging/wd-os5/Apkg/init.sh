#!/bin/sh
#
# init.sh — runs at every boot.
#
# Re-creates the symlinks under /var/www/apps/${APKG_NAME}/ so the dashboard
# can still reach the icon and the configuration page after a reboot in case
# /var/www is repopulated from a read-only template.

LOG=/tmp/debug_apkg
[ -f "$LOG" ] && echo "APKG_DEBUG: $0 $*" >> "$LOG"

APKG_PATH=$(readlink -f "$1")
. "${APKG_PATH}/env"

mkdir -p "${WEB_ROOT}"
chown 0:0 "${WEB_ROOT}" 2>/dev/null || true
chmod 0755 "${WEB_ROOT}" 2>/dev/null || true
rm -f "${WEB_ROOT}"/* 2>/dev/null || true
for asset in "${APKG_PATH}"/web/*; do
    [ -e "${asset}" ] && ln -sf "${asset}" "${WEB_ROOT}/$(basename "${asset}")"
done
# nasAdmin returns 403 for /apps/<pkg>/ assets that are not owned by root,
# so re-normalise ownership/modes of the web directory at every boot.
chown -R 0:0 "${APKG_PATH}/web" 2>/dev/null || true
find "${APKG_PATH}/web" -type d -exec chmod 0755 {} + 2>/dev/null || true
find "${APKG_PATH}/web" -type f -exec chmod 0644 {} + 2>/dev/null || true
chmod 0755 "${APKG_PATH}"/web/*.php 2>/dev/null || true

exit 0
