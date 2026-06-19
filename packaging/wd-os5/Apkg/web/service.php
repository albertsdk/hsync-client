<?php
/*
 * service.php — start / stop / restart / status of the hsync daemon.
 *
 * Replaces service.php (see config_get.php for the rationale).
 *
 * Query: ?action=start|stop|restart|status
 * Returns: {"ok":true,"action":"...","running":true|false}
 */

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

define('APKG_PATH', '/shares/Volume_1/Nas_Prog/hsync');
define('HSYNC_PID', '/mnt/HD/HD_a2/hsync/run/hsync.pid');
define('HSYNC_BINARY', 'hsync');

/**
 * Check whether the hsync daemon is currently running.
 *
 * Strategy (fastest-first, no external forks required):
 * 1. Read the PID file.  If the pid directory exists in /proc → running.
 * 2. Fallback: use `pidof` via shell_exec (available on OS5).
 */
function isRunning(): bool {
    // --- 1. PID file ---
    if (is_readable(HSYNC_PID)) {
        $pid = trim((string)@file_get_contents(HSYNC_PID));
        if (ctype_digit($pid) && $pid !== '0') {
            return is_dir('/proc/' . $pid);
        }
    }
    // --- 2. pidof fallback ---
    if (function_exists('shell_exec')) {
        $out = @shell_exec('pidof ' . escapeshellarg(HSYNC_BINARY) . ' 2>/dev/null');
        return ($out !== null && trim($out) !== '');
    }
    return false;
}

/**
 * Run a lifecycle script.  Uses shell_exec when available, falls back to
 * proc_open/popen, so the page degrades gracefully on hardened php.ini.
 */
function runScript(string $script, bool $background): void {
    $cmd = escapeshellcmd($script) . ' ' . escapeshellarg(APKG_PATH) . ' >/dev/null 2>&1';
    if ($background) $cmd .= ' &';
    if (function_exists('shell_exec')) {
        @shell_exec($cmd);
    } elseif (function_exists('proc_open')) {
        $p = @proc_open(['/bin/sh', '-c', $cmd], [], $pipes);
        if (is_resource($p)) proc_close($p);
    } elseif (function_exists('popen')) {
        $h = @popen($cmd, 'r');
        if ($h) pclose($h);
    }
}

// Sanitize action: only lowercase letters allowed.
$action = preg_replace('/[^a-z]/', '', $_GET['action'] ?? '');

switch ($action) {
    case 'start':
        if (!isRunning()) runScript(APKG_PATH . '/start.sh', true);
        break;

    case 'stop':
        if (isRunning()) runScript(APKG_PATH . '/stop.sh', false);
        break;

    case 'restart':
        runScript(APKG_PATH . '/stop.sh',  false);
        runScript(APKG_PATH . '/start.sh', true);
        break;

    case 'status':
        // No action needed; fall through to the JSON reply below.
        break;

    default:
        http_response_code(400);
        echo '{"ok":false,"error":"invalid_action"}';
        exit;
}

echo json_encode(['ok' => true, 'action' => $action, 'running' => isRunning()]);
