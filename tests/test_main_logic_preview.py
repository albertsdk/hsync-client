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
            "P2P_MODE": False,
        }

        with (
            patch("hsync_client.preview.CONFIG", config),
            patch("hsync_client.preview.ensure_hsynckeep_files"),
            patch("hsync_client.preview.get_files_local", return_value=[]),
            patch("hsync_client.preview.get_files_remote", return_value=[]),
            patch("hsync_client.preview.get_tombstones_remote", return_value={}),
            patch("hsync_client.preview.get_sync_actions", return_value=[]),
        ):
            _run_preview(mock_logger)

            # Should log preview start and end
            assert any("Preview" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_with_downloads(self, tmp_path):
        """Test preview showing files to download."""
        from hsync_common.models.file import File
        from hsync_common.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "P2P_MODE": False,
        }

        # Create a download action
        file_obj = File(str(tmp_path / "test.txt"))
        file_obj.size = 1024
        download_action = SyncAction(file_obj, SyncActionName.download)

        with (
            patch("hsync_client.preview.CONFIG", config),
            patch("hsync_client.preview.ensure_hsynckeep_files"),
            patch("hsync_client.preview.get_files_local", return_value=[]),
            patch("hsync_client.preview.get_files_remote", return_value=[]),
            patch("hsync_client.preview.get_tombstones_remote", return_value={}),
            patch("hsync_client.preview.get_sync_actions", return_value=[download_action]),
        ):
            _run_preview(mock_logger)

            # Should log download action
            assert any("TO DOWNLOAD" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_with_uploads(self, tmp_path):
        """Test preview showing files to upload."""
        from hsync_common.models.file import File
        from hsync_common.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "P2P_MODE": False,
        }

        # Create an upload action
        file_obj = File(str(tmp_path / "upload.txt"))
        file_obj.size = 2048
        upload_action = SyncAction(file_obj, SyncActionName.upload)

        with (
            patch("hsync_client.preview.CONFIG", config),
            patch("hsync_client.preview.ensure_hsynckeep_files"),
            patch("hsync_client.preview.get_files_local", return_value=[]),
            patch("hsync_client.preview.get_files_remote", return_value=[]),
            patch("hsync_client.preview.get_tombstones_remote", return_value={}),
            patch("hsync_client.preview.get_sync_actions", return_value=[upload_action]),
        ):
            _run_preview(mock_logger)

            # Should log upload action
            assert any("TO UPLOAD" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_with_deletes(self, tmp_path):
        """Test preview showing files to delete."""
        from hsync_common.models.file import File
        from hsync_common.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "P2P_MODE": False,
        }

        # Create a delete action
        file_obj = File(str(tmp_path / "delete.txt"))
        file_obj.size = 512
        delete_action = SyncAction(file_obj, SyncActionName.delete)

        with (
            patch("hsync_client.preview.CONFIG", config),
            patch("hsync_client.preview.ensure_hsynckeep_files"),
            patch("hsync_client.preview.get_files_local", return_value=[]),
            patch("hsync_client.preview.get_files_remote", return_value=[]),
            patch("hsync_client.preview.get_tombstones_remote", return_value={}),
            patch("hsync_client.preview.get_sync_actions", return_value=[delete_action]),
        ):
            _run_preview(mock_logger)

            # Should log delete action
            assert any("TO DELETE" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_with_synced_files(self, tmp_path):
        """Test preview showing files already in sync."""
        from hsync_common.models.file import File
        from hsync_common.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "P2P_MODE": False,
        }

        # Create a nothing action (already synced)
        file_obj = File(str(tmp_path / "synced.txt"))
        file_obj.size = 256
        synced_action = SyncAction(file_obj, SyncActionName.nothing)

        with (
            patch("hsync_client.preview.CONFIG", config),
            patch("hsync_client.preview.ensure_hsynckeep_files"),
            patch("hsync_client.preview.get_files_local", return_value=[]),
            patch("hsync_client.preview.get_files_remote", return_value=[]),
            patch("hsync_client.preview.get_tombstones_remote", return_value={}),
            patch("hsync_client.preview.get_sync_actions", return_value=[synced_action]),
        ):
            _run_preview(mock_logger)

            # Should log synced file
            assert any("SYNCED" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_detects_renames(self, tmp_path):
        """Test preview detecting rename operations."""
        from hsync_common.models.file import File
        from hsync_common.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "P2P_MODE": False,
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
            patch("hsync_client.preview.CONFIG", config),
            patch("hsync_client.preview.ensure_hsynckeep_files"),
            patch("hsync_client.preview.get_files_local", return_value=[]),
            patch("hsync_client.preview.get_files_remote", return_value=[]),
            patch("hsync_client.preview.get_tombstones_remote", return_value={}),
            patch(
                "hsync_client.preview.get_sync_actions",
                return_value=[delete_action, download_action],
            ),
        ):
            _run_preview(mock_logger)

            # Should log rename action
            assert any("TO RENAME" in str(call) for call in mock_logger.info.call_args_list)

    def test_preview_skips_sentinel_files(self, tmp_path):
        """Test preview skipping .hsynckeep sentinel files."""
        from hsync_common.models.file import File
        from hsync_common.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "P2P_MODE": False,
        }

        # Create action for sentinel file
        sentinel_file = File(str(tmp_path / ".hsynckeep"))
        sentinel_file.size = 0
        sentinel_action = SyncAction(sentinel_file, SyncActionName.upload)

        with (
            patch("hsync_client.preview.CONFIG", config),
            patch("hsync_client.preview.ensure_hsynckeep_files"),
            patch("hsync_client.preview.get_files_local", return_value=[]),
            patch("hsync_client.preview.get_files_remote", return_value=[]),
            patch("hsync_client.preview.get_tombstones_remote", return_value={}),
            patch("hsync_client.preview.get_sync_actions", return_value=[sentinel_action]),
        ):
            _run_preview(mock_logger)

            # Should not log sentinel file in upload list
            upload_calls = [
                str(call) for call in mock_logger.info.call_args_list if "TO UPLOAD" in str(call)
            ]
            assert len(upload_calls) == 0

    def test_preview_handles_exception(self):
        """Test preview handling exceptions gracefully."""
        mock_logger = MagicMock()

        config = {
            "P2P_MODE": False,
        }

        with (
            patch("hsync_client.preview.CONFIG", config),
            patch(
                "hsync_client.preview.ensure_hsynckeep_files",
                side_effect=Exception("Test error"),
            ),
        ):
            _run_preview(mock_logger)

            # Should log error
            mock_logger.error.assert_called()

    def test_preview_summary(self, tmp_path):
        """Test preview showing summary statistics."""
        from hsync_common.models.file import File
        from hsync_common.models.sync_action import SyncAction, SyncActionName

        mock_logger = MagicMock()

        config = {
            "P2P_MODE": False,
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
            patch("hsync_client.preview.CONFIG", config),
            patch("hsync_client.preview.ensure_hsynckeep_files"),
            patch("hsync_client.preview.get_files_local", return_value=[]),
            patch("hsync_client.preview.get_files_remote", return_value=[]),
            patch("hsync_client.preview.get_tombstones_remote", return_value={}),
            patch("hsync_client.preview.get_sync_actions", return_value=actions),
        ):
            _run_preview(mock_logger)

            # Should log the summary with the exact action counts.
            summary_calls = [
                str(call) for call in mock_logger.info.call_args_list if "Preview:" in str(call)
            ]
            assert len(summary_calls) == 1
            assert "1 synced, 1 downloads, 1 uploads, 0 deletes, 0 renames" in summary_calls[0]


class TestRunPreviewAuthErrors:
    """Authentication failures surface as ``auth_error`` (not a generic error)
    so the GUI can prompt for the shared password; other failures stay generic."""

    @staticmethod
    def _emitted(exc: Exception) -> list[tuple]:
        mock_logger = MagicMock()
        emitted: list[tuple] = []
        with (
            patch("hsync_client.preview.CONFIG", {"P2P_MODE": True}),
            patch("hsync_client.preview.ensure_hsynckeep_files"),
            patch("hsync_client.preview.get_files_local", return_value=[]),
            patch("hsync_client.preview.get_files_remote", side_effect=exc),
            patch("hsync_client.preview.get_tombstones_remote", return_value={}),
            patch("hsync_client.preview.get_sync_actions", return_value=[]),
            patch(
                "hsync_client.preview.emit_event",
                side_effect=lambda name, **kw: emitted.append((name, kw)),
            ),
        ):
            _run_preview(mock_logger)
        return emitted

    @staticmethod
    def _names(emitted: list[tuple]) -> list:
        return [name for name, _kw in emitted]

    def test_authentication_error_emits_auth_error(self):
        from hsync_client.events import UiEventName
        from hsync_client.exceptions import AuthenticationError

        names = self._names(self._emitted(AuthenticationError(message="Invalid API key")))

        assert UiEventName.auth_error in names
        assert UiEventName.error not in names

    def test_generic_error_stays_generic(self):
        from hsync_client.events import UiEventName

        names = self._names(self._emitted(RuntimeError("boom")))

        assert UiEventName.error in names
        assert UiEventName.auth_error not in names

    def test_connection_failure_is_tagged(self):
        """A connection-level failure is tagged so the GUI can name the cause."""
        from hsync_client.events import UiEventName
        from hsync_client.exceptions import NetworkError

        emitted = self._emitted(NetworkError("connection refused"))
        error_kw = next(kw for name, kw in emitted if name == UiEventName.error)

        assert error_kw["reason"] == "connection"

    def test_server_error_is_not_tagged_as_a_connection_failure(self):
        from hsync_client.events import UiEventName
        from hsync_client.exceptions import ServerError

        emitted = self._emitted(ServerError("500"))
        error_kw = next(kw for name, kw in emitted if name == UiEventName.error)

        assert error_kw["reason"] == ""
