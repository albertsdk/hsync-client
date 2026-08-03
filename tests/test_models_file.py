"""
Tests for File model.
"""

import os
import tempfile
from datetime import UTC, datetime

import pytest
from hsync_common.models.file import File, is_hidden_file


class TestIsHiddenFile:
    """Tests for is_hidden_file function.

    Following the industry standard (Syncthing, rsync, Nextcloud, OneDrive),
    dotfiles in general (.env, .config, .git …) are NOT excluded: only OS
    cache files and editor/office temporary files are filtered by default.
    """

    def test_dotfile_not_hidden(self):
        """Dotfiles like .env, .config, .gitignore are synced normally."""
        assert is_hidden_file(".gitignore") is False
        assert is_hidden_file("/path/to/.gitignore") is False
        assert is_hidden_file(".hidden") is False
        assert is_hidden_file(".env") is False
        assert is_hidden_file(".config") is False

    def test_ds_store_is_hidden(self):
        """macOS .DS_Store cache files are hidden."""
        assert is_hidden_file(".DS_Store") is True

    def test_thumbs_db_is_hidden(self):
        """Windows Thumbs.db cache files are hidden."""
        assert is_hidden_file("Thumbs.db") is True

    def test_office_lock_is_hidden(self):
        """Microsoft Office lock files are hidden."""
        assert is_hidden_file("~$document.docx") is True

    def test_backup_tilde(self):
        """Backup files ending with tilde are hidden."""
        assert is_hidden_file("file.txt~") is True
        assert is_hidden_file("/path/file~") is True

    def test_vim_temp(self):
        """Vim temporary file 4913 is hidden."""
        assert is_hidden_file("4913") is True
        assert is_hidden_file("/path/4913") is True

    def test_vim_swap(self):
        """Vim swap files are hidden, but only as dotfiles (`.file.swp`).

        The `.*.sw?` pattern deliberately ignores non-hidden names so files
        like `animation.swf` are not excluded by mistake.
        """
        assert is_hidden_file(".file.swp") is True
        assert is_hidden_file(".file.swo") is True
        assert is_hidden_file("file.swp") is False
        assert is_hidden_file("file.swo") is False

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

    def test_internal_files_are_hidden(self):
        """Engine-owned files are always hidden."""
        assert is_hidden_file(".hsyncignore") is True
        assert is_hidden_file(".hsyncstate") is True
        assert is_hidden_file(".hsynctombstones") is True

    def test_partial_files_are_hidden(self):
        """In-progress transfer artifacts are always hidden."""
        assert is_hidden_file("document.txt.part") is True
        assert is_hidden_file("document.txt.delta.part") is True


class TestFileParseTime:
    """Tests for File._parse_time method."""

    def test_parse_datetime(self):
        """Parse datetime object to timestamp."""
        dt = datetime(2024, 1, 15, 12, 30, 0, tzinfo=UTC)
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
            sha256sum="abc123",
            permissions="644",
        )
        d = file.to_dict()
        assert d["filepath"] == "/path/to/file.txt"
        assert d["name"] == "file.txt"
        assert d["size"] == 1024
        assert d["sha256sum"] == "abc123"
        assert d["permissions"] == "644"

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
            assert file.sha256sum is not None
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
            "sha256sum": "abc123",
            "permissions": "644",
        }
        file = File.from_dict(data, dir_prefix="/bucket")
        assert file.filepath == "file.txt"
        assert file.size == 1024

    def test_from_dict_no_strip(self):
        """Test File.from_dict without stripping prefix."""
        data = {"filepath": "file.txt", "size": 1024}
        file = File.from_dict(data, dir_prefix="/bucket", strip_dir_prefix=False)
        assert file.filepath == "/bucket/file.txt"

    def test_compute_sha256(self):
        """Test SHA-256 computation."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test content")
            temp_path = f.name

        try:
            sha256 = File._compute_sha256(temp_path)
            # SHA-256 of "test content"
            assert sha256 == "6ae8a75555209fd6c44157c0aed8016e763ff435a19cf186f76863140143ff72"
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
            "sha256sum": "abc123",
            "permissions": "644",
        }
        # Need to provide dir_prefix or set strip_dir_prefix=False
        f = File.from_dict(data, dir_prefix="/bucket", strip_dir_prefix=False)
        assert f.ctime is not None
        assert f.mtime is not None
        assert f.sha256sum == "abc123"
        assert f.permissions == "644"
