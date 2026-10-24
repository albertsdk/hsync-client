<?php
/*
 * hsyncignore_get.php — return the .hsyncignore file at the shared-folder root.
 *
 * The ignore list lives inside the synced folder, so the client picks up an
 * edit without a restart (it watches the file). Read-only: the path is fixed
 * and derived from HSYNC_PATH, never from user input.
 */

require __DIR__ . '/env_lib.php';

$target = hsync_data_path() . '/.hsyncignore';
$content = '';
if (is_file($target) && is_readable($target)) {
    $content = (string)file_get_contents($target, false, null, 0, HSYNC_MAX_TEXT);
}

hsync_reply(['ok' => true, 'content' => $content]);
