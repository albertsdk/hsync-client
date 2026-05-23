#!/bin/sh
#
# config_get.cgi — return the current hsync.env as a JSON object.
#
# The web UI consumes this on page load to populate the form. Only the
# whitelisted HSYNC_* keys are emitted; unknown lines in the .env file are
# ignored. A synthetic ``__VERSION__`` key carries the package version so
# the footer can display it without a separate round-trip.

# shellcheck disable=SC1091
APKG_PATH="/shares/Volume_1/Nas_Prog/hsync"
. "${APKG_PATH}/env"

VERSION_FILE="${APKG_PATH}/VERSION"
VERSION=$([ -f "${VERSION_FILE}" ] && cat "${VERSION_FILE}" || echo "unknown")

# Allowed config keys (whitelist).
KEYS="HSYNC_MODE HSYNC_PATH HSYNC_BUCKET HSYNC_ROLE \
      HSYNC_LAN_PERSISTENT HSYNC_LAN_HTTPS HSYNC_LAN_GROUP HSYNC_DISCOVERY_TIMEOUT \
      HSYNC_SERVER HSYNC_API_KEY \
      HSYNC_WATCH HSYNC_INTERVAL HSYNC_IGNORE_DELETE HSYNC_NO_COMPRESSION \
      HSYNC_MAX_BW_UPLOAD HSYNC_MAX_BW_DOWNLOAD HSYNC_MIN_FREE_DISK_MB HSYNC_VERBOSE"

# Load the env file in a subshell so we don't pollute our own namespace
# with stray exports. ``set -a`` would export everything; we just source.
if [ -f "${HSYNC_ENV_FILE}" ]; then
    # shellcheck disable=SC1090
    . "${HSYNC_ENV_FILE}"
fi

# Minimal JSON string escaper: backslash, double-quote, control chars.
json_escape() {
    printf '%s' "$1" | awk '
        BEGIN { ORS="" }
        {
            s = $0
            gsub(/\\/, "\\\\", s)
            gsub(/"/,  "\\\"", s)
            gsub(/\t/, "\\t",  s)
            gsub(/\r/, "\\r",  s)
            print s
            if (NR != 0) printf "\\n"
        }
    ' | sed 's/\\n$//'
}

printf 'Content-Type: application/json; charset=utf-8\r\n'
printf 'Cache-Control: no-store\r\n'
printf '\r\n'

printf '{'
first=1
for key in ${KEYS}; do
    eval "val=\${${key}:-}"
    [ ${first} -eq 1 ] || printf ','
    first=0
    printf '"%s":"%s"' "${key}" "$(json_escape "${val}")"
done
printf ',"__VERSION__":"%s"' "$(json_escape "${VERSION}")"
printf '}'
