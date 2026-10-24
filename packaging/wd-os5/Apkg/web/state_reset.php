<?php
/*
 * state_reset.php — forget the local sync state of the shared folder.
 *
 * The state database is owned by the client and guarded by a per-folder lock,
 * so the operation runs through ``hsync state reset`` while the service is
 * stopped, then brings the service back up.
 */

require __DIR__ . '/env_lib.php';

hsync_require_post();

$folder = hsync_data_path();
if (!is_dir($folder)) {
    hsync_reply(['ok' => false, 'error' => 'missing_folder'], 400);
}

$pkg = escapeshellarg(HSYNC_PKG_DIR);
exec($pkg . '/stop.sh ' . $pkg . ' 2>&1', $stopOut, $stopCode);
[$ok, $out, $err] = hsync_run(['state', 'reset', '--folder', $folder, '--quarantine', '--json']);
exec($pkg . '/start.sh ' . $pkg . ' 2>&1', $startOut, $startCode);

if (!$ok) {
    hsync_reply(['ok' => false, 'error' => trim($err) !== '' ? trim($err) : 'reset_failed'], 502);
}
if ($startCode !== 0) {
    // The state is gone but the daemon did not come back: report it instead of
    // claiming success, so the operator restarts it from the UI.
    hsync_reply(['ok' => false, 'error' => 'restart_failed'], 500);
}

$report = json_decode($out, true);
hsync_reply(['ok' => true, 'report' => is_array($report) ? $report : null]);
