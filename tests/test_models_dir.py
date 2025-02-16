"""
Tests for Dir model.
"""

import os
import tempfile

from hsync_common.models.dir import Dir, is_hidden_dir


class TestIsHiddenDir:
    """Tests for is_hidden_dir function.

    Following the industry standard (Syncthing, rsync, Nextcloud, OneDrive),
    dotfile directories in general (.git/, .config/ …) are NOT excluded:
    only OS cache directories and editor/office temporary files are filtered
    by default.
    """

    def test_dotdir_not_hidden(self):
        """Dotfile directories like .git, .config are synced normally."""
        assert is_hidden_dir("/.git/objects") is False
        assert is_hidden_dir("/path/.hidden/subdir") is False
        assert is_hidden_dir("/home/user/.config/test") is False

    def test_regular_dir(self):
        """Regular directories are not hidden."""
        assert is_hidden_dir("/path/to/dir") is False
        assert is_hidden_dir("/regular/path") is False

    def test_internal_dir_is_hidden(self):
        """Engine-owned directories are always hidden."""
        assert is_hidden_dir(".hsyncignore") is True
        assert is_hidden_dir(".hsyncstate") is True
        assert is_hidden_dir(".hsynctombstones") is True


class TestDirModel:
    """Tests for Dir class."""

    def test_init_basic(self):
        """Test basic initialization."""
        d = Dir(path="/path/to/dir", permissions="755")
        assert d.path == "/path/to/dir"
        assert d.permissions == "755"

    def test_to_dict(self):
        """Test conversion to dictionary."""
        d = Dir(path="/path/to/dir", permissions="755")
        result = d.to_dict()
        assert result["path"] == "/path/to/dir"
        assert result["permissions"] == "755"

    def test_repr(self):
        """Test string representation."""
        d = Dir(path="/test", permissions="755")
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
            assert d.permissions.isdigit()

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
        data = {"path": "/bucket/subdir", "permissions": "755"}
        d = Dir.from_dict(data, localpath="/bucket", strip_localpath=True)
        assert d.path == "subdir"
        assert d.permissions == "755"

    def test_from_dict_no_strip(self):
        """Test Dir.from_dict without stripping localpath."""
        data = {"path": "subdir", "permissions": "755"}
        d = Dir.from_dict(data, localpath="/bucket", strip_localpath=False)
        assert d.path == "/bucket/subdir"

    def test_get_permissions(self):
        """Test _get_permissions with real directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            perms = Dir._get_permissions(temp_dir)
            assert perms.isdigit()
            assert len(perms) == 3


class TestTombstoneReexports:
    """Tombstone symbols are importable from hsync_common.models."""

    def test_tombstone_symbols_importable_from_package(self):
        from hsync_common.models import (
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
        from hsync_common.models import TombstoneStore, tombstone_key
        from hsync_common.models import tombstone as common_tombstone

        assert TombstoneStore is common_tombstone.TombstoneStore
        assert tombstone_key is common_tombstone.tombstone_key
