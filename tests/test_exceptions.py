"""
Tests for hsync_client.exceptions module.
"""

import pytest

from hsync_client.exceptions import (
    HSyncClientError,
    ConfigurationError,
    InvalidLocalPathError,
    InvalidServerUrlError,
    InvalidBucketNameError,
    NetworkError,
    ConnectionError,
    AuthenticationError,
    WebSocketError,
    ServerError,
    SyncError,
    UploadError,
    DownloadError,
    ChecksumError,
    FileOperationError,
    FileReadError,
    FileWriteError,
    PermissionError,
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

    def test_invalid_local_path_error(self):
        """Test InvalidLocalPathError."""
        exc = InvalidLocalPathError(details={"path": "/invalid/path"})
        assert exc.error_code == "HSYNC-C1001"
        assert exc.message == "Invalid local path"
        assert exc.details == {"path": "/invalid/path"}
        assert isinstance(exc, ConfigurationError)

    def test_invalid_server_url_error(self):
        """Test InvalidServerUrlError."""
        exc = InvalidServerUrlError(message="URL must start with http")
        assert exc.error_code == "HSYNC-C1002"
        assert exc.message == "URL must start with http"
        assert isinstance(exc, ConfigurationError)

    def test_invalid_bucket_name_error(self):
        """Test InvalidBucketNameError."""
        exc = InvalidBucketNameError()
        assert exc.error_code == "HSYNC-C1003"
        assert exc.message == "Invalid bucket name"
        assert isinstance(exc, ConfigurationError)


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

    def test_websocket_error(self):
        """Test WebSocketError."""
        exc = WebSocketError()
        assert exc.error_code == "HSYNC-C2003"
        assert exc.message == "WebSocket error"
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

    def test_upload_error(self):
        """Test UploadError."""
        exc = UploadError(message="Failed to upload file.txt")
        assert exc.error_code == "HSYNC-C3001"
        assert exc.message == "Failed to upload file.txt"
        assert isinstance(exc, SyncError)

    def test_download_error(self):
        """Test DownloadError."""
        exc = DownloadError()
        assert exc.error_code == "HSYNC-C3002"
        assert exc.message == "Download failed"
        assert isinstance(exc, SyncError)

    def test_checksum_error(self):
        """Test ChecksumError."""
        exc = ChecksumError(details={"expected": "abc123", "actual": "def456"})
        assert exc.error_code == "HSYNC-C3003"
        assert exc.message == "Checksum mismatch"
        assert isinstance(exc, SyncError)


class TestFileOperationErrors:
    """Tests for file operation exceptions."""

    def test_file_operation_error(self):
        """Test FileOperationError."""
        exc = FileOperationError()
        assert exc.error_code == "HSYNC-C4000"
        assert exc.message == "File operation failed"
        assert isinstance(exc, HSyncClientError)

    def test_file_read_error(self):
        """Test FileReadError."""
        exc = FileReadError(details={"file": "/path/to/file.txt"})
        assert exc.error_code == "HSYNC-C4001"
        assert exc.message == "File read failed"
        assert isinstance(exc, FileOperationError)

    def test_file_write_error(self):
        """Test FileWriteError."""
        exc = FileWriteError(message="Disk full")
        assert exc.error_code == "HSYNC-C4002"
        assert exc.message == "Disk full"
        assert isinstance(exc, FileOperationError)

    def test_permission_error(self):
        """Test PermissionError."""
        exc = PermissionError()
        assert exc.error_code == "HSYNC-C4003"
        assert exc.message == "Permission denied"
        assert isinstance(exc, FileOperationError)


class TestExceptionHierarchy:
    """Tests for exception inheritance hierarchy."""

    def test_all_inherit_from_base(self):
        """Test that all exceptions inherit from HSyncClientError."""
        exceptions = [
            ConfigurationError,
            InvalidLocalPathError,
            InvalidServerUrlError,
            InvalidBucketNameError,
            NetworkError,
            ConnectionError,
            AuthenticationError,
            WebSocketError,
            ServerError,
            SyncError,
            UploadError,
            DownloadError,
            ChecksumError,
            FileOperationError,
            FileReadError,
            FileWriteError,
            PermissionError,
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
            raise UploadError()

        # Catch FileOperationError subclasses
        with pytest.raises(FileOperationError):
            raise FileReadError()

        # Catch all by base class
        with pytest.raises(HSyncClientError):
            raise InvalidBucketNameError()

