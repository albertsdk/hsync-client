"""
Tests for sync module functions.
"""

import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from hsync_client.sync import (
    get_files_local,
    get_sync_actions,
    update_dir,
    update_file,
)
from hsync_client.models.file import File
from hsync_client.models.sync_action import SyncActionName


class TestGetFilesLocal:
    """Tests for get_files_local function."""

    def test_returns_empty_list_for_empty_directory(self, tmp_path):
        """Empty directory returns empty list."""
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_files_local()
            assert result == []

    def test_returns_files_in_directory(self, tmp_path):
        """Files in directory are returned."""
        # Create test files
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "file2.txt").write_text("content2")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_files_local()
            assert len(result) == 2
            filepaths = [f["filepath"] for f in result]
            assert any("file1.txt" in fp for fp in filepaths)
            assert any("file2.txt" in fp for fp in filepaths)

    def test_skips_hidden_files(self, tmp_path):
        """Hidden files are skipped."""
        (tmp_path / "visible.txt").write_text("visible")
        (tmp_path / ".hidden").write_text("hidden")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_files_local()
            assert len(result) == 1
            assert "visible.txt" in result[0]["filepath"]

    def test_skips_hidden_directories(self, tmp_path):
        """Hidden directories are skipped."""
        visible_dir = tmp_path / "visible"
        visible_dir.mkdir()
        (visible_dir / "file.txt").write_text("visible")

        hidden_dir = tmp_path / ".hidden"
        hidden_dir.mkdir()
        (hidden_dir / "file.txt").write_text("hidden")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_files_local()
            assert len(result) == 1
            assert "visible" in result[0]["filepath"]

    def test_handles_nested_directories(self, tmp_path):
        """Files in nested directories are returned."""
        nested = tmp_path / "level1" / "level2"
        nested.mkdir(parents=True)
        (nested / "deep_file.txt").write_text("deep content")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_files_local()
            assert len(result) == 1
            assert "deep_file.txt" in result[0]["filepath"]


class TestGetSyncActions:
    """Tests for get_sync_actions function."""

    def test_returns_empty_list_for_no_files(self):
        """No files returns empty list."""
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": "/tmp", "SYNC_ORDER": "mtime"}):
            result = get_sync_actions([], [])
            assert result == []

    def test_returns_empty_list_for_none_inputs(self):
        """None inputs returns empty list."""
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": "/tmp", "SYNC_ORDER": "mtime"}):
            result = get_sync_actions(None, None)
            assert result == []

    def test_download_action_for_remote_only_file(self, tmp_path):
        """File on remote only triggers download."""
        remote_files = [{
            "filepath": str(tmp_path / "remote_file.txt"),
            "name": "remote_file.txt",
            "mtime": 1000,
            "ctime": 1000,
            "size": 100,
            "md5sum": "abc123",
            "permissions": "-rw-r--r--",
        }]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_sync_actions(remote_files, [])
            assert len(result) == 1
            assert result[0].name == SyncActionName.download

    def test_upload_action_for_local_only_file(self, tmp_path):
        """File on local only triggers upload."""
        # Create local file
        local_file = tmp_path / "local_file.txt"
        local_file.write_text("local content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_sync_actions([], local_files)
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload

    def test_nothing_action_for_same_mtime(self, tmp_path):
        """Same mtime triggers no action."""
        local_file = tmp_path / "same_file.txt"
        local_file.write_text("content")
        mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()
        # Use relative filepath - the function will join with LOCAL_BUCKET
        remote_files = [{
            "filepath": "same_file.txt",
            "name": "same_file.txt",
            "mtime": mtime,
            "ctime": mtime,
            "size": 7,
            "md5sum": local_file_dict["md5sum"],
            "permissions": "-rw-r--r--",
        }]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.nothing

    def test_download_action_for_newer_remote(self, tmp_path):
        """Newer remote file triggers download."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("content")
        local_mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()
        remote_files = [{
            "filepath": "file.txt",
            "name": "file.txt",
            "mtime": local_mtime + 1000,  # Remote is newer
            "ctime": local_mtime + 1000,
            "size": 7,
            "md5sum": "different_md5",
            "permissions": "-rw-r--r--",
        }]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.download

    def test_upload_action_for_newer_local(self, tmp_path):
        """Newer local file triggers upload."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("content")
        local_mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()
        remote_files = [{
            "filepath": "file.txt",
            "name": "file.txt",
            "mtime": local_mtime - 1000,  # Remote is older
            "ctime": local_mtime - 1000,
            "size": 7,
            "md5sum": "different_md5",
            "permissions": "-rw-r--r--",
        }]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "SYNC_ORDER": "mtime"}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload


class TestUpdateDir:
    """Tests for update_dir function."""

    def test_creates_new_directory(self, tmp_path):
        """New directory is created."""
        data = {
            "path": "new_dir",
            "permissions": "drwxr-xr-x",
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
            "permissions": "drwxr-xr-x",
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
            "permissions": "drwxr-xr-x",
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
            "md5sum": "abc123",
            "permissions": "-rw-r--r--",
        }

        mock_download = MagicMock()
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}), \
             patch("hsync_client.transfer.download_file", mock_download):
            update_file(data)
            mock_download.assert_called_once()

    def test_downloads_file_with_different_md5(self, tmp_path):
        """File with different MD5 triggers download."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("old content")

        data = {
            "filepath": "file.txt",
            "name": "file.txt",
            "path": "",
            "mtime": 2000,
            "ctime": 2000,
            "size": 11,
            "md5sum": "different_md5_hash",
            "permissions": "-rw-r--r--",
        }

        mock_download = MagicMock()
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}), \
             patch("hsync_client.transfer.download_file", mock_download):
            update_file(data)
            mock_download.assert_called_once()

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
            "md5sum": file_obj.md5sum,
            "permissions": "-rwxrwxrwx",  # Different permissions
        }

        mock_set_perms = MagicMock()
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}), \
             patch("hsync_client.sync.set_permissions", mock_set_perms):
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
            "md5sum": file_obj.md5sum,
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
                "md5sum": "abc123",
                "permissions": "-rw-r--r--",
            }
        ]

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_ORDER": "mtime",
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get_files_remote", return_value=remote_files), \
             patch("hsync_client.transfer.download_file") as mock_download, \
             patch("hsync_client.transfer.upload_file") as mock_upload:
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
            "SYNC_ORDER": "mtime",
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get_files_remote", return_value=[]), \
             patch("hsync_client.transfer.download_file") as mock_download, \
             patch("hsync_client.transfer.upload_file") as mock_upload:
            await sync()
            mock_download.assert_not_called()
            mock_upload.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_handles_auth_error(self, tmp_path):
        """sync handles authentication errors gracefully."""
        from hsync_client.sync import sync
        from hsync_client.exceptions import AuthenticationError

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_ORDER": "mtime",
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get_files_remote", side_effect=AuthenticationError("Invalid API key")), \
             patch("hsync_client.sync.is_debug", return_value=False):
            # Should not raise exception
            await sync()

    @pytest.mark.asyncio
    async def test_sync_handles_network_error(self, tmp_path):
        """sync handles network errors gracefully."""
        from hsync_client.sync import sync
        from hsync_client.exceptions import NetworkError

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_ORDER": "mtime",
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get_files_remote", side_effect=NetworkError("Connection failed")), \
             patch("hsync_client.sync.is_debug", return_value=False):
            # Should not raise exception
            await sync()

    @pytest.mark.asyncio
    async def test_sync_handles_unexpected_error(self, tmp_path):
        """sync handles unexpected errors gracefully."""
        from hsync_client.sync import sync

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_ORDER": "mtime",
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get_files_remote", side_effect=Exception("Unexpected")), \
             patch("hsync_client.sync.is_debug", return_value=False):
            # Should not raise exception
            await sync()


class TestGetFilesRemote:
    """Tests for get_files_remote function."""

    def test_get_files_remote_success(self):
        """get_files_remote returns file list on success."""
        from hsync_client.sync import get_files_remote

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"filepath": "file.txt"}]

        config = {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "testbucket",
            "SYNC_ORDER": "mtime",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "NO_VERIFY_SSL": True,
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get", return_value=mock_response), \
             patch("hsync_client.sync.get_ssl_verify", return_value=False):
            result = get_files_remote()
            assert result == [{"filepath": "file.txt"}]

    def test_get_files_remote_with_filter_path(self):
        """get_files_remote includes filter path in request."""
        from hsync_client.sync import get_files_remote

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []

        config = {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "testbucket",
            "SYNC_ORDER": "mtime",
            "FILTER_PATH": "subdir",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "NO_VERIFY_SSL": True,
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get", return_value=mock_response) as mock_get, \
             patch("hsync_client.sync.get_ssl_verify", return_value=False):
            get_files_remote()
            # Check that path parameter was included
            call_kwargs = mock_get.call_args[1]
            assert call_kwargs["params"]["path"] == "subdir"

    def test_get_files_remote_401_raises_auth_error(self):
        """get_files_remote raises AuthenticationError on 401."""
        from hsync_client.sync import get_files_remote
        from hsync_client.exceptions import AuthenticationError

        mock_response = MagicMock()
        mock_response.status_code = 401

        config = {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "testbucket",
            "SYNC_ORDER": "mtime",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "NO_VERIFY_SSL": True,
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get", return_value=mock_response), \
             patch("hsync_client.sync.get_ssl_verify", return_value=False):
            with pytest.raises(AuthenticationError):
                get_files_remote()

    def test_get_files_remote_403_raises_auth_error(self):
        """get_files_remote raises AuthenticationError on 403."""
        from hsync_client.sync import get_files_remote
        from hsync_client.exceptions import AuthenticationError

        mock_response = MagicMock()
        mock_response.status_code = 403

        config = {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "testbucket",
            "SYNC_ORDER": "mtime",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "NO_VERIFY_SSL": True,
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get", return_value=mock_response), \
             patch("hsync_client.sync.get_ssl_verify", return_value=False):
            with pytest.raises(AuthenticationError):
                get_files_remote()

    def test_get_files_remote_500_raises_server_error(self):
        """get_files_remote raises ServerError on 500."""
        from hsync_client.sync import get_files_remote
        from hsync_client.exceptions import ServerError

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        config = {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "testbucket",
            "SYNC_ORDER": "mtime",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "NO_VERIFY_SSL": True,
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get", return_value=mock_response), \
             patch("hsync_client.sync.get_ssl_verify", return_value=False):
            with pytest.raises(ServerError):
                get_files_remote()

    def test_get_files_remote_network_error(self):
        """get_files_remote raises NetworkError on connection failure."""
        from hsync_client.sync import get_files_remote
        from hsync_client.exceptions import NetworkError
        from requests.exceptions import RequestException

        config = {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "testbucket",
            "SYNC_ORDER": "mtime",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "NO_VERIFY_SSL": True,
        }

        with patch("hsync_client.sync.CONFIG", config), \
             patch("hsync_client.sync.get", side_effect=RequestException("Connection failed")), \
             patch("hsync_client.sync.get_ssl_verify", return_value=False):
            with pytest.raises(NetworkError):
                get_files_remote()


class TestExecuteParallelTransfers:
    """Tests for _execute_parallel_transfers function."""

    @pytest.mark.asyncio
    async def test_executes_downloads_in_parallel(self):
        """Downloads are executed in parallel."""
        from hsync_client.sync import _execute_parallel_transfers

        files = [
            File(filepath="/path/file1.txt"),
            File(filepath="/path/file2.txt"),
            File(filepath="/path/file3.txt"),
        ]

        download_calls = []

        def mock_download(file):
            download_calls.append(file.filepath)

        # Patch in transfer module (local import in _execute_parallel_transfers)
        with patch("hsync_client.transfer.download_file", mock_download):
            await _execute_parallel_transfers(files, [])

        assert len(download_calls) == 3
        assert "/path/file1.txt" in download_calls
        assert "/path/file2.txt" in download_calls
        assert "/path/file3.txt" in download_calls

    @pytest.mark.asyncio
    async def test_executes_uploads_in_parallel(self):
        """Uploads are executed in parallel."""
        from hsync_client.sync import _execute_parallel_transfers

        files = [
            File(filepath="/path/file1.txt"),
            File(filepath="/path/file2.txt"),
        ]

        upload_calls = []

        def mock_upload(file):
            upload_calls.append(file.filepath)

        # Patch in transfer module (local import in _execute_parallel_transfers)
        with patch("hsync_client.transfer.upload_file", mock_upload):
            await _execute_parallel_transfers([], files)

        assert len(upload_calls) == 2
        assert "/path/file1.txt" in upload_calls
        assert "/path/file2.txt" in upload_calls

    @pytest.mark.asyncio
    async def test_executes_mixed_transfers(self):
        """Downloads and uploads are executed together."""
        from hsync_client.sync import _execute_parallel_transfers

        downloads = [
            File(filepath="/path/download.txt"),
        ]
        uploads = [
            File(filepath="/path/upload.txt"),
        ]

        download_calls = []
        upload_calls = []

        def mock_download(file):
            download_calls.append(file.filepath)

        def mock_upload(file):
            upload_calls.append(file.filepath)

        # Patch in transfer module (local import in _execute_parallel_transfers)
        with patch("hsync_client.transfer.download_file", mock_download), \
             patch("hsync_client.transfer.upload_file", mock_upload):
            await _execute_parallel_transfers(downloads, uploads)

        assert len(download_calls) == 1
        assert len(upload_calls) == 1

    @pytest.mark.asyncio
    async def test_handles_transfer_errors_gracefully(self):
        """Transfer errors are logged but don't stop other transfers."""
        from hsync_client.sync import _execute_parallel_transfers

        files = [
            File(filepath="/path/file1.txt"),
            File(filepath="/path/file2.txt"),
        ]

        call_count = 0

        def mock_download(file):
            nonlocal call_count
            call_count += 1
            if file.filepath == "/path/file1.txt":
                raise Exception("Download failed")

        # Patch in transfer module (local import in _execute_parallel_transfers)
        with patch("hsync_client.transfer.download_file", mock_download):
            # Should not raise exception
            await _execute_parallel_transfers(files, [])

        # Both files should have been attempted
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_empty_lists_do_nothing(self):
        """Empty download and upload lists complete without error."""
        from hsync_client.sync import _execute_parallel_transfers

        # Should complete without error
        await _execute_parallel_transfers([], [])
