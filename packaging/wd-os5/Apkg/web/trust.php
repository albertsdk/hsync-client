<?php
/*
 * trust.php — list, add and remove pinned certificates.
 *
 * The trust stores belong to the sync engine, so every operation is delegated
 * to the packaged binary (``hsync trust <kind> <action>``) with the same
 * HSYNC_STATE_DIR the service uses; this endpoint never rewrites the stores.
 */

require __DIR__ . '/env_lib.php';

hsync_require_post();

$raw = file_get_contents('php://input');
if ($raw === false || strlen($raw) > 65536) {
    hsync_reply(['ok' => false, 'error' => 'payload_size'], 413);
}
parse_str($raw, $post);

$action = (string)($post['action'] ?? 'list');
$kind = (string)($post['kind'] ?? 'bootnode');
$host = trim((string)($post['host'] ?? ''));
$fingerprint = trim((string)($post['fingerprint'] ?? ''));

if (!in_array($kind, ['bootnode', 'server', 'peer'], true)) {
    hsync_reply(['ok' => false, 'error' => 'invalid_kind'], 400);
}
if (!in_array($action, ['list', 'add', 'remove'], true)) {
    hsync_reply(['ok' => false, 'error' => 'invalid_action'], 400);
}

if ($action === 'list') {
    [$ok, $out, $err] = hsync_run(['trust', $kind, 'list', '--json']);
    if (!$ok) {
        hsync_reply(['ok' => false, 'error' => trim($err) !== '' ? trim($err) : 'list_failed'], 502);
    }
    $entries = json_decode($out, true);
    hsync_reply(['ok' => true, 'entries' => is_array($entries) ? $entries : []]);
}

$endpoint_re = '/^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$/';
if ($host === '' || !preg_match($endpoint_re, $host)) {
    hsync_reply(['ok' => false, 'error' => 'invalid_host'], 400);
}
if ($action === 'add' && $fingerprint === '') {
    hsync_reply(['ok' => false, 'error' => 'missing_fingerprint'], 400);
}

$args = ['trust', $kind, $action];
if ($kind === 'peer') {
    $env = hsync_parse_env(HSYNC_ENV_FILE);
    $bucket = (string)($env['HSYNC_BUCKET'] ?? '');
    if ($bucket === '') {
        hsync_reply(['ok' => false, 'error' => 'missing_bucket'], 400);
    }
    $args[] = '--bucket';
    $args[] = $bucket;
}
$args[] = $host;
if ($action === 'add') {
    $args[] = $fingerprint;
}

[$ok, , $err] = hsync_run($args);
if (!$ok) {
    hsync_reply(['ok' => false, 'error' => trim($err) !== '' ? trim($err) : 'operation_failed'], 502);
}

hsync_reply(['ok' => true]);
