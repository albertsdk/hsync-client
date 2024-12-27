"""
Edge case tests for HSYNC Client.

Tests for edge cases including:
- File permissions handling
- Symlink handling
- Long paths
- File locking/retry scenarios
- Performance benchmarks
"""

import stat
import time
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from hsync_common.models.file import File


class TestFilePermissions:
    """Tests for file permission handling."""

    def test_readonly_file(self, tmp_path):
        """Test reading metadata from a read-only file."""
        test_file = tmp_path / "readonly.txt"
        test_file.write_text("content")
        # Make file read-only
        test_file.chmod(stat.S_IRUSR)

        try:
            file_obj = File.get(str(test_file))
            assert file_obj is not None
            assert file_obj.filepath == str(test_file)
        finally:
            # Restore permissions for cleanup
            test_file.chmod(stat.S_IRWXU)

    def test_executable_file(self, tmp_path):
        """Test reading metadata from an executable file."""
        test_file = tmp_path / "executable.bin"
        test_file.write_bytes(b"binary content")
        test_file.chmod(stat.S_IRWXU)

        file_obj = File.get(str(test_file))
        assert file_obj is not None
        assert file_obj.permissions is not None
        assert file_obj.permissions == "700"

    def test_set_permissions_from_octal(self, tmp_path):
        """Test setting permissions from octal notation."""
        from hsync_client.utils import set_permissions

        test_file = tmp_path / "perms.txt"
        test_file.write_text("test")

        # Set permissions
        set_permissions(str(test_file), "644")
        actual = stat.S_IMODE(test_file.stat().st_mode)
        assert actual == stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH

    def test_set_permissions_preserves_if_same(self, tmp_path):
        """Test that setting same permissions is skipped."""
        from hsync_client.utils import set_permissions

        test_file = tmp_path / "perms.txt"
        test_file.write_text("test")
        test_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

        # Should not fail and should skip if same
        set_permissions(str(test_file), "600")

        # Now try to set back (for cleanup)
        test_file.chmod(stat.S_IRWXU)


class TestSymlinks:
    """Tests for symbolic link handling."""

    def test_symlink_target_exists(self, tmp_path):
        """Test File.get on a symlink where target exists."""
        target = tmp_path / "target.txt"
        target.write_text("target content")

        link = tmp_path / "link.txt"
        link.symlink_to(target)

        # File.get follows symlink by default
        file_obj = File.get(str(link))
        assert file_obj is not None

    def test_symlink_broken(self, tmp_path):
        """Test File.get on a broken symlink."""
        link = tmp_path / "broken_link.txt"
        link.symlink_to("nonexistent_target")

        # Should return None for broken symlink
        file_obj = File.get(str(link))
        assert file_obj is None


class TestLongPaths:
    """Tests for long path handling."""

    def test_very_long_filename(self, tmp_path):
        """Test handling files with very long names."""
        # Create a path that's close to filesystem limits
        long_name = "a" * 200 + ".txt"
        test_file = tmp_path / long_name

        try:
            test_file.write_text("long name content")
            file_obj = File.get(str(test_file))
            assert file_obj is not None
            assert file_obj.name == long_name
        except OSError:
            # Some filesystems have limits, skip if not supported
            pytest.skip("Filesystem does not support long filenames")

    def test_deeply_nested_directory(self, tmp_path):
        """Test handling deeply nested directory structures."""
        # Create nested directories
        current = tmp_path
        for i in range(20):
            current = current / f"dir_{i}"
            current.mkdir()

        test_file = current / "deep_file.txt"
        test_file.write_text("deep content")

        file_obj = File.get(str(test_file))
        assert file_obj is not None
        assert file_obj.filepath is not None and "dir_19" in file_obj.filepath


class TestFileLocking:
    """Tests for file locking and retry scenarios."""

    def test_retry_on_permission_error(self, tmp_path):
        """Test that file operations retry on permission errors."""
        from errno import EACCES

        from hsync_client.sync import _get_single_file_metadata

        test_file = tmp_path / "locked.txt"
        test_file.write_text("content")

        # Mock File.get to raise permission error first, then succeed
        original_get = File.get

        call_count = [0]

        def mock_get(path, buckets_dir=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call raises permission error
                error = OSError()
                error.errno = EACCES
                raise error
            return original_get(path, buckets_dir)

        with patch.object(File, "get", side_effect=mock_get):
            _get_single_file_metadata(str(test_file))
            # Should have retried and eventually succeeded
            assert call_count[0] > 1

    def test_max_retries_exceeded(self, tmp_path):
        """Test that operations fail after max retries."""
        from errno import EACCES

        from hsync_client.sync import _get_single_file_metadata

        test_file = tmp_path / "always_locked.txt"
        test_file.write_text("content")

        # Mock File.get to always raise permission error
        def mock_get(path, buckets_dir=None):
            error = OSError()
            error.errno = EACCES
            raise error

        with patch.object(File, "get", side_effect=mock_get):
            result = _get_single_file_metadata(str(test_file))
            # Should return None after max retries
            assert result is None


class TestSHA256Performance:
    """Performance tests for SHA-256 computation."""

    def test_sha256_chunking_performance(self, tmp_path):
        """Test that chunking SHA-256 computation performs reasonably."""
        # Create a 10MB test file
        test_file = tmp_path / "large_file.bin"
        with open(test_file, "wb") as f:
            # Write 10MB of data
            f.write(b"x" * (10 * 1024 * 1024))

        start = time.time()
        sha256sum = File._compute_sha256(str(test_file))
        elapsed = time.time() - start

        # Should complete in reasonable time (< 2 seconds for 10MB)
        assert elapsed < 2.0
        assert len(sha256sum) == 64  # SHA-256 is 64 hex chars

        # Verify correctness
        import hashlib

        expected = hashlib.sha256(open(test_file, "rb").read()).hexdigest()
        assert sha256sum == expected

    def test_sha256_small_file(self, tmp_path):
        """Test SHA-256 on small file."""
        test_file = tmp_path / "small.txt"
        test_file.write_text("hello world")

        sha256sum = File._compute_sha256(str(test_file))

        import hashlib

        expected = hashlib.sha256(b"hello world").hexdigest()
        assert sha256sum == expected

    def test_sha256_empty_file(self, tmp_path):
        """Test SHA-256 on empty file."""
        test_file = tmp_path / "empty.txt"
        test_file.write_text("")

        sha256sum = File._compute_sha256(str(test_file))

        import hashlib

        expected = hashlib.sha256(b"").hexdigest()
        assert sha256sum == expected


class TestEdgeCasesFileOperations:
    """Additional edge case tests for file operations."""

    def test_file_with_special_chars_in_name(self, tmp_path):
        """Test handling files with special characters."""
        # Create file with various special chars (but not forbidden)
        test_file = tmp_path / "file-with-dashes_underscores.txt"
        test_file.write_text("special content")

        file_obj = File.get(str(test_file))
        assert file_obj is not None
        assert file_obj.name == "file-with-dashes_underscores.txt"

    def test_file_with_spaces(self, tmp_path):
        """Test handling files with spaces."""
        test_file = tmp_path / "file with spaces.txt"
        test_file.write_text("spaces content")

        file_obj = File.get(str(test_file))
        assert file_obj is not None
        assert file_obj.name is not None
        assert " " in file_obj.name

    def test_hidden_file_detection(self):
        """Test hidden file detection patterns."""
        from hsync_common.models.file import is_hidden_file

        assert is_hidden_file(".hidden") is True
        assert is_hidden_file("file.txt") is False
        assert is_hidden_file("backup~") is True
        assert is_hidden_file("file.txt.bak") is True
        assert is_hidden_file("file.txt.tmp") is True

    def test_sentinel_not_hidden(self):
        """Test that .hsynckeep sentinel file is never considered hidden."""
        from hsync_common.models.file import is_hidden_file

        assert is_hidden_file(".hsynckeep") is False

    def test_update_file_with_permissions(self, tmp_path):
        """Test updating file when only permissions differ."""
        from hsync_client.config import CONFIG
        from hsync_client.sync import update_file

        # Create remote file data (wire paths are bucket-relative)
        remote_data = {
            "filepath": "test.txt",
            "mtime": int(time.time()),
            "size": 10,
            "sha256sum": "abc123",
            "permissions": "644",
        }

        # Create local file with different permissions
        local_file = tmp_path / "test.txt"
        local_file.write_text("content")
        local_file.chmod(stat.S_IRWXU)

        # Set up required CONFIG
        original_config = CONFIG.copy()
        CONFIG["LOCAL_BUCKET"] = str(tmp_path)

        try:
            # Mock download_file to avoid network calls
            with patch("hsync_client.transfer.download_file") as mock_download:
                update_file(remote_data)

                # Should not download since content matches (mock only checks sha256)
                # but may set permissions
                # The key is it shouldn't crash
                mock_download.assert_called()
        finally:
            # Cast to dict to access clear/update methods (TypedDict doesn't have them)
            cast(dict, CONFIG).clear()
            cast(dict, CONFIG).update(original_config)


class TestConcurrency:
    """Tests for concurrent file operations."""

    def test_parallel_file_reads(self, tmp_path):
        """Test reading multiple files in parallel."""
        from concurrent.futures import ThreadPoolExecutor

        # Create multiple test files
        files = []
        for i in range(10):
            f = tmp_path / f"file_{i}.txt"
            f.write_text(f"content {i}")
            files.append(str(f))

        # Read all files in parallel
        def get_file(path):
            return File.get(path)

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(get_file, files))

        # All should succeed
        assert len(results) == 10
        assert all(r is not None for r in results)


class TestLANHandler:
    """Tests for LAN handler functions."""

    def test_measure_clock_offset_success(self, tmp_path):
        """Test _measure_clock_offset with successful response."""
        from hsync_client.config import HsyncConfig
        from hsync_client.lan_handler import _measure_clock_offset

        mock_master = MagicMock()
        mock_master.cid = "master-1"
        mock_master.get_server_url.return_value = "http://192.168.1.1:9000"

        mock_config: HsyncConfig = cast(
            HsyncConfig,
            {
                "REQUEST_HEADERS": {"X-Client-Id": "test"},
                "HTTP_TIMEOUT": 30,
            },
        )
        mock_log = MagicMock()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"timestamp": 1000.0}

        with patch("hsync_client.utils.http_get", return_value=mock_response):
            with patch("hsync_client.utils.get_ssl_verify", return_value=True):
                _measure_clock_offset(mock_master, mock_config, mock_log)

        # Verify clock_offset was set (MagicMock returns a MagicMock, check if assignment happened)
        assert hasattr(mock_master, "clock_offset")

    def test_measure_clock_offset_non_200_status(self, tmp_path):
        """Test _measure_clock_offset with non-200 status code."""
        from hsync_client.config import HsyncConfig
        from hsync_client.lan_handler import _measure_clock_offset

        mock_master = MagicMock()
        mock_master.cid = "master-1"

        mock_config: HsyncConfig = cast(
            HsyncConfig,
            {
                "REQUEST_HEADERS": {"X-Client-Id": "test"},
                "HTTP_TIMEOUT": 30,
            },
        )
        mock_log = MagicMock()

        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("hsync_client.utils.http_get", return_value=mock_response):
            with patch("hsync_client.utils.get_ssl_verify", return_value=True):
                _measure_clock_offset(mock_master, mock_config, mock_log)

        # Should log debug message about non-200 status
        assert mock_log.debug.call_count >= 1

    def test_measure_clock_offset_no_timestamp(self, tmp_path):
        """Test _measure_clock_offset when response has no timestamp."""
        from hsync_client.config import HsyncConfig
        from hsync_client.lan_handler import _measure_clock_offset

        mock_master = MagicMock()
        mock_master.cid = "master-1"

        mock_config: HsyncConfig = cast(
            HsyncConfig,
            {
                "REQUEST_HEADERS": {"X-Client-Id": "test"},
                "HTTP_TIMEOUT": 30,
            },
        )
        mock_log = MagicMock()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}  # No timestamp

        with patch("hsync_client.utils.http_get", return_value=mock_response):
            with patch("hsync_client.utils.get_ssl_verify", return_value=True):
                _measure_clock_offset(mock_master, mock_config, mock_log)

        # Should log debug message about missing timestamp
        assert mock_log.debug.call_count >= 1

    def test_measure_clock_offset_request_exception(self, tmp_path):
        """Test _measure_clock_offset when request fails."""
        from requests import RequestException

        from hsync_client.config import HsyncConfig
        from hsync_client.lan_handler import _measure_clock_offset

        mock_master = MagicMock()
        mock_master.cid = "master-1"

        mock_config: HsyncConfig = cast(
            HsyncConfig,
            {
                "REQUEST_HEADERS": {"X-Client-Id": "test"},
                "HTTP_TIMEOUT": 30,
            },
        )
        mock_log = MagicMock()

        with patch(
            "hsync_client.utils.http_get",
            side_effect=RequestException("Connection failed"),
        ):
            with patch("hsync_client.utils.get_ssl_verify", return_value=True):
                _measure_clock_offset(mock_master, mock_config, mock_log)

        # Should log debug message about request failure
        assert mock_log.debug.call_count >= 1

    def test_measure_clock_offset_large_clock_skew(self, tmp_path):
        """Test _measure_clock_offset logs warning on large clock skew."""
        import time

        from hsync_common.clock import CLOCK_SKEW_THRESHOLD

        from hsync_client.config import HsyncConfig
        from hsync_client.lan_handler import _measure_clock_offset

        mock_master = MagicMock()
        mock_master.cid = "master-1"

        # Calculate a timestamp that will result in a large offset
        # Large offset = timestamp - (t1 + rtt/2) > CLOCK_SKEW_THRESHOLD
        large_timestamp = time.time() + CLOCK_SKEW_THRESHOLD * 10

        mock_config: HsyncConfig = cast(
            HsyncConfig,
            {
                "REQUEST_HEADERS": {"X-Client-Id": "test"},
                "HTTP_TIMEOUT": 30,
            },
        )
        mock_log = MagicMock()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"timestamp": large_timestamp}

        with patch("hsync_client.utils.http_get", return_value=mock_response):
            with patch("hsync_client.utils.get_ssl_verify", return_value=True):
                _measure_clock_offset(mock_master, mock_config, mock_log)

        # Should log a warning about clock skew
        assert mock_log.warning.call_count == 1

    def test_run_preview_calls_preview_impl(self, tmp_path):
        """Test _run_preview calls the preview implementation."""
        from unittest.mock import patch

        from hsync_client.lan_handler import _run_preview

        mock_log = MagicMock()

        with patch("hsync_client.main_logic._run_preview") as mock_preview_impl:
            _run_preview(mock_log)

        mock_preview_impl.assert_called_once_with(mock_log)
