<?php
/*
 * log_get.php — stream the hsync daemon log to the web UI incrementally.
 *
 * Executed by Apache via PHP-FPM, like config_get.php / service.php. The web
 * UI polls this endpoint with the last byte offset it received; the response
 * carries only the bytes appended since then, so a fast poll feels real-time
 * without re-transferring the whole file.
 *
 * Query:  ?offset=<int>   byte offset already consumed by the client
 *                         (omitted / invalid -> start from the tail window)
 * Returns: {"ok":true,"offset":<next>,"size":<bytes>,"data":"<chunk>"}
 *
 * Read-only and path-fixed: the log location is a constant, so no user input
 * can influence which file is read. The per-poll payload is capped so a burst
 * of log activity cannot return megabytes at once. A shrunk file (rotation or
 * truncation) is detected (offset > size) and the client restarts from the
 * tail.
 */

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

define('HSYNC_LOG', '/mnt/HD/HD_a2/hsync/log/hsync.log');
const TAIL_BYTES = 65536;    // first-load window (last 64 KiB)
const MAX_CHUNK  = 262144;   // hard cap per poll (256 KiB)

$size = @filesize(HSYNC_LOG);
if ($size === false) {
    // Log not created yet (service never started). Not an error for the UI.
    echo json_encode(['ok' => true, 'offset' => 0, 'size' => 0, 'data' => '']);
    exit;
}

$param  = $_GET['offset'] ?? '';
$offset = ctype_digit((string) $param) ? (int) $param : -1;

// First load or a rotated/truncated file: (re)start from the tail window.
if ($offset < 0 || $offset > $size) {
    $offset = max(0, $size - TAIL_BYTES);
}

// Cap the delta so a write burst cannot return an unbounded chunk.
$length = $size - $offset;
if ($length > MAX_CHUNK) {
    $offset = $size - MAX_CHUNK;
    $length = MAX_CHUNK;
}

$data = '';
if ($length > 0 && ($fh = @fopen(HSYNC_LOG, 'rb')) !== false) {
    @fseek($fh, $offset);
    $data = (string) @fread($fh, $length);
    @fclose($fh);
}

echo json_encode(
    [
        'ok'     => true,
        'offset' => $offset + strlen($data),
        'size'   => $size,
        'data'   => $data,
    ],
    JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
);
