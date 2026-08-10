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
    def test_mass_delete_guard_trips(self, live_server, bb_bucket, local_dir, run_cli):
        """--mass-delete-threshold 1 blocks deletion of all pre-existing files."""
        # Create 5 files locally, sync them to server
        for i in range(5):
            (Path(local_dir) / f"g_{i:02d}.txt").write_bytes(f"file{i}\n".encode())

        # Run first sync to upload files to server
        import os

        from hsync_client.config import get_env_settings

        old_key = os.environ.get("HSYNC_API_KEY")
        base_url, api_key = live_server
        os.environ["HSYNC_API_KEY"] = api_key
        get_env_settings.cache_clear()

        from typer.testing import CliRunner

        from hsync_client.cli import app

        runner = CliRunner()
        runner.invoke(
            app,
            [
                "-b",
                bb_bucket,
                "-s",
                base_url,
                "-p",
                local_dir,
                "--yes",
                "--insecure",
                "--min-free-disk-space",
                "0",
            ],
        )
        get_env_settings.cache_clear()

        # Now delete all files locally and re-sync with low threshold.
        # The local tombstone store must be cleared first: in server
        # (non-watch) mode local deletions would otherwise be recorded as
        # tombstones and reconciled as remote deletions, bypassing the guard
        # the test is about.
        # Both client-side stores must be cleared: the tombstones (otherwise
        # the deletions become remote deletions, bypassing the guard) and the
        # merge-base (otherwise the guard sees 0 local files and treats the
        # empty master as the suspect state, not the deletions).
        (Path(local_dir) / ".hsynctombstones").unlink(missing_ok=True)
        (Path(local_dir) / ".hsyncstate").unlink(missing_ok=True)
        from hsync_client import sync_state as _ss
        from hsync_client import tombstones as _ts

        _ts._stores.clear()
        _ss._stores.clear()
        for i in range(5):
            (Path(local_dir) / f"g_{i:02d}.txt").unlink()

        os.environ["HSYNC_API_KEY"] = api_key
        get_env_settings.cache_clear()
        result = runner.invoke(
            app,
            [
                "-b",
                bb_bucket,
                "-s",
                base_url,
                "-p",
                local_dir,
                "--yes",
                "--insecure",
                "--min-free-disk-space",
                "0",
                "--mass-delete-threshold",
                "1",
            ],
        )
        get_env_settings.cache_clear()

        if old_key:
            os.environ["HSYNC_API_KEY"] = old_key
        else:
            os.environ.pop("HSYNC_API_KEY", None)

        # The guard should have tripped (warning in output)
        assert "GUARD" in result.output.upper() or "mass" in result.output.lower(), (
            "Mass-deletion guard did not trip"
        )


class TestMassDeletePercentGuard:
    def test_mass_delete_percent_guard_trips(self, live_server, bb_bucket, local_dir, run_cli):
        """--mass-delete-percent 10 blocks when >10% of files are deleted."""
        # Create 10 files locally
        for i in range(10):
            (Path(local_dir) / f"h_{i:02d}.txt").write_bytes(f"file{i}\n".encode())

        # Upload to server first
        import os

        from typer.testing import CliRunner

        from hsync_client.cli import app
        from hsync_client.config import get_env_settings

        old_key = os.environ.get("HSYNC_API_KEY")
        base_url, api_key = live_server
        os.environ["HSYNC_API_KEY"] = api_key
        get_env_settings.cache_clear()

        runner = CliRunner()
        runner.invoke(
            app,
            [
                "-b",
                bb_bucket,
                "-s",
                base_url,
                "-p",
                local_dir,
                "--yes",
                "--insecure",
                "--min-free-disk-space",
                "0",
            ],
        )
        get_env_settings.cache_clear()

        # Delete 5 files (50%) — threshold is 10%, so guard should trip.
        # Clear the local tombstone store first: otherwise the deletions are
        # recorded as tombstones and reconciled as remote deletions, bypassing
        # the guard the test is about.
        # Both client-side stores must be cleared: the tombstones (otherwise
        # the deletions become remote deletions, bypassing the guard) and the
        # merge-base (otherwise the guard sees 0 local files and treats the
        # empty master as the suspect state, not the deletions).
        (Path(local_dir) / ".hsynctombstones").unlink(missing_ok=True)
        (Path(local_dir) / ".hsyncstate").unlink(missing_ok=True)
        from hsync_client import sync_state as _ss
        from hsync_client import tombstones as _ts

        _ts._stores.clear()
        _ss._stores.clear()
        for i in range(5):
            (Path(local_dir) / f"h_{i:02d}.txt").unlink()

        os.environ["HSYNC_API_KEY"] = api_key
        get_env_settings.cache_clear()
        result = runner.invoke(
            app,
            [
                "-b",
                bb_bucket,
                "-s",
                base_url,
                "-p",
                local_dir,
                "--yes",
                "--insecure",
                "--min-free-disk-space",
                "0",
                "--mass-delete-percent",
                "10",
            ],
        )
        get_env_settings.cache_clear()

        if old_key:
            os.environ["HSYNC_API_KEY"] = old_key
        else:
            os.environ.pop("HSYNC_API_KEY", None)

        # Guard should have tripped
        assert "GUARD" in result.output.upper() or "mass" in result.output.lower(), (
            "Mass-delete percent guard did not trip"
        )


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
