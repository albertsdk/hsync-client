"""
Tests for hsync_client.main_logic module - Preview functionality.

Contains TestRunPreview class.
"""

from unittest.mock import MagicMock, patch

from hsync_client.main_logic import _run_preview


class TestRunPreview:
    """Tests for _run_preview function."""

    def test_preview_with_no_files(self):
        """Test preview with no files to sync."""
        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        with (
            patch("hsync_client.main_logic.CONFIG", config),
            patch("hsync_client.main_logic.ensure_hsynckeep_files"),
            patch("hsync_client.main_logic.get_files_local", return_value=[]),
            patch("hsync_client.main_logic.get_files_remote", return_value=[]),
            patch("hsync_client.main_logic.get_tombstones_remote", return_value={}),
            patch("hsync_client.main_logic.get_sync_actions", return_value=[]),
        ):
            _run_preview(mock_logger)

            # Should log preview start and end
            assert any("Preview" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_with_downloads(self, tmp_path):
        """Test preview showing files to download."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create a download action
        file_obj = File(str(tmp_path / "test.txt"))
        file_obj.size = 1024
        download_action = SyncAction(file_obj, SyncActionName.download)

        with (
            patch("hsync_client.main_logic.CONFIG", config),
            patch("hsync_client.main_logic.ensure_hsynckeep_files"),
            patch("hsync_client.main_logic.get_files_local", return_value=[]),
            patch("hsync_client.main_logic.get_files_remote", return_value=[]),
            patch("hsync_client.main_logic.get_tombstones_remote", return_value={}),
            patch("hsync_client.main_logic.get_sync_actions", return_value=[download_action]),
        ):
            _run_preview(mock_logger)

            # Should log download action
            assert any("TO_DOWNLOAD" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_with_uploads(self, tmp_path):
        """Test preview showing files to upload."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create an upload action
        file_obj = File(str(tmp_path / "upload.txt"))
        file_obj.size = 2048
        upload_action = SyncAction(file_obj, SyncActionName.upload)

        with (
            patch("hsync_client.main_logic.CONFIG", config),
            patch("hsync_client.main_logic.ensure_hsynckeep_files"),
            patch("hsync_client.main_logic.get_files_local", return_value=[]),
            patch("hsync_client.main_logic.get_files_remote", return_value=[]),
            patch("hsync_client.main_logic.get_tombstones_remote", return_value={}),
            patch("hsync_client.main_logic.get_sync_actions", return_value=[upload_action]),
        ):
            _run_preview(mock_logger)

            # Should log upload action
            assert any("TO_UPLOAD" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_with_deletes(self, tmp_path):
        """Test preview showing files to delete."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create a delete action
        file_obj = File(str(tmp_path / "delete.txt"))
        file_obj.size = 512
        delete_action = SyncAction(file_obj, SyncActionName.delete)

        with (
            patch("hsync_client.main_logic.CONFIG", config),
            patch("hsync_client.main_logic.ensure_hsynckeep_files"),
            patch("hsync_client.main_logic.get_files_local", return_value=[]),
            patch("hsync_client.main_logic.get_files_remote", return_value=[]),
            patch("hsync_client.main_logic.get_tombstones_remote", return_value={}),
            patch("hsync_client.main_logic.get_sync_actions", return_value=[delete_action]),
        ):
            _run_preview(mock_logger)

            # Should log delete action
            assert any("TO_DELETE" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_with_synced_files(self, tmp_path):
        """Test preview showing files already in sync."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create a nothing action (already synced)
        file_obj = File(str(tmp_path / "synced.txt"))
        file_obj.size = 256
        synced_action = SyncAction(file_obj, SyncActionName.nothing)

        with (
            patch("hsync_client.main_logic.CONFIG", config),
            patch("hsync_client.main_logic.ensure_hsynckeep_files"),
            patch("hsync_client.main_logic.get_files_local", return_value=[]),
            patch("hsync_client.main_logic.get_files_remote", return_value=[]),
            patch("hsync_client.main_logic.get_tombstones_remote", return_value={}),
            patch("hsync_client.main_logic.get_sync_actions", return_value=[synced_action]),
        ):
            _run_preview(mock_logger)

            # Should log synced file
            assert any("SYNCED" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_detects_renames(self, tmp_path):
        """Test preview detecting rename operations."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create delete and download actions with same sha256sum (rename)
        old_file = File(str(tmp_path / "old.txt"))
        old_file.size = 1024
        old_file.sha256sum = "abc123"
        delete_action = SyncAction(old_file, SyncActionName.delete)

        new_file = File(str(tmp_path / "new.txt"))
        new_file.size = 1024
        new_file.sha256sum = "abc123"
        download_action = SyncAction(new_file, SyncActionName.download)

        with (
            patch("hsync_client.main_logic.CONFIG", config),
            patch("hsync_client.main_logic.ensure_hsynckeep_files"),
            patch("hsync_client.main_logic.get_files_local", return_value=[]),
            patch("hsync_client.main_logic.get_files_remote", return_value=[]),
            patch("hsync_client.main_logic.get_tombstones_remote", return_value={}),
            patch(
                "hsync_client.main_logic.get_sync_actions",
                return_value=[delete_action, download_action],
            ),
        ):
            _run_preview(mock_logger)

            # Should log rename action
            assert any("TO_RENAME" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_skips_sentinel_files(self, tmp_path):
        """Test preview skipping .hsynckeep sentinel files."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create action for sentinel file
        sentinel_file = File(str(tmp_path / ".hsynckeep"))
        sentinel_file.size = 0
        sentinel_action = SyncAction(sentinel_file, SyncActionName.upload)

        with (
            patch("hsync_client.main_logic.CONFIG", config),
            patch("hsync_client.main_logic.ensure_hsynckeep_files"),
            patch("hsync_client.main_logic.get_files_local", return_value=[]),
            patch("hsync_client.main_logic.get_files_remote", return_value=[]),
            patch("hsync_client.main_logic.get_tombstones_remote", return_value={}),
            patch("hsync_client.main_logic.get_sync_actions", return_value=[sentinel_action]),
        ):
            _run_preview(mock_logger)

            # Should not log sentinel file in upload list
            upload_calls = [
                str(call) for call in mock_logger.info.call_args_list if "TO_UPLOAD" in str(call)
            ]
            assert len(upload_calls) == 0

    def test_preview_handles_exception(self):
        """Test preview handling exceptions gracefully."""
        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        with (
            patch("hsync_client.main_logic.CONFIG", config),
            patch(
                "hsync_client.main_logic.ensure_hsynckeep_files",
                side_effect=Exception("Test error"),
            ),
        ):
            _run_preview(mock_logger)

            # Should log error
            mock_logger.error.assert_called()

    def test_preview_summary(self, tmp_path):
        """Test preview showing summary statistics."""
        from hsync_client.models.file import File
        from hsync_client.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "LAN_MODE": False,
        }

        # Create various actions
        actions = [
            SyncAction(File(str(tmp_path / "dl.txt")), SyncActionName.download),
            SyncAction(File(str(tmp_path / "ul.txt")), SyncActionName.upload),
            SyncAction(File(str(tmp_path / "sync.txt")), SyncActionName.nothing),
        ]

        for action in actions:
            action.file.size = 100

        with (
            patch("hsync_client.main_logic.CONFIG", config),
            patch("hsync_client.main_logic.ensure_hsynckeep_files"),
            patch("hsync_client.main_logic.get_files_local", return_value=[]),
            patch("hsync_client.main_logic.get_files_remote", return_value=[]),
            patch("hsync_client.main_logic.get_tombstones_remote", return_value={}),
            patch("hsync_client.main_logic.get_sync_actions", return_value=actions),
        ):
            _run_preview(mock_logger)

            # Should log summary with counts
            summary_calls = [
                str(call) for call in mock_logger.info.call_args_list if "Preview:" in str(call)
            ]
            assert len(summary_calls) > 0
