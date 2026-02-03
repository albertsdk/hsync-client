"""
Tests for utility functions in hsync_client.main module.
"""

import os
import stat
import tempfile
from argparse import ArgumentTypeError

import pytest

from hsync_client.main import (
    is_valid_logfile,
    is_same_paths,
    get_cid,
    bytes_to_human_readable,
    compute_md5,
    get_permissions,
    get_chunk_size,
    set_permissions,
    valid_reconnect_delay,
    valid_resync,
    valid_bucket,
)


class TestIsValidLogfile:
    """Tests for is_valid_logfile function."""

    def test_simple_filename(self):
        """Test simple filename is valid."""
        assert is_valid_logfile("hsync.log") is True

    def test_filename_with_extension(self):
        """Test filename with extension is valid."""
        assert is_valid_logfile("app.log.txt") is True

    def test_forbidden_chars_in_filename(self):
        """Test forbidden characters are rejected."""
        assert is_valid_logfile("file<name>.log") is False
        assert is_valid_logfile("file:name.log") is False
        assert is_valid_logfile("file|name.log") is False
        assert is_valid_logfile("file?name.log") is False
        assert is_valid_logfile("file*name.log") is False

    def test_reserved_names_windows(self):
        """Test Windows reserved names are rejected."""
        assert is_valid_logfile("CON") is False
        assert is_valid_logfile("PRN") is False
        assert is_valid_logfile("AUX") is False
        assert is_valid_logfile("NUL") is False
        assert is_valid_logfile("COM1") is False
        assert is_valid_logfile("LPT1") is False

    def test_absolute_path_valid(self):
        """Test absolute path is valid."""
        assert is_valid_logfile("/var/log/hsync.log") is True
        assert is_valid_logfile("/tmp/app.log") is True

    def test_relative_path_with_dir_invalid(self):
        """Test relative path with directory is invalid."""
        assert is_valid_logfile("logs/hsync.log") is False
        assert is_valid_logfile("subdir/logs/hsync.log") is False


class TestIsSamePaths:
    """Tests for is_same_paths function."""

    def test_same_path(self):
        """Test identical paths."""
        assert is_same_paths("/tmp/file.txt", "/tmp/file.txt") is True

    def test_normalized_paths(self):
        """Test paths that normalize to same location."""
        assert is_same_paths("/tmp/../tmp/file.txt", "/tmp/file.txt") is True
        assert is_same_paths("/tmp/./file.txt", "/tmp/file.txt") is True

    def test_different_paths(self):
        """Test different paths."""
        assert is_same_paths("/tmp/file1.txt", "/tmp/file2.txt") is False


class TestGetCid:
    """Tests for get_cid function."""

    def test_default_length(self):
        """Test default CID length is 10."""
        cid = get_cid()
        assert len(cid) == 10

    def test_custom_length(self):
        """Test custom CID length."""
        cid = get_cid(length=20)
        assert len(cid) == 20

    def test_alphanumeric_only(self):
        """Test CID contains only alphanumeric characters."""
        cid = get_cid(length=100)
        assert cid.isalnum()

    def test_unique_cids(self):
        """Test CIDs are unique."""
        cids = [get_cid() for _ in range(100)]
        assert len(set(cids)) == 100


class TestBytesToHumanReadable:
    """Tests for bytes_to_human_readable function."""

    def test_bytes(self):
        """Test bytes formatting."""
        assert bytes_to_human_readable(0) == "0.00B"
        assert bytes_to_human_readable(512) == "512.00B"
        assert bytes_to_human_readable(1023) == "1023.00B"

    def test_kilobytes(self):
        """Test kilobytes formatting."""
        assert bytes_to_human_readable(1024) == "1.00KB"
        assert bytes_to_human_readable(1536) == "1.50KB"

    def test_megabytes(self):
        """Test megabytes formatting."""
        assert bytes_to_human_readable(1024 * 1024) == "1.00MB"
        assert bytes_to_human_readable(1.5 * 1024 * 1024) == "1.50MB"

    def test_gigabytes(self):
        """Test gigabytes formatting."""
        assert bytes_to_human_readable(1024 ** 3) == "1.00GB"

    def test_terabytes(self):
        """Test terabytes formatting."""
        assert bytes_to_human_readable(1024 ** 4) == "1.00TB"


class TestComputeMd5:
    """Tests for compute_md5 function."""

    def test_compute_md5_empty_file(self):
        """Test MD5 of empty file."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"")
            temp_path = f.name

        try:
            # MD5 of empty string
            assert compute_md5(temp_path) == "d41d8cd98f00b204e9800998ecf8427e"
        finally:
            os.unlink(temp_path)

    def test_compute_md5_known_content(self):
        """Test MD5 of known content."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"hello world")
            temp_path = f.name

        try:
            # MD5 of "hello world"
            assert compute_md5(temp_path) == "5eb63bbbe01eeed093cb22bb8f5acdc3"
        finally:
            os.unlink(temp_path)


class TestGetChunkSize:
    """Tests for get_chunk_size function."""

    def test_none_returns_default(self):
        """Test None file size returns default 64KB."""
        assert get_chunk_size(None) == 64 * 1024

    def test_small_file_under_1mb(self):
        """Test small file returns 8KB chunk."""
        assert get_chunk_size(500_000) == 8 * 1024
        assert get_chunk_size(999_999) == 8 * 1024

    def test_medium_file_under_100mb(self):
        """Test medium file returns 64KB chunk."""
        assert get_chunk_size(1_000_000) == 64 * 1024
        assert get_chunk_size(50_000_000) == 64 * 1024
        assert get_chunk_size(99_999_999) == 64 * 1024

    def test_large_file_over_100mb(self):
        """Test large file returns 256KB chunk."""
        assert get_chunk_size(100_000_000) == 256 * 1024
        assert get_chunk_size(1_000_000_000) == 256 * 1024


class TestGetPermissions:
    """Tests for get_permissions function."""

    def test_get_file_permissions(self):
        """Test getting file permissions."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name
            os.chmod(temp_path, 0o644)

        try:
            perms = get_permissions(temp_path)
            assert perms == "-rw-r--r--"
        finally:
            os.unlink(temp_path)

    def test_get_directory_permissions(self):
        """Test getting directory permissions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chmod(temp_dir, 0o755)
            perms = get_permissions(temp_dir)
            assert perms == "drwxr-xr-x"

    def test_get_executable_permissions(self):
        """Test getting executable file permissions."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name
            os.chmod(temp_path, 0o755)

        try:
            perms = get_permissions(temp_path)
            assert perms == "-rwxr-xr-x"
        finally:
            os.unlink(temp_path)


class TestSetPermissions:
    """Tests for set_permissions function."""

    def test_set_read_write_permissions(self):
        """Test setting read-write permissions."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name

        try:
            set_permissions(temp_path, "-rw-r--r--")
            mode = stat.S_IMODE(os.stat(temp_path).st_mode)
            assert mode == 0o644
        finally:
            os.unlink(temp_path)

    def test_set_executable_permissions(self):
        """Test setting executable permissions."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name

        try:
            set_permissions(temp_path, "-rwxr-xr-x")
            mode = stat.S_IMODE(os.stat(temp_path).st_mode)
            assert mode == 0o755
        finally:
            os.unlink(temp_path)

    def test_set_permissions_no_change(self):
        """Test setting same permissions does nothing."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name
            os.chmod(temp_path, 0o644)

        try:
            # Should not raise, just return
            set_permissions(temp_path, "-rw-r--r--")
            mode = stat.S_IMODE(os.stat(temp_path).st_mode)
            assert mode == 0o644
        finally:
            os.unlink(temp_path)


class TestValidReconnectDelay:
    """Tests for valid_reconnect_delay function."""

    def test_valid_value(self):
        """Test valid reconnect delay value."""
        assert valid_reconnect_delay("30") == 30
        assert valid_reconnect_delay("10") == 10
        assert valid_reconnect_delay("59") == 59

    def test_boundary_too_low(self):
        """Test value at lower boundary is rejected."""
        with pytest.raises(ArgumentTypeError, match="must be between 1 and 60"):
            valid_reconnect_delay("1")

    def test_boundary_too_high(self):
        """Test value at upper boundary is rejected."""
        with pytest.raises(ArgumentTypeError, match="must be between 1 and 60"):
            valid_reconnect_delay("60")

    def test_non_integer(self):
        """Test non-integer value is rejected."""
        with pytest.raises(ArgumentTypeError, match="must be integer"):
            valid_reconnect_delay("abc")
        with pytest.raises(ArgumentTypeError, match="must be integer"):
            valid_reconnect_delay("3.5")


class TestValidResync:
    """Tests for valid_resync function."""

    def test_valid_value(self):
        """Test valid resync value."""
        assert valid_resync("60") == 60
        assert valid_resync("300") == 300
        assert valid_resync("3599") == 3599

    def test_boundary_too_low(self):
        """Test value at lower boundary is rejected."""
        with pytest.raises(ArgumentTypeError, match="must be between 20 and 3600"):
            valid_resync("20")

    def test_boundary_too_high(self):
        """Test value at upper boundary is rejected."""
        with pytest.raises(ArgumentTypeError, match="must be between 20 and 3600"):
            valid_resync("3600")

    def test_non_integer(self):
        """Test non-integer value is rejected."""
        with pytest.raises(ArgumentTypeError, match="must be integer"):
            valid_resync("abc")


class TestValidBucket:
    """Tests for valid_bucket function."""

    def test_valid_alphanumeric(self):
        """Test valid alphanumeric bucket names."""
        assert valid_bucket("bucket123") == "bucket123"
        assert valid_bucket("MYBUCKET") == "MYBUCKET"
        assert valid_bucket("test") == "test"
        assert valid_bucket("123") == "123"

    def test_invalid_special_chars(self):
        """Test bucket names with special characters are rejected."""
        with pytest.raises(ArgumentTypeError, match="must be alphanumeric"):
            valid_bucket("bucket-name")
        with pytest.raises(ArgumentTypeError, match="must be alphanumeric"):
            valid_bucket("bucket_name")
        with pytest.raises(ArgumentTypeError, match="must be alphanumeric"):
            valid_bucket("bucket.name")

    def test_invalid_with_spaces(self):
        """Test bucket names with spaces are rejected."""
        with pytest.raises(ArgumentTypeError, match="must be alphanumeric"):
            valid_bucket("my bucket")

