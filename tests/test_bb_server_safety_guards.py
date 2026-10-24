# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for client pre-sync safety guards.

Drives the public CLI against the real server. Verifies the observable
abort/proceed behaviour of the disk-space and deletion-risk guards without
touching internals.

Requires HSYNC_BB_SERVER=1 to run.
"""

import os
from pathlib import Path

import pytest

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON)


class TestDiskSpaceGuard:
    def test_insufficient_disk_space_aborts(self, live_server, local_dir, run_cli):
        # Given a local dir with content and an impossibly high free-space requirement
        (Path(local_dir) / "a.txt").write_bytes(b"a")

        # When running with a free-space reserve outside the accepted range
        result = run_cli(
            "anybucket", local_dir, "--allow-deletions", "--min-free-disk-space", "1TB"
        )

        # Then the sync aborts (non-zero exit) without proceeding
        assert result.exit_code != 0


class TestMassDeleteGuard:
    """Server-mode behaviour for the --mass-delete-threshold safety guard.

    In server mode the mass-deletion guard CANNOT trip because local-only
    files produce ``upload`` actions (not ``delete``) and tombstone-confirmed
    deletes bypass the guard by design. The guard only produces unconfirmed
    ``delete`` actions in P2P ``download-only`` mode (slave mirrors master).

    The guard *logic* is unit-tested in ``test_sync_local.py::
    TestMassDeletionGuard``.  These BB tests verify the observable
    server-mode contract: the CLI flag is accepted, tombstone-confirmed
    deletes proceed unconditionally, and no spurious ``GUARD`` warning
    appears in the output.
    """

    def test_tombstone_confirmed_deletes_proceed_with_low_threshold(
        self, live_server, bb_bucket, local_dir, run_cli, bb_server_upload, bb_server_files
    ):
        """Tombstone-confirmed deletes bypass the guard even with threshold=1.

        When a file is deleted on the server (recording a tombstone), the
        client's next sync must remove the local copy unconditionally:
        the tombstone is positive proof the deletion is intentional, so
        the mass-deletion guard must never block it.
        """
        import requests

        # Upload a file to the server via API
        content = b"to be deleted\n"
        bb_server_upload(bb_bucket, "todel.txt", content)

        # Sync so the file reaches the local dir
        result = run_cli(bb_bucket, local_dir, "--allow-deletions", "--min-free-disk-space", "0")
        assert result.exit_code == 0
        assert (Path(local_dir) / "todel.txt").exists()

        # Delete the file on the server via API (records a tombstone)
        base_url, api_key = live_server
        r = requests.delete(
            f"{base_url}/api/v1/buckets/{bb_bucket}/files",
            data={"cid": "bbclient0001bbcltBB10", "filename": "todel.txt"},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
            verify=False,
        )
        assert r.status_code == 200

        # Sync again with --mass-delete-threshold 1
        # The tombstone-confirmed delete must proceed despite the low threshold
        result = run_cli(
            bb_bucket,
            local_dir,
            "--allow-deletions",
            "--min-free-disk-space",
            "0",
            "--mass-delete-threshold",
            "1",
        )
        assert result.exit_code == 0
        assert not (Path(local_dir) / "todel.txt").exists(), (
            "Tombstone-confirmed delete was blocked by the guard"
        )
        # No GUARD warning should appear (guard did not trip)
        assert "GUARD" not in result.output.upper(), (
            f"Guard tripped on tombstone-confirmed delete: {result.output}"
        )

    def test_threshold_flag_accepted_and_sync_completes(
        self, live_server, bb_bucket, local_dir, run_cli
    ):
        """The --mass-delete-threshold flag is accepted and sync completes."""
        (Path(local_dir) / "safe.txt").write_bytes(b"safe\n")
        result = run_cli(
            bb_bucket,
            local_dir,
            "--allow-deletions",
            "--min-free-disk-space",
            "0",
            "--mass-delete-threshold",
            "5",
        )
        assert result.exit_code == 0


class TestMassDeletePercentGuard:
    """Server-mode behaviour for the --mass-delete-percent safety guard.

    Same rationale as ``TestMassDeleteGuard``: the guard cannot trip in
    server mode because local-only files produce ``upload`` actions.
    Unit-tested in ``test_sync_local.py::TestMassDeletionGuard``.
    """

    def test_percent_flag_accepted_and_sync_completes(
        self, live_server, bb_bucket, local_dir, run_cli
    ):
        """The --mass-delete-percent flag is accepted and sync completes."""
        (Path(local_dir) / "safe.txt").write_bytes(b"safe\n")
        result = run_cli(
            bb_bucket,
            local_dir,
            "--allow-deletions",
            "--min-free-disk-space",
            "0",
            "--mass-delete-percent",
            "10",
        )
        assert result.exit_code == 0


class TestDeletionRiskGuard:
    def test_download_only_without_yes_aborts_in_noninteractive(
        self, live_server, local_dir, run_cli
    ):
        # Given a non-empty local dir and a download-only direction that could
        # delete local files, with no --allow-deletions confirmation
        (Path(local_dir) / "keep.txt").write_bytes(b"keep")

        # When running download-only without --allow-deletions
        result = run_cli("anybucket", local_dir, "--sync-direction", "download-only")

        # Then the guard aborts (no interactive terminal to confirm)
        assert result.exit_code != 0
        assert (Path(local_dir) / "keep.txt").exists()

    def test_download_only_with_yes_proceeds(self, live_server, bb_bucket, local_dir, run_cli):
        # Given a non-empty local dir and download-only direction, with --allow-deletions
        (Path(local_dir) / "keep.txt").write_bytes(b"keep")

        # When running download-only with --allow-deletions
        result = run_cli(
            bb_bucket,
            local_dir,
            "--sync-direction",
            "download-only",
            "--allow-deletions",
            "--min-free-disk-space",
            "0",
        )

        # Then the guard is bypassed and the sync proceeds
        assert result.exit_code == 0


class TestInstanceLock:
    """Two live clients must never sync the same local directory.

    The second run on an already-locked directory refuses to start with a
    clear, actionable error (see ``hsync_client.identity.InstanceLock``).
    """

    def test_second_process_on_same_directory_refused(
        self, live_server, bb_bucket, local_dir, watch_sync, run_cli, monkeypatch
    ):
        from tests.p2p_helpers import _terminate, _wait_for_client_ready

        # The in-process run must resolve the same locks dir as the watch
        # subprocess (the watch_sync fixture uses <tmp_path>/locks).
        monkeypatch.setenv("HSYNC_LOCKS_DIR", str(Path(local_dir).parent / "locks"))

        # Given a watch-mode client already syncing local_dir
        proc = watch_sync(bb_bucket, local_dir)
        try:
            ready = str(Path(local_dir).parent / "client.ready")
            assert _wait_for_client_ready(proc, ready), "watch client did not become ready"

            # When a second client targets the same directory
            result = run_cli(
                bb_bucket, local_dir, "--allow-deletions", "--min-free-disk-space", "0"
            )

            # Then it refuses to start: non-zero exit and a clear message
            # naming the conflicting directory.
            assert result.exit_code != 0
            assert "already syncing" in result.output
            assert str(Path(local_dir).resolve()) in result.output
        finally:
            _terminate(proc)
