<?php
/*
 * hsyncignore_set.php — replace the .hsyncignore file at the shared-folder root.
 *
 * The destination is fixed and resolved from HSYNC_PATH; the content is written
 * atomically as a root-owned 0640 file so a local NAS user cannot inject rules.
 */

require __DIR__ . '/env_lib.php';

hsync_require_post();

$raw = file_get_contents('php://input');
if ($raw === false || strlen($raw) > 1048576) {
    hsync_reply(['ok' => false, 'error' => 'payload_size'], 413);
}
parse_str($raw, $post);

$content = (string)($post['content'] ?? '');
if (strlen($content) > HSYNC_MAX_TEXT) {
    hsync_reply(['ok' => false, 'error' => 'too_large'], 413);
}

$folder = hsync_data_path();
$realFolder = realpath($folder);
if ($realFolder === false || !is_dir($realFolder)) {
    hsync_reply(['ok' => false, 'error' => 'missing_folder'], 400);
}

$target = $realFolder . '/.hsyncignore';
if (!hsync_write_private($target, $content)) {
    hsync_reply(['ok' => false, 'error' => 'write_failed'], 500);
}

hsync_reply(['ok' => true]);
