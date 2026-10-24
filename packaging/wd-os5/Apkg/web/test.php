<?php
/*
 * test.php — check that a server or discovery server is reachable.
 *
 * Reading the peer certificate through ``hsync trust <kind> fingerprint`` proves
 * reachability and returns the SHA256 fingerprint in one round-trip, so the UI
 * never has to trust a second implementation of the TLS handshake.
 */

require __DIR__ . '/env_lib.php';

hsync_require_post();

$raw = file_get_contents('php://input');
if ($raw === false || strlen($raw) > 65536) {
    hsync_reply(['ok' => false, 'error' => 'payload_size'], 413);
}
parse_str($raw, $post);

$target = (string)($post['target'] ?? '');
$host = trim((string)($post['host'] ?? ''));

if (!in_array($target, ['server', 'bootnode'], true)) {
    hsync_reply(['ok' => false, 'error' => 'invalid_target'], 400);
}
$endpoint_re = '/^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$/';
if ($host === '' || !preg_match($endpoint_re, $host)) {
    hsync_reply(['ok' => false, 'error' => 'invalid_host'], 400);
}

[$ok, $out, $err] = hsync_run(['trust', $target, 'fingerprint', $host]);
if (!$ok) {
    hsync_reply(['ok' => false, 'error' => trim($err) !== '' ? trim($err) : 'unreachable'], 502);
}

hsync_reply(['ok' => true, 'fingerprint' => trim($out)]);
