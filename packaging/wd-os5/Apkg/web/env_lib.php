<?php
/*
 * env_lib.php — shared helpers for the hsync.env-backed endpoints.
 *
 * Runtime layout, the POSIX .env parser, the shared-folder resolver, the binary
 * runner and the JSON reply helper used by the configuration, ignore-list,
 * trust-store, probe and maintenance endpoints.
 */

const HSYNC_ROOT         = '/mnt/HD/HD_a2/hsync';
const HSYNC_ENV_FILE     = HSYNC_ROOT . '/config/hsync.env';
const HSYNC_CONFIG_DIR   = HSYNC_ROOT . '/config';
const HSYNC_STATE_DIR    = HSYNC_ROOT . '/state';
const HSYNC_PKG_DIR      = '/shares/Volume_1/Nas_Prog/hsync';
const HSYNC_BINARY       = HSYNC_PKG_DIR . '/bin/hsync/hsync';
const HSYNC_DEFAULT_DATA = HSYNC_ROOT . '/data';
const HSYNC_MAX_TEXT     = 262144;

function hsync_reply(array $payload, int $status = 200): void {
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function hsync_require_post(): void {
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
        hsync_reply(['ok' => false, 'error' => 'method_not_allowed'], 405);
    }
}

function hsync_parse_env(string $path): array {
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
        if (preg_match("/^'(.*)'$/s", $val, $q)) {
            $val = str_replace("'\\''", "'", $q[1]);
        } elseif (preg_match('/^"(.*)"$/s', $val, $q)) {
            $val = stripcslashes($q[1]);
        }
        $vars[$m[1]] = $val;
    }
    return $vars;
}

/** Absolute shared folder configured in hsync.env, never a traversal. */
function hsync_data_path(): string {
    $env = hsync_parse_env(HSYNC_ENV_FILE);
    $path = (string)($env['HSYNC_PATH'] ?? HSYNC_DEFAULT_DATA);
    if ($path === '' || $path[0] !== '/' || str_contains($path, '..')) {
        return HSYNC_DEFAULT_DATA;
    }
    return rtrim($path, '/');
}

/** Run the packaged binary with the state directory the service uses. */
function hsync_run(array $args): array {
    $env = hsync_parse_env(HSYNC_ENV_FILE);
    $command = 'HSYNC_STATE_DIR=' . escapeshellarg(HSYNC_STATE_DIR)
        . ' HSYNC_P2P_PASSPHRASE=' . escapeshellarg((string)($env['HSYNC_P2P_PASSPHRASE'] ?? ''))
        . ' ' . escapeshellarg(HSYNC_BINARY);
    foreach ($args as $arg) {
        $command .= ' ' . escapeshellarg((string)$arg);
    }
    $descriptors = [1 => ['pipe', 'w'], 2 => ['pipe', 'w']];
    $proc = proc_open($command, $descriptors, $pipes);
    if (!is_resource($proc)) {
        return [false, '', 'cannot_execute'];
    }
    $out = (string)stream_get_contents($pipes[1]);
    $err = (string)stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    $code = proc_close($proc);
    return [$code === 0, $out, $err];
}

/** Write $content to $target atomically with root-owned 0640 permissions. */
function hsync_write_private(string $target, string $content): bool {
    $dir = dirname($target);
    $tmp = tempnam($dir, '.' . basename($target) . '.');
    if ($tmp === false) {
        return false;
    }
    if (file_put_contents($tmp, $content) === false) {
        @unlink($tmp);
        return false;
    }
    chmod($tmp, 0640);
    if (!rename($tmp, $target)) {
        @unlink($tmp);
        return false;
    }
    chmod($target, 0640);
    return true;
}
