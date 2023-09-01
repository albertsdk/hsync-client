"""
Tests for Event model.
"""

import pytest

from hsync_client.models.event import Event, EventName
from hsync_client.models.file import File
from hsync_client.models.dir import Dir
from hsync_client.models.symlink import Symlink


class TestEventName:
    """Tests for EventName enum."""

    def test_event_values(self):
        """Test enum values are correct."""
        assert EventName.added.value == "added"
        assert EventName.modified.value == "modified"
        assert EventName.renamed.value == "renamed"
        assert EventName.deleted.value == "deleted"
        assert EventName.symlink.value == "symlink"

    def test_event_from_string(self):
        """Test creating enum from string value."""
        assert EventName("added") == EventName.added
        assert EventName("modified") == EventName.modified


class TestEvent:
    """Tests for Event class."""

    def test_init_with_file(self):
        """Test event creation with File source."""
        file = File(filepath="/local/bucket/file.txt", size=1024)
        event = Event(
            cid="client-123",
            localpath="/local/bucket",
            bucket="mybucket",
            name=EventName.added,
            src=file,
            strip_localpath=True,
        )
        
        assert event.cid == "client-123"
        assert event.bucket == "mybucket"
        assert event.name == "added"
        assert isinstance(event.src, File)
        assert event.src.filepath == "file.txt"  # localpath stripped
        assert event.dst is None

    def test_init_with_dir(self):
        """Test event creation with Dir source."""
        d = Dir(path="/local/bucket/subdir", permissions="drwxr-xr-x")
        event = Event(
            cid="client-123",
            localpath="/local/bucket",
            bucket="mybucket",
            name=EventName.added,
            src=d,
            strip_localpath=True,
        )
        
        assert event.src.path == "subdir"

    def test_init_without_strip(self):
        """Test event creation without stripping localpath."""
        file = File(filepath="file.txt", size=1024)
        event = Event(
            cid="client-123",
            localpath="/local/bucket",
            bucket="mybucket",
            name=EventName.added,
            src=file,
            strip_localpath=False,
        )
        
        assert isinstance(event.src, File)
        assert event.src.filepath == "/local/bucket/file.txt"

    def test_rename_event(self):
        """Test rename event with source and destination."""
        src_file = File(filepath="/local/bucket/old.txt", size=1024)
        dst_file = File(filepath="/local/bucket/new.txt", size=1024)
        
        event = Event(
            cid="client-123",
            localpath="/local/bucket",
            bucket="mybucket",
            name=EventName.renamed,
            src=src_file,
            dst=dst_file,
            strip_localpath=True,
        )
        
        assert isinstance(event.src, File)
        assert isinstance(event.dst, File)
        assert event.src.filepath == "old.txt"
        assert event.dst.filepath == "new.txt"

    def test_to_dict(self):
        """Test serialization to dictionary."""
        file = File(filepath="/local/bucket/file.txt", size=1024)
        event = Event(
            cid="client-123",
            localpath="/local/bucket",
            bucket="mybucket",
            name=EventName.modified,
            src=file,
            strip_localpath=True,
        )
        
        d = event.to_dict()
        assert d["cid"] == "client-123"
        assert d["name"] == "modified"
        assert d["bucket"] == "mybucket"
        assert d["src"]["filepath"] == "file.txt"
        assert d["dst"] is None

    def test_to_dict_with_dst(self):
        """Test serialization with destination."""
        src = Dir(path="/local/bucket/old_dir", permissions="drwxr-xr-x")
        dst = Dir(path="/local/bucket/new_dir", permissions="drwxr-xr-x")
        
        event = Event(
            cid="client-123",
            localpath="/local/bucket",
            bucket="mybucket",
            name=EventName.renamed,
            src=src,
            dst=dst,
            strip_localpath=True,
        )
        
        d = event.to_dict()
        assert d["src"]["path"] == "old_dir"
        assert d["dst"]["path"] == "new_dir"

    def test_symlink_event(self):
        """Test symlink event type."""
        symlink = Symlink(filepath="/local/bucket/link", target="../target")
        event = Event(
            cid="client-123",
            localpath="/local/bucket",
            bucket="mybucket",
            name=EventName.symlink,
            src=symlink,
            strip_localpath=True,
        )
        
        assert event.name == "symlink"
        d = event.to_dict()
        assert d["src"]["type"] == "symlink"
        assert d["src"]["target"] == "../target"

    def test_repr(self):
        """Test string representation."""
        file = File(filepath="test.txt", size=100)
        event = Event(
            cid="c1",
            localpath="/",
            bucket="b1",
            name=EventName.added,
            src=file,
        )
        
        repr_str = repr(event)
        assert "Event" in repr_str
        assert "c1" in repr_str
        assert "added" in repr_str

