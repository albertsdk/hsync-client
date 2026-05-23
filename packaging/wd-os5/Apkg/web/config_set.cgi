#!/bin/sh
#
# config_set.cgi — write hsync.env from a URL-encoded POST body.
#
# Accepts ``application/x-www-form-urlencoded`` only. Each whitelisted
# HSYNC_* key is written exactly once to a temporary file which is then
# moved into place atomically (``mv -f``) to avoid leaving the daemon with
# a half-written configuration.

# shellcheck disable=SC1091
APKG_PATH="/shares/Volume_1/Nas_Prog/hsync"
. "${APKG_PATH}/env"

json_reply() {
    printf 'Content-Type: application/json; charset=utf-8\r\n'
    printf 'Cache-Control: no-store\r\n'
    printf '\r\n'
    printf '%s' "$1"
}

# Method and content-type checks.
if [ "${REQUEST_METHOD:-}" != "POST" ]; then
    printf 'Status: 405 Method Not Allowed\r\n'
    json_reply '{"error":"method_not_allowed"}'
    exit 0
fi

CT="${CONTENT_TYPE:-}"
case "${CT}" in
    application/x-www-form-urlencoded*) ;;
    *)
        printf 'Status: 415 Unsupported Media Type\r\n'
        json_reply '{"error":"unsupported_media_type"}'
        exit 0
        ;;
esac

# Cap body size to a sane upper bound (1 MiB).
LEN=${CONTENT_LENGTH:-0}
if [ "${LEN}" -le 0 ] || [ "${LEN}" -gt 1048576 ]; then
    printf 'Status: 413 Payload Too Large\r\n'
    json_reply '{"error":"payload_size"}'
    exit 0
fi

BODY=$(dd bs=1 count="${LEN}" 2>/dev/null)

# Whitelist of accepted keys.
KEYS="HSYNC_MODE HSYNC_PATH HSYNC_BUCKET HSYNC_ROLE \
      HSYNC_LAN_PERSISTENT HSYNC_LAN_HTTPS HSYNC_LAN_GROUP HSYNC_DISCOVERY_TIMEOUT \
      HSYNC_SERVER HSYNC_API_KEY \
      HSYNC_WATCH HSYNC_INTERVAL HSYNC_IGNORE_DELETE HSYNC_NO_COMPRESSION \
      HSYNC_MAX_BW_UPLOAD HSYNC_MAX_BW_DOWNLOAD HSYNC_MIN_FREE_DISK_MB HSYNC_VERBOSE"

# URL-decode helper: ``+`` → space, ``%xx`` → byte.
urldecode() {
    printf '%b' "$(printf '%s' "$1" | sed 's/+/ /g; s/%/\\x/g')"
}

# Extract a single key from BODY (last occurrence wins, matching CGI norms).
extract() {
    _k="$1"
    printf '%s' "${BODY}" \
        | tr '&' '\n' \
        | awk -v k="${_k}" -F= 'BEGIN{v=""} $1==k { $1=""; sub(/^=/, ""); v=$0 } END{print v}'
}

# Single-quote escape for the .env file (POSIX-safe).
shquote() {
    printf %s "$1" | sed "s/'/'\\\\''/g"
}

mkdir -p "${HSYNC_CONFIG_DIR}"
TMP=$(mktemp "${HSYNC_CONFIG_DIR}/.hsync.env.XXXXXX")
trap 'rm -f "${TMP}"' EXIT

{
    printf '# HSYNC configuration — written by config_set.cgi on %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    for key in ${KEYS}; do
        raw=$(extract "${key}")
        val=$(urldecode "${raw}")
        # Defensive validation for the few fields with strict grammars.
        case "${key}" in
            HSYNC_MODE)
                case "${val}" in lan|server) ;; *) val="lan" ;; esac
                ;;
            HSYNC_ROLE)
                case "${val}" in auto|master|slave) ;; *) val="auto" ;; esac
                ;;
            HSYNC_BUCKET)
                printf '%s' "${val}" | grep -Eq '^[a-zA-Z0-9]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$' \
                    || val="hsync"
                ;;
            HSYNC_DISCOVERY_TIMEOUT|HSYNC_INTERVAL|HSYNC_MIN_FREE_DISK_MB)
                printf '%s' "${val}" | grep -Eq '^[0-9]+$' || val=""
                ;;
            HSYNC_LAN_PERSISTENT|HSYNC_LAN_HTTPS|HSYNC_WATCH|HSYNC_IGNORE_DELETE|HSYNC_NO_COMPRESSION|HSYNC_VERBOSE)
                case "${val}" in true|false) ;; *) val="false" ;; esac
                ;;
        esac
        printf "%s='%s'\n" "${key}" "$(shquote "${val}")"
    done
} > "${TMP}"

chmod 0640 "${TMP}"
mv -f "${TMP}" "${HSYNC_ENV_FILE}"
trap - EXIT

json_reply '{"ok":true}'
