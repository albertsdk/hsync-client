"""
Tests for LAN embedded HTTP server endpoints.
"""

import time
from unittest.mock import patch

import pytest

from hsync_client.lan import get_free_port
from hsync_client.lan_server import LANServer


class TestLANRequestHandlerDELETE:
    """Tests for DELETE endpoint."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""

        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_delete_file(self, server, tmp_path):
        """Test deleting a file via DELETE endpoint."""
        import requests

        srv, port = server

        # Create test file
        test_file = tmp_path / "to_delete.txt"
        test_file.write_text("content to delete")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.delete(url, params={"path": "to_delete.txt"}, timeout=5)

            assert response.status_code == 200
            assert not test_file.exists()

    def test_delete_directory(self, server, tmp_path):
        """Test deleting a directory via DELETE endpoint."""
        import requests

        srv, port = server

        # Create test directory with file
        test_dir = tmp_path / "to_delete_dir"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("content")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.delete(
                url, params={"path": "to_delete_dir", "is_dir": "true"}, timeout=5
            )

            assert response.status_code == 200
            assert not test_dir.exists()

    def test_delete_nonexistent_file(self, server, tmp_path):
        """Test deleting a non-existent file returns success."""
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
            response = requests.delete(url, params={"path": "nonexistent.txt"}, timeout=5)

            assert response.status_code == 200

    def test_delete_without_path_param(self, server, tmp_path):
        """Test DELETE without path parameter returns error."""
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
            response = requests.delete(url, timeout=5)

            assert response.status_code == 400


class TestLANRequestHandlerPATCH:
    """Tests for PATCH (rename) endpoint."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""

        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_rename_file(self, server, tmp_path):
        """Test renaming a file via PATCH endpoint."""
        import requests

        srv, port = server

        # Create test file
        test_file = tmp_path / "old_name.txt"
        test_file.write_text("content")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.patch(
                url, params={"src": "old_name.txt", "dst": "new_name.txt"}, timeout=5
            )

            assert response.status_code == 200
            assert not test_file.exists()
            assert (tmp_path / "new_name.txt").exists()

    def test_rename_directory(self, server, tmp_path):
        """Test renaming a directory via PATCH endpoint."""
        import requests

        srv, port = server

        # Create test directory
        test_dir = tmp_path / "old_dir"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("content")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.patch(url, params={"src": "old_dir", "dst": "new_dir"}, timeout=5)

            assert response.status_code == 200
            assert not test_dir.exists()
            assert (tmp_path / "new_dir").exists()

    def test_rename_nonexistent_file(self, server, tmp_path):
        """Test renaming a non-existent file returns error."""
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
                url, params={"src": "nonexistent.txt", "dst": "new.txt"}, timeout=5
            )

            assert response.status_code == 404

    def test_rename_without_params(self, server, tmp_path):
        """Test rename without src/dst params returns error."""
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
            response = requests.patch(url, timeout=5)

            assert response.status_code == 400
