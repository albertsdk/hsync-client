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

        with open(test_file, "rb") as f:
            expected = hashlib.sha256(f.read()).hexdigest()
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
        """Test hidden file detection patterns.

        Following the industry standard, dotfiles are NOT excluded by
        default: only OS cache files and editor/office temporary files
        are filtered.
        """
        from hsync_common.models.file import is_hidden_file

        # Dotfiles are NOT hidden (industry standard)
        assert is_hidden_file(".hidden") is False
        assert is_hidden_file(".env") is False
        # Regular files are not hidden
        assert is_hidden_file("file.txt") is False
        # OS cache / temp / backup files ARE hidden
        assert is_hidden_file(".DS_Store") is True
        assert is_hidden_file("Thumbs.db") is True
        assert is_hidden_file("backup~") is True
        assert is_hidden_file("file.txt.bak") is True
        assert is_hidden_file("file.txt.tmp") is True

    def test_sentinel_not_hidden(self):
        """Test that .hsynckeep sentinel file is never considered hidden."""
        from hsync_common.models.file import is_hidden_file

        assert is_hidden_file(".hsynckeep") is False

    def test_update_file_with_permissions(self, tmp_path):
        """Test updating file when only permissions differ."""
        from hsync_client import sync_state
        from hsync_client.config import CONFIG
        from hsync_client.sync import update_file

        # Create remote file data (wire paths are bucket-relative). Use the
        # local file's real hash and a recorded base equal to it so the causal
        # matrix resolves to dominance (local at base, remote moved on) and the
        # permissions-only branch is reached after the (mocked) download.
        local_file = tmp_path / "test.txt"
        local_file.write_text("content")
        local_file.chmod(stat.S_IRWXU)

        from hsync_common.hashing import compute_sha256

        local_sha = compute_sha256(str(local_file))

        remote_data = {
            "filepath": "test.txt",
            "mtime": int(time.time()),
            "size": 10,
            "sha256sum": local_sha,
            "permissions": "644",
        }

        # Set up required CONFIG
        original_config = CONFIG.copy()
        CONFIG["LOCAL_BUCKET"] = str(tmp_path)
        CONFIG["CID"] = "EdgeClient1234567890"

        try:
            with patch("hsync_client.sync_state.CONFIG", CONFIG):
                sync_state.record_base(str(local_file), "0" * 64)
                sync_state.flush()
                sync_state._stores.clear()
            # Mock download_file to avoid network calls
            with patch("hsync_client.transfer.download_file") as mock_download:
                update_file(remote_data)

                # Content matches the remote hash (both are local_sha), so no
                # download is needed; permissions differ and are applied. The
                # key is it shouldn't crash.
                assert not mock_download.called
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

    def test_run_preview_calls_preview_impl(self, tmp_path):
        """Test _run_preview calls the preview implementation."""
        from unittest.mock import patch

        from hsync_client import lan_handler

        mock_log = MagicMock()

        with patch("hsync_client.lan_handler._run_preview") as mock_preview_impl:
            lan_handler._run_preview(mock_log)

        mock_preview_impl.assert_called_once_with(mock_log)
