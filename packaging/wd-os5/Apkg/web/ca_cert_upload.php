<?php
/*
 * ca_cert_upload.php — store a PEM certificate authority file for server mode.
 *
 * The uploaded file replaces config/ca.pem, which is the path the form keeps in
 * HSYNC_CA_CERT and start.sh passes to --ca-cert. Only a PEM bundle with at
 * least one certificate block is accepted.
 */

require __DIR__ . '/env_lib.php';

hsync_require_post();

if (!isset($_FILES['file']) || !is_uploaded_file($_FILES['file']['tmp_name'])) {
    hsync_reply(['ok' => false, 'error' => 'missing_file'], 400);
}

$file = $_FILES['file'];
$size = (int)($file['size'] ?? 0);
if ($size <= 0 || $size > 65536) {
    hsync_reply(['ok' => false, 'error' => 'bad_size'], 413);
}

$ext = strtolower(pathinfo((string)($file['name'] ?? ''), PATHINFO_EXTENSION));
if (!in_array($ext, ['pem', 'crt', 'cer'], true)) {
    hsync_reply(['ok' => false, 'error' => 'bad_extension'], 415);
}

$content = (string)file_get_contents($file['tmp_name']);
if (substr_count($content, '-----BEGIN CERTIFICATE-----') < 1
    || !str_contains($content, '-----END CERTIFICATE-----')) {
    hsync_reply(['ok' => false, 'error' => 'not_pem'], 415);
}

if (!is_dir(HSYNC_CONFIG_DIR)) {
    mkdir(HSYNC_CONFIG_DIR, 0750, true);
}

$target = HSYNC_CONFIG_DIR . '/ca.pem';
if (!hsync_write_private($target, $content)) {
    hsync_reply(['ok' => false, 'error' => 'write_failed'], 500);
}

hsync_reply(['ok' => true, 'path' => $target]);
