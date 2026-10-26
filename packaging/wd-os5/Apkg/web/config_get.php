<?php
/*
 * config_get.php — return the current hsync.env as a JSON object.
 *
 * PHP is executed by Apache via PHP-FPM (the same mechanism used by the stock
 * Transmission add-on's rpc.php), whereas CGI requires ExecCGI to be enabled
 * per-directory in Apache, which it is not for /var/www/apps/ on OS5.
 *
 * The web UI calls this on page load to populate the form.  Only the
 * whitelisted HSYNC_* keys are emitted; unknown lines in the .env file are
 * ignored.  A synthetic __VERSION__ key carries the package version so the
 * footer can display it without a separate round-trip.
 */

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

define('APKG_PATH',    '/shares/Volume_1/Nas_Prog/hsync');
define('HSYNC_ENV',    '/mnt/HD/HD_a2/hsync/config/hsync.env');
define('VERSION_FILE', APKG_PATH . '/VERSION');

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
    'HSYNC_INTERVAL', 'HSYNC_IGNORE_DELETE', 'HSYNC_COMPRESSION_ENABLED',
    'HSYNC_DELTA_ENABLED', 'HSYNC_DELTA_MIN_FILE_SIZE', 'HSYNC_DELTA_MIN_FILE_SIZE_P2P',
    'HSYNC_MAX_BW_UPLOAD', 'HSYNC_MAX_BW_DOWNLOAD', 'HSYNC_MIN_FREE_DISK',
    'HSYNC_MAX_INBOUND_SIZE_MB',
    'HSYNC_MASS_DELETE_GUARD', 'HSYNC_MASS_DELETE_THRESHOLD', 'HSYNC_MASS_DELETE_PERCENT',
    'HSYNC_HTTP_TIMEOUT', 'HSYNC_RECONNECT_DELAY', 'HSYNC_STABILITY_DELAY',
    'HSYNC_MAX_WAIT_TIMEOUT', 'HSYNC_TRANSFER_RETRY_MIN_WAIT', 'HSYNC_TRANSFER_RETRY_MAX_WAIT',
    'HSYNC_AUTOTUNE', 'HSYNC_MAX_CONCURRENT_TRANSFERS',
    'HSYNC_VERBOSE', 'HSYNC_QUIET', 'HSYNC_DRY_RUN',
];

/**
 * Parse a POSIX shell .env file and return an associative array.
 * Handles:  KEY=value  KEY='value'  KEY="value"  KEY='it'\''s'
 */
function parseEnvFile(string $path): array {
    $vars = [];
    if (!file_exists($path)) {
        return $vars;
    }
    foreach (file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
        $line = trim($line);
        if ($line === '' || $line[0] === '#') {
            continue;
        }
        if (!preg_match('/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/', $line, $m)) {
            continue;
        }
        $val = $m[2];
        // Strip surrounding single or double quotes and unescape internals.
        if (preg_match("/^'(.*)'$/s", $val, $q)) {
            $val = str_replace("'\\''", "'", $q[1]);   // POSIX single-quote escape
        } elseif (preg_match('/^"(.*)"$/s', $val, $q)) {
            $val = stripcslashes($q[1]);
        }
        $vars[$m[1]] = $val;
    }
    return $vars;
}

$cfg    = parseEnvFile(HSYNC_ENV);
$result = [];
foreach (KEYS as $k) {
    $result[$k] = $cfg[$k] ?? '';
}
$result['__VERSION__'] = file_exists(VERSION_FILE)
    ? trim(file_get_contents(VERSION_FILE))
    : 'unknown';

echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
