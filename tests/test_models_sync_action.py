"""
Tests for SyncAction model.
"""

import pytest
from hsync_common.models.file import File
from hsync_common.models.sync_action import SyncAction, SyncActionName


class TestSyncActionName:
    """Tests for SyncActionName enum."""

    def test_enum_values(self):
        """Verify enum values."""
        assert SyncActionName.download.value == "download"
        assert SyncActionName.upload.value == "upload"
        assert SyncActionName.nothing.value == "nothing"

    def test_enum_from_string(self):
        """Create enum from string value."""
        assert SyncActionName("download") == SyncActionName.download
        assert SyncActionName("upload") == SyncActionName.upload
        assert SyncActionName("nothing") == SyncActionName.nothing


class TestSyncAction:
    """Tests for SyncAction class."""

    @pytest.fixture
    def sample_file(self):
        """Create a sample File for testing."""
        return File(
            filepath="/bucket/test.txt",
            size=100,
            sha256sum="abc123",
            permissions="644",
        )

    def test_init_basic(self, sample_file):
        """Initialize SyncAction with basic parameters."""
        action = SyncAction(file=sample_file, name=SyncActionName.upload)
        assert action.file == sample_file
        assert action.name == SyncActionName.upload
        assert action.timedelta == "0s"

    def test_init_with_timedelta(self, sample_file):
        """Initialize SyncAction with time delta."""
        action = SyncAction(
            file=sample_file,
            name=SyncActionName.download,
            timedelta=3661,  # 1h 1m 1s
        )
        assert action.name == SyncActionName.download
        assert action.timedelta == "1h 1m 1s"

    def test_to_dict(self, sample_file):
        """Convert SyncAction to dictionary."""
        action = SyncAction(file=sample_file, name=SyncActionName.upload, timedelta=60)
        result = action.to_dict()
        assert result["name"] == SyncActionName.upload
        assert result["timedelta"] == "1m"
        assert "file" in result
        assert result["file"]["filepath"] == "/bucket/test.txt"

    def test_repr(self, sample_file):
        """Test string representation."""
        action = SyncAction(file=sample_file, name=SyncActionName.nothing)
        repr_str = repr(action)
        assert "SyncAction" in repr_str
        assert "nothing" in repr_str


class TestFormatTimedelta:
    """Tests for _format_timedelta method."""

    @pytest.fixture
    def sample_file(self):
        """Create a sample File for testing."""
        return File(filepath="/test.txt", size=0, sha256sum="", permissions="644")

    def test_zero_seconds(self, sample_file):
        """Zero seconds returns '0s'."""
        action = SyncAction(file=sample_file, timedelta=0)
        assert action.timedelta == "0s"

    def test_only_seconds(self, sample_file):
        """Only seconds."""
        action = SyncAction(file=sample_file, timedelta=45)
        assert action.timedelta == "45s"

    def test_only_minutes(self, sample_file):
        """Only minutes (no seconds)."""
        action = SyncAction(file=sample_file, timedelta=120)
        assert action.timedelta == "2m"

    def test_only_hours(self, sample_file):
        """Only hours."""
        action = SyncAction(file=sample_file, timedelta=7200)
        assert action.timedelta == "2h"

    def test_only_days(self, sample_file):
        """Only days."""
        action = SyncAction(file=sample_file, timedelta=86400)
        assert action.timedelta == "1d"

    def test_days_hours_minutes_seconds(self, sample_file):
        """Full format with all components."""
        # 2 days + 3 hours + 15 minutes + 30 seconds
        delta = 2 * 86400 + 3 * 3600 + 15 * 60 + 30
        action = SyncAction(file=sample_file, timedelta=delta)
        assert action.timedelta == "2d 3h 15m 30s"

    def test_hours_and_seconds(self, sample_file):
        """Hours and seconds (no minutes)."""
        action = SyncAction(file=sample_file, timedelta=3605)  # 1h 5s
        assert action.timedelta == "1h 5s"

    def test_days_and_minutes(self, sample_file):
        """Days and minutes (no hours or seconds)."""
        action = SyncAction(file=sample_file, timedelta=86400 + 300)  # 1d 5m
        assert action.timedelta == "1d 5m"
