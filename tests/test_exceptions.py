"""
Tests for hsync_client.exceptions module.
"""

import pytest

from hsync_client.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConnectionError,
    DownloadError,
    FileOperationError,
    FilePermissionError,
    HSyncClientError,
    NetworkError,
    ServerError,
    SyncError,
)


class TestHSyncClientError:
    """Tests for the base HSyncClientError exception."""

    def test_default_message(self):
        """Test exception with default message."""
        exc = HSyncClientError()
        assert exc.message == "An unexpected error occurred"
        assert exc.error_code == "HSYNC-C5000"
        assert exc.details == {}
        assert str(exc) == "An unexpected error occurred"

    def test_custom_message(self):
        """Test exception with custom message."""
        exc = HSyncClientError(message="Custom error message")
        assert exc.message == "Custom error message"
        assert str(exc) == "Custom error message"

    def test_with_details(self):
        """Test exception with details."""
        details = {"key": "value", "count": 42}
        exc = HSyncClientError(message="Error", details=details)
        assert exc.details == details

    def test_raise_and_catch(self):
        """Test raising and catching the exception."""
        with pytest.raises(HSyncClientError) as exc_info:
            raise HSyncClientError(message="Test error")
        assert "Test error" in str(exc_info.value)


class TestConfigurationErrors:
    """Tests for configuration-related exceptions."""

    def test_configuration_error(self):
        """Test ConfigurationError."""
        exc = ConfigurationError()
        assert exc.error_code == "HSYNC-C1000"
        assert exc.message == "Configuration error"
        assert isinstance(exc, HSyncClientError)


class TestNetworkErrors:
    """Tests for network-related exceptions."""

    def test_network_error(self):
        """Test NetworkError."""
        exc = NetworkError()
        assert exc.error_code == "HSYNC-C2000"
        assert exc.message == "Network error"
        assert isinstance(exc, HSyncClientError)

    def test_connection_error(self):
        """Test ConnectionError."""
        exc = ConnectionError(details={"host": "localhost", "port": 8080})
        assert exc.error_code == "HSYNC-C2001"
        assert exc.message == "Connection failed"
        assert isinstance(exc, NetworkError)

    def test_authentication_error(self):
        """Test AuthenticationError."""
        exc = AuthenticationError(message="Invalid API key")
        assert exc.error_code == "HSYNC-C2002"
        assert exc.message == "Invalid API key"
        assert isinstance(exc, NetworkError)

    def test_server_error(self):
        """Test ServerError."""
        exc = ServerError(details={"status_code": 500})
        assert exc.error_code == "HSYNC-C2004"
        assert exc.message == "Server error"
        assert isinstance(exc, NetworkError)


class TestSyncErrors:
    """Tests for sync operation exceptions."""

    def test_sync_error(self):
        """Test SyncError."""
        exc = SyncError()
        assert exc.error_code == "HSYNC-C3000"
        assert exc.message == "Sync error"
        assert isinstance(exc, HSyncClientError)

    def test_download_error(self):
        """Test DownloadError."""
        exc = DownloadError()
        assert exc.error_code == "HSYNC-C3002"
        assert exc.message == "Download failed"
        assert isinstance(exc, SyncError)

    def test_file_operation_error(self):
        """Test FileOperationError."""
        exc = FileOperationError()
        assert exc.error_code == "HSYNC-C4000"
        assert exc.message == "File operation failed"
        assert isinstance(exc, HSyncClientError)

    def test_permission_error(self):
        """Test FilePermissionError."""
        exc = FilePermissionError()
        assert exc.error_code == "HSYNC-C4003"
        assert exc.message == "Permission denied"
        assert isinstance(exc, FileOperationError)


class TestExceptionHierarchy:
    """Tests for exception inheritance hierarchy."""

    def test_all_inherit_from_base(self):
        """Test that all exceptions inherit from HSyncClientError."""
        exceptions = [
            ConfigurationError,
            NetworkError,
            ConnectionError,
            AuthenticationError,
            ServerError,
            SyncError,
            DownloadError,
            FileOperationError,
            FilePermissionError,
        ]
        for exc_class in exceptions:
            assert issubclass(exc_class, HSyncClientError)

    def test_catch_by_parent_class(self):
        """Test catching specific exceptions by parent class."""
        # Catch NetworkError subclasses
        with pytest.raises(NetworkError):
            raise AuthenticationError()

        # Catch SyncError subclasses
        with pytest.raises(SyncError):
            raise DownloadError()

        # Catch FileOperationError subclasses
        with pytest.raises(FileOperationError):
            raise FilePermissionError()

        # Catch all by base class
        with pytest.raises(HSyncClientError):
            raise ConfigurationError()
