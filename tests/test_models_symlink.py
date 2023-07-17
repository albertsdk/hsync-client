"""
Tests for Symlink model.
"""

import os
import tempfile

import pytest

from hsync_client.models.symlink import Symlink


class TestSymlinkModel:
    """Tests for Symlink class."""

    def test_init_basic(self):
        """Test basic initialization."""
        symlink = Symlink(filepath="/path/to/link", target="../target")
        assert symlink.filepath == "/path/to/link"
        assert symlink.name == "link"
        assert symlink.target == "../target"

    def test_init_with_all_fields(self):
        """Test initialization with all fields."""
        symlink = Symlink(
            filepath="/path/to/link",
            target="../target",
            name="mylink",
            ctime=1705320600.0,
            mtime=1705320700.0,
        )
        assert symlink.name == "mylink"
        assert symlink.ctime == 1705320600.0
        assert symlink.mtime == 1705320700.0

    def test_to_dict(self):
        """Test conversion to dictionary."""
        symlink = Symlink(
            filepath="/path/to/link",
            target="../target",
            ctime=1705320600.0,
            mtime=1705320700.0,
        )
        d = symlink.to_dict()
        
        assert d["type"] == "symlink"
        assert d["filepath"] == "/path/to/link"
        assert d["name"] == "link"
        assert d["target"] == "../target"
        assert d["ctime"] == 1705320600.0
        assert d["mtime"] == 1705320700.0

    def test_from_dict(self):
        """Test creation from dictionary."""
        data = {
            "filepath": "/path/to/link",
            "name": "link",
            "target": "../target",
            "ctime": 1705320600.0,
            "mtime": 1705320700.0,
        }
        symlink = Symlink.from_dict(data)
        
        assert symlink.filepath == "/path/to/link"
        assert symlink.name == "link"
        assert symlink.target == "../target"

    def test_repr(self):
        """Test string representation."""
        symlink = Symlink(filepath="/test/link", target="target")
        repr_str = repr(symlink)
        
        assert "Symlink" in repr_str
        assert "/test/link" in repr_str
        assert "target" in repr_str

    def test_get_existing_symlink(self):
        """Test Symlink.get with real symlink."""
        with tempfile.TemporaryDirectory() as temp_dir:
            target_file = os.path.join(temp_dir, "target.txt")
            link_path = os.path.join(temp_dir, "link")
            
            # Create target file
            with open(target_file, "w") as f:
                f.write("target content")
            
            # Create symlink
            os.symlink(target_file, link_path)
            
            symlink = Symlink.get(link_path)
            assert symlink is not None
            assert symlink.filepath == link_path
            assert symlink.name == "link"
            assert symlink.target == target_file
            assert symlink.ctime is not None
            assert symlink.mtime is not None

    def test_get_relative_symlink(self):
        """Test Symlink.get with relative target."""
        with tempfile.TemporaryDirectory() as temp_dir:
            target_file = os.path.join(temp_dir, "target.txt")
            link_path = os.path.join(temp_dir, "link")
            
            with open(target_file, "w") as f:
                f.write("content")
            
            # Create symlink with relative target
            os.symlink("target.txt", link_path)
            
            symlink = Symlink.get(link_path)
            assert symlink is not None
            assert symlink.target == "target.txt"

    def test_get_nonexistent_path(self):
        """Test Symlink.get with non-existent path."""
        symlink = Symlink.get("/nonexistent/path")
        assert symlink is None

    def test_get_regular_file(self):
        """Test Symlink.get with regular file (not symlink)."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            temp_path = f.name
        
        try:
            symlink = Symlink.get(temp_path)
            assert symlink is None  # Regular file, not a symlink
        finally:
            os.unlink(temp_path)

    def test_get_broken_symlink(self):
        """Test Symlink.get with broken symlink (target deleted)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            target_file = os.path.join(temp_dir, "target.txt")
            link_path = os.path.join(temp_dir, "broken_link")
            
            # Create target and symlink
            with open(target_file, "w") as f:
                f.write("content")
            os.symlink(target_file, link_path)
            
            # Delete target to break the symlink
            os.unlink(target_file)
            
            # Symlink.get should still work for broken symlinks
            symlink = Symlink.get(link_path)
            assert symlink is not None
            assert symlink.target == target_file

