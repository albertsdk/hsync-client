"""
Tests for CLI validator functions in hsync_client.validators module.
"""

from argparse import ArgumentTypeError

import pytest

from hsync_client.validators import (
    valid_max_wait_timeout,
    valid_reconnect_delay,
    valid_resync,
    valid_bucket,
)


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
        with pytest.raises(ArgumentTypeError, match="must be an integer"):
            valid_reconnect_delay("abc")
        with pytest.raises(ArgumentTypeError, match="must be an integer"):
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
        with pytest.raises(ArgumentTypeError, match="must be an integer"):
            valid_resync("abc")


class TestValidBucket:
    """Tests for valid_bucket function."""

    def test_valid_alphanumeric(self):
        """Test valid alphanumeric bucket names."""
        assert valid_bucket("bucket123") == "bucket123"
        assert valid_bucket("MYBUCKET") == "MYBUCKET"
        assert valid_bucket("test") == "test"
        assert valid_bucket("123") == "123"

    def test_valid_with_hyphens_and_underscores(self):
        """Test bucket names with hyphens and underscores are accepted."""
        assert valid_bucket("bucket-name") == "bucket-name"
        assert valid_bucket("bucket_name") == "bucket_name"
        assert valid_bucket("my-bucket-123") == "my-bucket-123"
        assert valid_bucket("my_bucket_123") == "my_bucket_123"
        assert valid_bucket("a-b") == "a-b"
        assert valid_bucket("a_b") == "a_b"

    def test_invalid_special_chars(self):
        """Test bucket names with special characters are rejected."""
        with pytest.raises(ArgumentTypeError, match="must be alphanumeric"):
            valid_bucket("bucket.name")
        with pytest.raises(ArgumentTypeError, match="must be alphanumeric"):
            valid_bucket("bucket@name")
        with pytest.raises(ArgumentTypeError, match="must be alphanumeric"):
            valid_bucket("bucket/name")

    def test_invalid_start_end_with_hyphen_or_underscore(self):
        """Test bucket names starting/ending with hyphen or underscore are rejected."""
        with pytest.raises(ArgumentTypeError, match="must be alphanumeric"):
            valid_bucket("-bucket")
        with pytest.raises(ArgumentTypeError, match="must be alphanumeric"):
            valid_bucket("bucket-")
        with pytest.raises(ArgumentTypeError, match="must be alphanumeric"):
            valid_bucket("_bucket")
        with pytest.raises(ArgumentTypeError, match="must be alphanumeric"):
            valid_bucket("bucket_")

    def test_invalid_with_spaces(self):
        """Test bucket names with spaces are rejected."""
        with pytest.raises(ArgumentTypeError, match="must be alphanumeric"):
            valid_bucket("my bucket")


class TestValidMaxWaitTimeout:
    """Tests for valid_max_wait_timeout function."""

    def test_valid_value(self):
        """Test valid max wait timeout values."""
        assert valid_max_wait_timeout("60") == 60
        assert valid_max_wait_timeout("120") == 120
        assert valid_max_wait_timeout("3599") == 3599

    def test_boundary_too_low(self):
        """Test value below lower boundary is rejected."""
        with pytest.raises(ArgumentTypeError, match="must be between 9 and 3601"):
            valid_max_wait_timeout("9")
        with pytest.raises(ArgumentTypeError, match="must be between 9 and 3601"):
            valid_max_wait_timeout("5")

    def test_boundary_too_high(self):
        """Test value above upper boundary is rejected."""
        with pytest.raises(ArgumentTypeError, match="must be between 9 and 3601"):
            valid_max_wait_timeout("3601")
        with pytest.raises(ArgumentTypeError, match="must be between 9 and 3601"):
            valid_max_wait_timeout("4000")

    def test_non_integer(self):
        """Test non-integer value is rejected."""
        with pytest.raises(ArgumentTypeError, match="must be an integer"):
            valid_max_wait_timeout("abc")
        with pytest.raises(ArgumentTypeError, match="must be an integer"):
            valid_max_wait_timeout("120.5")
