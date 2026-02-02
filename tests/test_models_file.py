"""
Tests for File model.
"""

import os
import tempfile
from datetime import datetime, timezone

import pytest

from hsync_client.models.file import File, is_hidden_file


class TestIsHiddenFile:
    """Tests for is_hidden_file function."""

    def test_hidden_dotfile(self):
        """Files starting with dot are hidden."""
        assert is_hidden_file(".gitignore") is True
        assert is_hidden_file("/path/to/.gitignore") is True
        assert is_hidden_file(".hidden") is True

    def test_backup_tilde(self):
        """Backup files ending with tilde are hidden."""
        assert is_hidden_file("file.txt~") is True
        assert is_hidden_file("/path/file~") is True

    def test_vim_temp(self):
        """Vim temporary file 4913 is hidden."""
        assert is_hidden_file("4913") is True
        assert is_hidden_file("/path/4913") is True

    def test_vim_swap(self):
        """Vim swap files (.swp, .swo) are hidden."""
        assert is_hidden_file(".file.swp") is True
        assert is_hidden_file(".file.swo") is True
        assert is_hidden_file("file.swp") is True
        assert is_hidden_file("file.swo") is True

    def test_libreoffice_lock(self):
        """LibreOffice lock files are hidden."""
        assert is_hidden_file(".~lock.document.odt#") is True

    def test_backup_extensions(self):
        """Common backup extensions are hidden."""
        assert is_hidden_file("file.bak") is True
        assert is_hidden_file("file.tmp") is True
        assert is_hidden_file("file.temp") is True

    def test_timestamped_backup(self):
        """Timestamped backup files are hidden."""
        assert is_hidden_file("file-2024-01-15-143022") is True

    def test_regular_files(self):
        """Regular files are not hidden."""
        assert is_hidden_file("file.txt") is False
        assert is_hidden_file("document.pdf") is False
        assert is_hidden_file("/path/to/file.py") is False


class TestFileParseTime:
    """Tests for File._parse_time method."""

    def test_parse_datetime(self):
        """Parse datetime object to timestamp."""
        dt = datetime(2024, 1, 15, 12, 30, 0, tzinfo=timezone.utc)
        file = File()
        result = file._parse_time(dt)
        assert result == int(dt.timestamp())

    def test_parse_iso_string(self):
        """Parse ISO format string to timestamp."""
        file = File()
        result = file._parse_time("2024-01-15T12:30:00+00:00")
        assert isinstance(result, int)

    def test_parse_int_timestamp(self):
        """Parse integer timestamp."""
        file = File()
        ts = 1705320600
        result = file._parse_time(ts)
        assert result == ts

    def test_parse_float_timestamp(self):
        """Parse float timestamp."""
        file = File()
        ts = 1705320600.123
        result = file._parse_time(ts)
        assert isinstance(result, int)

    def test_parse_none(self):
        """Parse None returns None."""
        file = File()
        assert file._parse_time(None) is None


class TestFileModel:
    """Tests for File class."""

    def test_init_basic(self):
        """Test basic initialization."""
        file = File(filepath="/path/to/file.txt", size=1024)
        assert file.filepath == "/path/to/file.txt"
        assert file.name == "file.txt"
        assert file.size == 1024

    def test_to_dict(self):
        """Test conversion to dictionary."""
        file = File(
            filepath="/path/to/file.txt",
            ctime=1705320600,
            mtime=1705320700,
            size=1024,
            md5sum="abc123",
            permissions="-rw-r--r--",
        )
        d = file.to_dict()
        assert d["filepath"] == "/path/to/file.txt"
        assert d["name"] == "file.txt"
        assert d["size"] == 1024
        assert d["md5sum"] == "abc123"
        assert d["permissions"] == "-rw-r--r--"

    def test_get_existing_file(self):
        """Test File.get with existing file."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test content")
            temp_path = f.name

        try:
            file = File.get(temp_path)
            assert file is not None
            assert file.filepath == temp_path
            assert file.size == 12
            assert file.md5sum is not None
            assert file.permissions is not None
        finally:
            os.unlink(temp_path)

    def test_get_nonexistent_file(self):
        """Test File.get with non-existent file."""
        file = File.get("/nonexistent/file.txt")
        assert file is None

    def test_from_dict(self):
        """Test File.from_dict."""
        data = {
            "filepath": "/bucket/file.txt",
            "ctime": 1705320600,
            "mtime": 1705320700,
            "size": 1024,
            "md5sum": "abc123",
            "permissions": "-rw-r--r--",
        }
        file = File.from_dict(data, dir_prefix="/bucket")
        assert file.filepath == "file.txt"
        assert file.size == 1024

    def test_from_dict_no_strip(self):
        """Test File.from_dict without stripping prefix."""
        data = {"filepath": "file.txt", "size": 1024}
        file = File.from_dict(data, dir_prefix="/bucket", strip_dir_prefix=False)
        assert file.filepath == "/bucket/file.txt"

    def test_compute_md5(self):
        """Test MD5 computation."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test content")
            temp_path = f.name

        try:
            md5 = File._compute_md5(temp_path)
            # MD5 of "test content"
            assert md5 == "9473fdd0d880a43c21b7778d34872157"
        finally:
            os.unlink(temp_path)

    def test_from_dict_strip_without_prefix_raises(self):
        """from_dict raises ValueError when strip_dir_prefix=True but no dir_prefix."""
        data = {"filepath": "/bucket/test.txt", "size": 100}
        with pytest.raises(ValueError, match="Set dir_prefix to strip it"):
            File.from_dict(data, strip_dir_prefix=True)

    def test_from_dict_with_all_fields(self):
        """from_dict handles all optional fields."""
        data = {
            "filepath": "test.txt",
            "ctime": 1000.0,
            "mtime": 2000.0,
            "size": 500,
            "md5sum": "abc123",
            "permissions": "-rw-r--r--"
        }
        # Need to provide dir_prefix or set strip_dir_prefix=False
        f = File.from_dict(data, dir_prefix="/bucket", strip_dir_prefix=False)
        assert f.ctime is not None
        assert f.mtime is not None
        assert f.md5sum == "abc123"
        assert f.permissions == "-rw-r--r--"

