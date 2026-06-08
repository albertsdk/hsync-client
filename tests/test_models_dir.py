"""
Tests for Dir model.
"""

import os
import tempfile

from hsync_client.models.dir import Dir, is_hidden_dir


class TestIsHiddenDir:
    """Tests for is_hidden_dir function."""

    def test_hidden_parent(self):
        """Directory with hidden parent is hidden."""
        assert is_hidden_dir("/.git/objects") is True
        assert is_hidden_dir("/path/.hidden/subdir") is True

    def test_regular_dir(self):
        """Regular directories are not hidden."""
        assert is_hidden_dir("/path/to/dir") is False
        assert is_hidden_dir("/regular/path") is False

    def test_root_hidden(self):
        """Hidden directory at root is detected via parent."""
        # Note: is_hidden_dir checks parent directory name
        assert is_hidden_dir("/home/user/.config/test") is True


class TestDirModel:
    """Tests for Dir class."""

    def test_init_basic(self):
        """Test basic initialization."""
        d = Dir(path="/path/to/dir", permissions="drwxr-xr-x")
        assert d.path == "/path/to/dir"
        assert d.permissions == "drwxr-xr-x"

    def test_to_dict(self):
        """Test conversion to dictionary."""
        d = Dir(path="/path/to/dir", permissions="drwxr-xr-x")
        result = d.to_dict()
        assert result["path"] == "/path/to/dir"
        assert result["permissions"] == "drwxr-xr-x"

    def test_repr(self):
        """Test string representation."""
        d = Dir(path="/test", permissions="drwxr-xr-x")
        repr_str = repr(d)
        assert "Dir" in repr_str
        assert "/test" in repr_str

    def test_get_existing_dir(self):
        """Test Dir.get with existing directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            d = Dir.get(temp_dir)
            assert d is not None
            assert d.path == temp_dir
            assert d.permissions is not None
            assert d.permissions.startswith("d")

    def test_get_nonexistent_dir(self):
        """Test Dir.get with non-existent directory."""
        d = Dir.get("/nonexistent/directory")
        assert d is None

    def test_get_with_bucket_dir(self):
        """Test Dir.get with bucket directory prefix."""
        with tempfile.TemporaryDirectory() as temp_dir:
            subdir = os.path.join(temp_dir, "subdir")
            os.makedirs(subdir)

            # When bucket_dir is provided, path is relative
            d = Dir.get("subdir", buckets_dir=temp_dir)
            assert d is not None
            assert d.path == subdir

    def test_from_dict(self):
        """Test Dir.from_dict."""
        data = {"path": "/bucket/subdir", "permissions": "drwxr-xr-x"}
        d = Dir.from_dict(data, localpath="/bucket", strip_localpath=True)
        assert d.path == "subdir"
        assert d.permissions == "drwxr-xr-x"

    def test_from_dict_no_strip(self):
        """Test Dir.from_dict without stripping localpath."""
        data = {"path": "subdir", "permissions": "drwxr-xr-x"}
        d = Dir.from_dict(data, localpath="/bucket", strip_localpath=False)
        assert d.path == "/bucket/subdir"

    def test_get_permissions(self):
        """Test _get_permissions with real directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            perms = Dir._get_permissions(temp_dir)
            assert perms.startswith("d")
            assert len(perms) == 10


class TestTombstoneReexports:
    """Tombstone symbols are re-exported from hsync_client.models (2.15)."""

    def test_tombstone_symbols_importable_from_package(self):
        from hsync_client.models import (
            TombstoneEntry,
            TombstoneStore,
            relpath_in_bucket,
            tombstone_key,
        )

        assert TombstoneEntry is not None
        assert TombstoneStore is not None
        assert callable(tombstone_key)
        assert callable(relpath_in_bucket)

    def test_reexports_are_canonical_common_objects(self):
        from hsync_common.models import tombstone as common_tombstone

        from hsync_client.models import TombstoneStore, tombstone_key

        assert TombstoneStore is common_tombstone.TombstoneStore
        assert tombstone_key is common_tombstone.tombstone_key
