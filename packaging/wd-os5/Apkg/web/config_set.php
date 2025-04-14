<?php
/*
 * config_set.php — write hsync.env from a URL-encoded POST body.
 *
 * Replaces config_set.php (see config_get.php for the rationale).
 * Accepts application/x-www-form-urlencoded only.  Each whitelisted HSYNC_*
 * key is written exactly once to a temporary file which is then renamed into
 * place atomically to avoid leaving the daemon with a half-written config.
 */

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

define('HSYNC_ENV',    '/mnt/HD/HD_a2/hsync/config/hsync.env');
define('HSYNC_CONFIG', '/mnt/HD/HD_a2/hsync/config');

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
if ($raw === false || strlen($raw) > 1048576) {
    http_response_code(413);
    echo '{"error":"payload_size"}';
    exit;
}
parse_str($raw, $post);

const KEYS = [
    'HSYNC_MODE', 'HSYNC_PATH', 'HSYNC_BUCKET', 'HSYNC_ROLE',
    'HSYNC_LAN_PERSISTENT', 'HSYNC_LAN_KEY', 'HSYNC_LAN_REQUIRE_PAIRING',
    'HSYNC_LAN_GROUP', 'HSYNC_DISCOVERY_TIMEOUT',
    'HSYNC_SERVER', 'HSYNC_API_KEY',
    'HSYNC_WATCH', 'HSYNC_INTERVAL', 'HSYNC_IGNORE_DELETE', 'HSYNC_NO_COMPRESSION',
    'HSYNC_DELTA_ENABLED', 'HSYNC_DELTA_MIN_FILE_SIZE', 'HSYNC_DELTA_MIN_FILE_SIZE_LAN',
    'HSYNC_MAX_BW_UPLOAD', 'HSYNC_MAX_BW_DOWNLOAD', 'HSYNC_MIN_FREE_DISK_MB',
    'HSYNC_MAX_INBOUND_SIZE_MB', 'HSYNC_VERBOSE',
    'HSYNC_AUTOTUNE', 'HSYNC_HTTP_TIMEOUT', 'HSYNC_RECONNECT_DELAY',
    'HSYNC_TRANSFER_RETRY_MIN_WAIT', 'HSYNC_TRANSFER_RETRY_MAX_WAIT',
];

$lines = ['# HSYNC configuration — written by config_set.php on ' . gmdate('Y-m-d\TH:i:s\Z')];

foreach (KEYS as $key) {
    $val = (string)($post[$key] ?? '');

    // Defensive validation for fields with strict grammars.
    switch ($key) {
        case 'HSYNC_MODE':
            if (!in_array($val, ['lan', 'server'], true)) $val = 'lan';
            break;
        case 'HSYNC_ROLE':
            if (!in_array($val, ['auto', 'master', 'slave', 'never-master'], true)) $val = 'auto';
            break;
        case 'HSYNC_BUCKET':
            if (!preg_match('/^[a-zA-Z0-9]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$/', $val)) $val = 'hsync';
            break;
        case 'HSYNC_DISCOVERY_TIMEOUT':
        case 'HSYNC_INTERVAL':
        case 'HSYNC_MIN_FREE_DISK_MB':
        case 'HSYNC_MAX_INBOUND_SIZE_MB':
        case 'HSYNC_DELTA_MIN_FILE_SIZE':
        case 'HSYNC_DELTA_MIN_FILE_SIZE_LAN':
        case 'HSYNC_HTTP_TIMEOUT':
        case 'HSYNC_RECONNECT_DELAY':
            if (!preg_match('/^[0-9]*$/', $val)) $val = '';
            break;
        case 'HSYNC_TRANSFER_RETRY_MIN_WAIT':
        case 'HSYNC_TRANSFER_RETRY_MAX_WAIT':
            if (!preg_match('/^[0-9]*\.?[0-9]*$/', $val)) $val = '';
            break;
        case 'HSYNC_AUTOTUNE':
        case 'HSYNC_LAN_PERSISTENT':
        case 'HSYNC_LAN_REQUIRE_PAIRING':
        case 'HSYNC_WATCH':
        case 'HSYNC_IGNORE_DELETE':
        case 'HSYNC_NO_COMPRESSION':
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
