# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

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

        # When running with --min-free-disk-space far above what is available
        result = run_cli("anybucket", local_dir, "--yes", "--min-free-disk-space", "999999")

        # Then the sync aborts (non-zero exit) without proceeding
        assert result.exit_code != 0


class TestMassDeleteGuard:
    """Server-mode behaviour for the --mass-delete-threshold safety guard.

    In server mode the mass-deletion guard CANNOT trip because local-only
    files produce ``upload`` actions (not ``delete``) and tombstone-confirmed
    deletes bypass the guard by design. The guard only produces unconfirmed
    ``delete`` actions in LAN ``download-only`` mode (slave mirrors master).

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
        result = run_cli(bb_bucket, local_dir, "--yes", "--min-free-disk-space", "0")
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
            "--yes",
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
            "--yes",
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
            "--yes",
            "--min-free-disk-space",
            "0",
            "--mass-delete-percent",
            "10",
        )
        assert result.exit_code == 0


class TestMaxInboundGuard:
    def test_max_inbound_size_e2e_skip(self):
        """--max-inbound-size e2e requires multi-MB file transfers.

        Functionality verified by unit tests in test_security_inbound.py.
        """
        import pytest

        pytest.skip(
            "max-inbound-size e2e requires multi-MB file; functionality verified by unit tests"
        )


class TestDeletionRiskGuard:
    def test_download_only_without_yes_aborts_in_noninteractive(
        self, live_server, local_dir, run_cli
    ):
        # Given a non-empty local dir and a download-only direction that could
        # delete local files, with no --yes confirmation
        (Path(local_dir) / "keep.txt").write_bytes(b"keep")

        # When running download-only without --yes
        result = run_cli("anybucket", local_dir, "--sync-direction", "download-only")

        # Then the guard aborts (no interactive terminal to confirm)
        assert result.exit_code != 0
        assert (Path(local_dir) / "keep.txt").exists()

    def test_download_only_with_yes_proceeds(self, live_server, bb_bucket, local_dir, run_cli):
        # Given a non-empty local dir and download-only direction, with --yes
        (Path(local_dir) / "keep.txt").write_bytes(b"keep")

        # When running download-only with --yes
        result = run_cli(
            bb_bucket,
            local_dir,
            "--sync-direction",
            "download-only",
            "--yes",
            "--min-free-disk-space",
            "0",
        )

        # Then the guard is bypassed and the sync proceeds
        assert result.exit_code == 0
