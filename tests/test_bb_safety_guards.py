# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for client pre-sync safety guards.

Drives the public CLI against the real server. Verifies the observable
abort/proceed behaviour of the disk-space and deletion-risk guards without
touching internals.
"""

from pathlib import Path


class TestDiskSpaceGuard:
    def test_insufficient_disk_space_aborts(self, live_server, local_dir, run_cli):
        # Given a local dir with content and an impossibly high free-space requirement
        (Path(local_dir) / "a.txt").write_bytes(b"a")

        # When running with --min-free-disk-space far above what is available
        result = run_cli("anybucket", local_dir, "--yes", "--min-free-disk-space", "999999")

        # Then the sync aborts (non-zero exit) without proceeding
        assert result.exit_code != 0


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
