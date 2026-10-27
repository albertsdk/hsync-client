<?php
/*
 * config_set.php — write hsync.env from a URL-encoded POST body.
 *
 * Accepts application/x-www-form-urlencoded only.  Each whitelisted HSYNC_*
 * key is written exactly once to a temporary file which is then renamed into
 * place atomically to avoid leaving the daemon with a half-written config.
 * Every value is validated against the range the sync engine accepts, so the
 * file this endpoint writes can always be launched by start.sh.
 */

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

define('HSYNC_ENV',    '/mnt/HD/HD_a2/hsync/config/hsync.env');
define('HSYNC_CONFIG', '/mnt/HD/HD_a2/hsync/config');
define('HSYNC_DEFAULT_PATH', '/mnt/HD/HD_a2/hsync/data');

// Method check.
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo '{"error":"method_not_allowed"}';
    exit;
}

// Content-type check.
$ct = $_SERVER['CONTENT_TYPE'] ?? '';
if (strpos($ct, 'application/x-www-form-urlencoded') === false) {
    http_response_code(415);
    echo '{"error":"unsupported_media_type"}';
    exit;
}

// Read raw body so we can call parse_str ourselves (avoids php.ini limits on
// max_input_vars which could silently drop keys when the form is large).
$raw = file_get_contents('php://input');
if ($raw === false) {
    $raw = '';
}
if (strlen($raw) > 1048576) {
    http_response_code(413);
    echo '{"error":"payload_size"}';
    exit;
}
parse_str($raw, $post);
if ($post === [] && $_POST !== []) {
    // The SAPI already parsed the body (php://input is empty in that case);
    // the parsed form is the same payload, so validate it directly.
    $post = $_POST;
}

const KEYS = [
    'HSYNC_MODE', 'HSYNC_PATH', 'HSYNC_BUCKET',
    'HSYNC_SYNC_DIRECTION', 'HSYNC_FILTER_PATH', 'HSYNC_IGNORE_PATTERNS',
    'HSYNC_P2P_GROUP', 'HSYNC_P2P_PASSPHRASE', 'HSYNC_P2P_PORT',
    'HSYNC_PREFER_MASTER', 'HSYNC_NEVER_MASTER',
    'HSYNC_P2P_TRUST', 'HSYNC_P2P_PEER_DOWNLOAD',
    'HSYNC_P2P_PEER_READ_CAP', 'HSYNC_P2P_PEER_SOURCES_MAX',
    'HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES', 'HSYNC_P2P_ALLOW_UNAUTHENTICATED',
    'HSYNC_ALLOW_DELETIONS', 'HSYNC_DISCOVERY_TIMEOUT',
    'HSYNC_BOOTNODE', 'HSYNC_BOOTNODE_PORTS', 'HSYNC_BOOTNODE_FINGERPRINT',
    'HSYNC_BOOTNODE_TOFU', 'HSYNC_BOOTNODE_TRUST', 'HSYNC_UPNP_ENABLED',
    'HSYNC_NTP_HOSTS',
    'HSYNC_SERVER', 'HSYNC_API_KEY', 'HSYNC_INSECURE', 'HSYNC_CA_CERT',
    'HSYNC_SERVER_TRUST', 'HSYNC_SERVER_FINGERPRINT', 'HSYNC_SERVER_TOFU',
    'HSYNC_INTERVAL', 'HSYNC_DELETION_POLICY', 'HSYNC_COMPRESSION_ENABLED',
    'HSYNC_DELTA_ENABLED', 'HSYNC_DELTA_MIN_FILE_SIZE', 'HSYNC_DELTA_MIN_FILE_SIZE_P2P',
    'HSYNC_MAX_BW_UPLOAD', 'HSYNC_MAX_BW_DOWNLOAD', 'HSYNC_MIN_FREE_DISK',
    'HSYNC_MAX_INBOUND_SIZE_MB',
    'HSYNC_MASS_DELETE_GUARD', 'HSYNC_MASS_DELETE_THRESHOLD', 'HSYNC_MASS_DELETE_PERCENT',
    'HSYNC_HTTP_TIMEOUT', 'HSYNC_RECONNECT_DELAY', 'HSYNC_STABILITY_DELAY',
    'HSYNC_MAX_WAIT_TIMEOUT', 'HSYNC_TRANSFER_RETRY_MIN_WAIT', 'HSYNC_TRANSFER_RETRY_MAX_WAIT',
    'HSYNC_AUTOTUNE', 'HSYNC_MAX_CONCURRENT_TRANSFERS',
    'HSYNC_VERBOSE', 'HSYNC_QUIET', 'HSYNC_DRY_RUN',
];

// Clamp an integer field into the range the engine accepts; malformed or empty
// input falls back to the shipped default instead of writing an empty value.
function clamp_int(string $value, int $min, int $max, int $fallback): string {
    if (!preg_match('/^[0-9]+$/', $value)) {
        return (string)$fallback;
    }
    $n = (int)$value;
    if ($n < $min) $n = $min;
    if ($n > $max) $n = $max;
    return (string)$n;
}

$lines = ['# HSYNC configuration — written by config_set.php on ' . gmdate('Y-m-d\TH:i:s\Z')];

// Shared endpoint grammar (validate-only: the client normalizes on load).
// `host`, `host:port`, `[IPv6]`, `[IPv6]:port` — no scheme, no path.
$endpoint_re = '/^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$/';

foreach (KEYS as $key) {
    $val = (string)($post[$key] ?? '');

    // A value that spans lines would break the line-based .env parser (and
    // could smuggle another key into the file), so every field is collapsed to
    // a single line before validation.
    $val = (string)preg_replace('/[\x00-\x1F\x7F]+/', ' ', $val);
    $val = trim($val);

    // Defensive validation for fields with strict grammars.
    switch ($key) {
        case 'HSYNC_MODE':
            if (!in_array($val, ['p2p', 'server'], true)) $val = 'p2p';
            break;
        case 'HSYNC_PATH':
            // Absolute path without traversal; anything else keeps the default.
            if ($val === '' || $val[0] !== '/' || str_contains($val, '..')) {
                $val = HSYNC_DEFAULT_PATH;
            }
            break;
        case 'HSYNC_SYNC_DIRECTION':
            if (!in_array($val, ['bidirectional', 'upload-only', 'download-only'], true)) {
                $val = 'bidirectional';
            }
            break;
        case 'HSYNC_FILTER_PATH':
            // Bucket-relative subfolder: never absolute, never a traversal.
            $val = trim($val, "/ \t");
            if ($val === '' || str_contains($val, '..')) $val = '';
            break;
        case 'HSYNC_IGNORE_PATTERNS':
            // Comma-separated gitignore-style patterns on a single line: the
            // .env parser reads line by line, so a newline would truncate it.
            $patterns = [];
            foreach (preg_split('/\R/', $val) as $line) {
                foreach (explode(',', $line) as $pattern) {
                    $pattern = trim($pattern);
                    if ($pattern !== '' && !in_array($pattern, $patterns, true)) {
                        $patterns[] = $pattern;
                    }
                }
            }
            $val = implode(',', array_slice($patterns, 0, 50));
            break;
        case 'HSYNC_BOOTNODE':
            // HOST[:PORT] endpoint (hostname/IPv4 or bracketed IPv6); empty is
            // valid and means "mDNS-only discovery". No scheme, no path.
            if ($val !== '' && !preg_match($endpoint_re, $val)) $val = '';
            break;
        case 'HSYNC_SERVER':
            // A bare HOST[:PORT] endpoint or a full https:// URL; empty means
            // server mode is not configured. The client normalizes both forms.
            if ($val !== ''
                && !preg_match($endpoint_re, $val)
                && !preg_match('/^https:\/\//', $val)) $val = '';
            break;
        case 'HSYNC_BOOTNODE_FINGERPRINT':
            // Empty (no pin) or up to two 'sha256:<hex64>' / '<hex64>' pins,
            // comma-separated. Anything else is dropped to '' (fail closed).
            if ($val !== '') {
                $parts = array_map('trim', explode(',', $val));
                $valid = count($parts) <= 2;
                foreach ($parts as $p) {
                    if (!preg_match('/^(sha256:)?[0-9a-fA-F]{64}$/', $p)) $valid = false;
                }
                if (!$valid) $val = '';
            }
            break;
        case 'HSYNC_SERVER_FINGERPRINT':
            // Same grammar as the bootnode pins: empty or up to two sha256 pins.
            if ($val !== '') {
                $parts = array_map('trim', explode(',', $val));
                $valid = count($parts) <= 2;
                foreach ($parts as $p) {
                    if (!preg_match('/^(sha256:)?[0-9a-fA-F]{64}$/', $p)) $valid = false;
                }
                if (!$valid) $val = '';
            }
            break;
        case 'HSYNC_BUCKET':
            if (!preg_match('/^[a-zA-Z0-9]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$/', $val)) $val = 'my-bucket';
            break;
        case 'HSYNC_CA_CERT':
            // Absolute path to an existing readable PEM file, or empty.
            if ($val !== '' && (!str_starts_with($val, '/') || !is_file($val))) $val = '';
            break;
        case 'HSYNC_BOOTNODE_PORTS':
            // Comma-separated UDP ports (1-65535), order preserved; invalid and
            // duplicate entries are dropped (the client applies the same
            // normalization on load). Empty = single-port dialing (only the
            // bootnode/default port is tried).
            $ports = [];
            foreach (array_map('trim', explode(',', $val)) as $p) {
                if ($p === '' || !preg_match('/^[0-9]{1,5}$/', $p)) continue;
                $n = (int)$p;
                if ($n >= 1 && $n <= 65535 && !in_array($n, $ports, true)) $ports[] = $n;
            }
            $val = implode(',', $ports);
            break;
        case 'HSYNC_BOOTNODE_TRUST':
            // Discovery-server identity check; an unknown value keeps the default.
            if (!in_array($val, ['ask', 'accept-new', 'strict', 'none'], true)) $val = 'ask';
            break;
        case 'HSYNC_SERVER_TRUST':
            // Server identity check; chain is the standard CA validation.
            if (!in_array($val, ['chain', 'ask', 'accept-new', 'strict', 'none'], true)) $val = 'chain';
            break;
        case 'HSYNC_MIN_FREE_DISK':
            // A size with an explicit unit ("500MB", "2GB") or "0" (off): the
            // client rejects a bare number so a value cannot be read as bytes
            // when MB was meant.
            if ($val === '0') {
                break;
            }
            if (!preg_match('/^[0-9]+(\.[0-9]+)?\s*(B|K|KB|KiB|M|MB|MiB|G|GB|GiB|T|TB|TiB)$/i', $val)) $val = '';
            break;
        case 'HSYNC_MAX_BW_UPLOAD':
        case 'HSYNC_MAX_BW_DOWNLOAD':
            // Empty (unlimited) or a size with an explicit unit.
            if ($val !== '' && !preg_match('/^[0-9]+(\.[0-9]+)?\s*(B|K|KB|KiB|M|MB|MiB|G|GB|GiB|T|TB|TiB)$/i', $val)) {
                $val = '';
            }
            break;
        case 'HSYNC_DISCOVERY_TIMEOUT':
            $val = clamp_int($val, 5, 60, 10);
            break;
        case 'HSYNC_HTTP_TIMEOUT':
            $val = clamp_int($val, 1, 300, 30);
            break;
        case 'HSYNC_RECONNECT_DELAY':
            $val = clamp_int($val, 1, 60, 5);
            break;
        case 'HSYNC_STABILITY_DELAY':
            $val = clamp_int($val, 1, 60, 5);
            break;
        case 'HSYNC_MAX_WAIT_TIMEOUT':
            $val = clamp_int($val, 10, 3600, 120);
            break;
        case 'HSYNC_MAX_CONCURRENT_TRANSFERS':
            // 0 = let auto-tuning decide the parallelism.
            $val = clamp_int($val, 0, 20, 0);
            break;
        case 'HSYNC_MASS_DELETE_THRESHOLD':
            $val = clamp_int($val, 0, 100000, 50);
            break;
        case 'HSYNC_MASS_DELETE_PERCENT':
            $val = clamp_int($val, 0, 100, 50);
            break;
        case 'HSYNC_P2P_PASSPHRASE':
            // User password: the client enforces >=12 characters and derives
            // the group key. Count code points (not bytes) when mbstring is
            // available, and drop a shorter value instead of persisting one
            // the engine would reject at startup.
            $password_len = function_exists('mb_strlen')
                ? mb_strlen($val, 'UTF-8')
                : strlen($val);
            if ($val !== '' && $password_len < 12) $val = '';
            break;
        case 'HSYNC_P2P_PEER_READ_CAP':
            $val = clamp_int($val, 1, 32, 2);
            break;
        case 'HSYNC_P2P_PEER_SOURCES_MAX':
            $val = clamp_int($val, 1, 8, 3);
            break;
        case 'HSYNC_P2P_PORT':
            // 0 = auto-assign.
            $val = clamp_int($val, 0, 65535, 0);
            break;
        case 'HSYNC_MAX_INBOUND_SIZE_MB':
            $val = clamp_int($val, 0, 10000000, 10240);
            break;
        case 'HSYNC_INTERVAL':
            // 0 disables the periodic check; any other value is 20-3600 s.
            $interval = clamp_int($val, 0, 3600, 0);
            if ((int)$interval !== 0 && (int)$interval < 20) $interval = '20';
            $val = $interval;
            break;
        case 'HSYNC_DELTA_MIN_FILE_SIZE':
        case 'HSYNC_DELTA_MIN_FILE_SIZE_P2P':
            $val = clamp_int(
                $val,
                0,
                1048576,
                $key === 'HSYNC_DELTA_MIN_FILE_SIZE' ? 256 : 1024
            );
            break;
        case 'HSYNC_TRANSFER_RETRY_MIN_WAIT':
        case 'HSYNC_TRANSFER_RETRY_MAX_WAIT':
            if (!preg_match('/^[0-9]*\.?[0-9]*$/', $val)) $val = '';
            break;
        case 'HSYNC_P2P_TRUST':
            if (!in_array($val, ['accept-new', 'strict'], true)) $val = 'accept-new';
            break;
        case 'HSYNC_DELETION_POLICY':
            if (!in_array($val, ['keep', 'propagate'], true)) $val = 'keep';
            break;
        case 'HSYNC_PREFER_MASTER':
        case 'HSYNC_NEVER_MASTER':
        case 'HSYNC_INSECURE':
        case 'HSYNC_QUIET':
        case 'HSYNC_DRY_RUN':
        case 'HSYNC_MASS_DELETE_GUARD':
        case 'HSYNC_UPNP_ENABLED':
        case 'HSYNC_BOOTNODE_TOFU':
        case 'HSYNC_SERVER_TOFU':
        case 'HSYNC_AUTOTUNE':
        case 'HSYNC_P2P_PEER_DOWNLOAD':
        case 'HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES':
        case 'HSYNC_P2P_ALLOW_UNAUTHENTICATED':
        case 'HSYNC_ALLOW_DELETIONS':
        case 'HSYNC_COMPRESSION_ENABLED':
        case 'HSYNC_DELTA_ENABLED':
        case 'HSYNC_VERBOSE':
            if (!in_array($val, ['true', 'false'], true)) $val = 'false';
            break;
    }

    // POSIX single-quote escaping: embed '  as '\''
    $escaped = str_replace("'", "'\\''", $val);
    $lines[] = "{$key}='{$escaped}'";
}

// Write atomically: temp file → rename. The config dir and the resulting
// file must not be writable by group/other: hsync.env is sourced as root by
// start.sh, so a non-root-writable config would allow local privilege
// escalation. PHP runs as root here, so the rename keeps root ownership.
if (!is_dir(HSYNC_CONFIG)) {
    mkdir(HSYNC_CONFIG, 0750, true);
}
$tmp = tempnam(HSYNC_CONFIG, '.hsync.env.');
if ($tmp === false) {
    http_response_code(500);
    echo '{"error":"tmpfile"}';
    exit;
}
file_put_contents($tmp, implode("\n", $lines) . "\n");
chmod($tmp, 0640);
rename($tmp, HSYNC_ENV);
chmod(HSYNC_ENV, 0640);

echo '{"ok":true}';
