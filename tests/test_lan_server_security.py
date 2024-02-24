"""
Tests for LAN embedded HTTP server security.
"""

import time
from unittest.mock import patch

import pytest

from hsync_client.lan_server import LANServer


class TestLANRequestHandlerDownloadSecurity:
    """Tests for download security checks."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""
        from hsync_client.lan import get_free_port

        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_download_path_traversal_blocked(self, server, tmp_path):
        """Test that path traversal attempts are blocked."""

        import requests

        srv, port = server

        # Create test file in bucket
        test_file = tmp_path / "safe.txt"
        test_file.write_text("safe content")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            # Try path traversal with a file that exists outside bucket
            # Use a relative path that gets resolved outside bucket
            url = f"http://localhost:{port}/api/v1/buckets/test/files/download"
            # Try to access a file outside bucket via traversal
            # The path normalization will result in path outside bucket
            response = requests.get(url, params={"filename": "../../etc/hostname"}, timeout=5)

            # Should be forbidden (403) or not found (404) depending on path resolution
            assert response.status_code in (403, 404)


class TestLANRequestHandlerUploadSecurity:
    """Tests for upload security checks."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""
        from hsync_client.lan import get_free_port

        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_upload_path_traversal_blocked(self, server, tmp_path):
        """Test that path traversal in uploads is blocked."""
        import requests

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            # Try path traversal
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {
                "cid": "test",
                "path": "../",
                "mtime": "",
            }
            files = {"file": ("traversal.txt", b"content")}

            response = requests.post(url, data=data, files=files, timeout=5)

            # Should be forbidden
            assert response.status_code == 403


class TestLANRequestHandlerDeleteSecurity:
    """Tests for delete security checks."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""
        from hsync_client.lan import get_free_port

        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_delete_path_traversal_blocked(self, server, tmp_path):
        """Test that path traversal in deletes is blocked."""
        import requests

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.delete(url, params={"path": "../somefile.txt"}, timeout=5)

            # Should be bad request
            assert response.status_code == 400


class TestLANRequestHandlerRenameSecurity:
    """Tests for rename security checks."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""
        from hsync_client.lan import get_free_port

        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_rename_path_traversal_blocked(self, server, tmp_path):
        """Test that path traversal in renames is blocked."""
        import requests

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.patch(
                url, params={"src": "safe.txt", "dst": "../malicious.txt"}, timeout=5
            )

            # Should be bad request
            assert response.status_code == 400


class TestLANRequestHandlerBucketNotConfigured:
    """Tests for bucket not configured scenarios."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""
        from hsync_client.lan import get_free_port

        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_list_files_no_bucket_configured(self, server):
        """Test list files when bucket is not configured."""
        import requests

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": "",
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.get(url, timeout=5)

            assert response.status_code == 404

    def test_download_no_bucket_configured(self, server):
        """Test download when bucket is not configured."""
        import requests

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": "",
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files/download"
            response = requests.get(url, params={"filename": "test.txt"}, timeout=5)

            assert response.status_code == 404
