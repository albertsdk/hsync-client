# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for update operations in sync module."""

import os
from unittest.mock import MagicMock, patch

import pytest
from hsync_common.models.file import File

from hsync_client.sync import update_dir, update_file


class TestUpdateDir:
    """Tests for update_dir function."""

    def test_creates_new_directory(self, tmp_path):
        """New directory is created."""
        data = {
            "path": "new_dir",
            "permissions": "755",
        }

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = update_dir(data)
            assert result is not None
            assert os.path.exists(tmp_path / "new_dir")
            assert os.path.isdir(tmp_path / "new_dir")

    def test_creates_nested_directory(self, tmp_path):
        """Nested directory is created."""
        data = {
            "path": "level1/level2/level3",
            "permissions": "755",
        }

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = update_dir(data)
            assert result is not None
            assert os.path.exists(tmp_path / "level1" / "level2" / "level3")

    def test_updates_permissions_on_existing_directory(self, tmp_path):
        """Permissions are updated on existing directory."""
        existing_dir = tmp_path / "existing"
        existing_dir.mkdir()
        os.chmod(existing_dir, 0o777)

        data = {
            "path": "existing",
            "permissions": "755",
        }

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = update_dir(data)
            assert result is not None
            # Check permissions were updated
            assert (os.stat(existing_dir).st_mode & 0o777) == 0o755


class TestUpdateFile:
    """Tests for update_file function."""

    def test_downloads_new_file(self, tmp_path):
        """New file triggers download."""
        data = {
            "filepath": "new_file.txt",
            "name": "new_file.txt",
            "path": "",
            "mtime": 1000,
            "ctime": 1000,
            "size": 100,
            "sha256sum": "abc123",
            "permissions": "644",
        }

        mock_download = MagicMock()
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.transfer.download_file", mock_download),
        ):
            update_file(data)
            mock_download.assert_called_once()

    def test_downloads_when_remote_dominates(self, tmp_path):
        """Remote is causally ahead (local at base) → download, no conflict copy.

        With a recorded merge base equal to the local content, the remote has
        moved on: the causal matrix resolves to ``download`` (dominance), so
        the local copy is overwritten silently — no conflict copy.
        """
        local_file = tmp_path / "file.txt"
        local_file.write_text("base content")
        os.utime(local_file, (1000, 1000))

        from hsync_common.hashing import compute_sha256

        local_sha = compute_sha256(str(local_file))

        # Record the merge base so the local side is "at base" and the remote
        # is causally ahead (dominance → download).
        from hsync_client import sync_state

        with patch(
            "hsync_client.sync_state.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "CID": "clientcid000000000000"},
        ):
            sync_state.record_base(str(local_file), local_sha)
            sync_state.flush()
            sync_state._stores.clear()  # force reload from disk in update_file

        data = {
            "filepath": "file.txt",
            "name": "file.txt",
            "path": "",
            "mtime": 2000,
            "ctime": 2000,
            "size": 11,
            "sha256sum": "different_sha256_hash",
            "permissions": "644",
        }

        mock_download = MagicMock()
        with (
            patch(
                "hsync_client.sync.CONFIG",
                {"LOCAL_BUCKET": str(tmp_path), "CID": "clientcid000000000000"},
            ),
            patch(
                "hsync_client.sync_state.CONFIG",
                {"LOCAL_BUCKET": str(tmp_path), "CID": "clientcid000000000000"},
            ),
            patch("hsync_client.transfer.download_file", mock_download),
        ):
            update_file(data)
            mock_download.assert_called_once()
            # Dominance: no conflict copy should be created.
            assert list(tmp_path.glob("file_conflicted_copy_*.txt")) == []

    def test_saves_conflict_copy_on_concurrent_edit(self, tmp_path):
        """A true causal divergence (both sides changed since base) preserves
        the local copy before overwriting when the remote wins the tie-break.

        Both local and remote moved on from the recorded base with different
        content and empty clocks: the tie-break is deterministic by hash. The
        remote hash is chosen to be lexicographically higher so it wins and the
        local copy is preserved as a conflicted copy.
        """
        local_file = tmp_path / "file.txt"
        local_file.write_text("local change")
        os.utime(local_file, (2000, 2000))

        from hsync_common.hashing import compute_sha256

        # Record a base that differs from both sides so both have "moved on".
        base_sha = "0" * 64
        from hsync_client import sync_state

        with patch(
            "hsync_client.sync_state.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "CID": "clientcid000000000000"},
        ):
            sync_state.record_base(str(local_file), base_sha)
            sync_state.flush()
            sync_state._stores.clear()  # force reload from disk in update_file

        local_sha = compute_sha256(str(local_file))
        # Remote hash lexicographically higher than local → remote wins.
        remote_sha = "f" * 64 if local_sha < "f" * 64 else "z" * 64

        data = {
            "filepath": "file.txt",
            "name": "file.txt",
            "path": "",
            "mtime": 2000,
            "ctime": 2000,
            "size": 12,
            "sha256sum": remote_sha,
            "permissions": "644",
            # Remote also moved on (concurrent edit) with its own clock tick.
            "version": {
                "vclock": {"remotecid000000000000": 1},
                "version_id": "abc",
                "origin_cid": "remotecid000000000000",
                "ts": 0,
            },
        }

        mock_download = MagicMock()
        with (
            patch(
                "hsync_client.sync.CONFIG",
                {"LOCAL_BUCKET": str(tmp_path), "CID": "clientcid000000000000"},
            ),
            patch(
                "hsync_client.sync_state.CONFIG",
                {"LOCAL_BUCKET": str(tmp_path), "CID": "clientcid000000000000"},
            ),
            patch("hsync_client.transfer.download_file", mock_download),
        ):
            update_file(data)
            mock_download.assert_called_once()
            conflict_copies = list(tmp_path.glob("file_conflicted_copy_*.txt"))
            assert len(conflict_copies) == 1
            assert conflict_copies[0].read_text() == "local change"

    def test_updates_permissions_when_only_permissions_differ(self, tmp_path):
        """Only permissions differ - updates without download."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("content")
        file_obj = File.get(str(local_file))
        assert file_obj is not None

        data = {
            "filepath": "file.txt",
            "name": "file.txt",
            "path": "",
            "mtime": file_obj.mtime,
            "ctime": file_obj.ctime,
            "size": file_obj.size,
            "sha256sum": file_obj.sha256sum,
            "permissions": "777",  # Different permissions
        }

        mock_set_perms = MagicMock()
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.set_permissions", mock_set_perms),
        ):
            update_file(data)
            mock_set_perms.assert_called_once()

    def test_skips_file_with_no_changes(self, tmp_path):
        """File with no changes is skipped."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("content")
        file_obj = File.get(str(local_file))
        assert file_obj is not None

        data = {
            "filepath": "file.txt",
            "name": "file.txt",
            "path": "",
            "mtime": file_obj.mtime,
            "ctime": file_obj.ctime,
            "size": file_obj.size,
            "sha256sum": file_obj.sha256sum,
            "permissions": file_obj.permissions,
        }

        # No download should occur - just check no exception is raised
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            update_file(data)  # Should complete without error


class TestSync:
    """Tests for sync function."""

    @pytest.mark.asyncio
    async def test_sync_downloads_remote_files(self, tmp_path):
        """sync downloads files from remote."""
        from hsync_client.sync import sync

        remote_files = [
            {
                "filepath": "remote.txt",
                "name": "remote.txt",
                "path": "",
                "mtime": 1000,
                "ctime": 1000,
                "size": 10,
                "sha256sum": "abc123",
                "permissions": "644",
            }
        ]

        config = {
            "LOCAL_BUCKET": str(tmp_path),
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get_files_remote", return_value=remote_files),
            patch("hsync_client.sync.get_tombstones_remote", return_value={}),
            patch("hsync_client.sync.push_tombstones_remote"),
            patch("hsync_client.sync.ensure_hsynckeep_files"),
            patch("hsync_client.sync.sync_state.flush"),
            patch("hsync_client.transfer.download_file") as mock_download,
            patch("hsync_client.transfer.upload_file") as mock_upload,
        ):
            await sync()
            mock_download.assert_called_once()
            mock_upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_uploads_local_files(self, tmp_path):
        """sync uploads local files not on remote."""
        from hsync_client.sync import sync

        # Create local file
        local_file = tmp_path / "local.txt"
        local_file.write_text("content")

        config = {
            "LOCAL_BUCKET": str(tmp_path),
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get_files_remote", return_value=[]),
            patch("hsync_client.sync.get_tombstones_remote", return_value={}),
            patch("hsync_client.sync.push_tombstones_remote"),
            patch("hsync_client.sync.sync_state.flush"),
            patch("hsync_client.transfer.download_file") as mock_download,
            patch("hsync_client.transfer.upload_file") as mock_upload,
        ):
            await sync()
            mock_download.assert_not_called()
            mock_upload.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_handles_auth_error(self, tmp_path):
        """sync logs authentication errors and re-raises so the CLI exits non-zero."""
        from hsync_client.exceptions import AuthenticationError
        from hsync_client.sync import sync

        config = {
            "LOCAL_BUCKET": str(tmp_path),
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch(
                "hsync_client.sync.get_files_remote",
                side_effect=AuthenticationError("Invalid API key"),
            ),
            patch("hsync_client.sync.is_debug", return_value=False),
        ):
            # The error is logged and then re-raised so the caller (CLI)
            # can exit non-zero instead of reporting success.
            with pytest.raises(AuthenticationError, match="Invalid API key"):
                await sync()

    @pytest.mark.asyncio
    async def test_sync_handles_network_error(self, tmp_path):
        """sync logs network errors and re-raises so the CLI exits non-zero."""
        from hsync_client.exceptions import NetworkError
        from hsync_client.sync import sync

        config = {
            "LOCAL_BUCKET": str(tmp_path),
        }

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch(
                "hsync_client.sync.get_files_remote", side_effect=NetworkError("Connection failed")
            ),
            patch("hsync_client.sync.is_debug", return_value=False),
        ):
            # The error is logged and then re-raised so the caller (CLI)
            # can exit non-zero instead of reporting success.
            with pytest.raises(NetworkError, match="Connection failed"):
                await sync()

    @pytest.mark.asyncio
    async def test_sync_handles_unexpected_error(self, tmp_path):
        """sync logs unexpected errors and re-raises so the CLI exits non-zero."""
        from hsync_client.sync import sync

        config = {
            "LOCAL_BUCKET": str(tmp_path),
        }

        class _Boom(Exception):
            """Sentinel for an unexpected failure inside sync."""

        with (
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.get_files_remote", side_effect=_Boom("Unexpected")),
            patch("hsync_client.sync.is_debug", return_value=False),
        ):
            # The error is logged and then re-raised so the caller (CLI)
            # can exit non-zero instead of reporting success.
            with pytest.raises(_Boom, match="Unexpected"):
                await sync()
