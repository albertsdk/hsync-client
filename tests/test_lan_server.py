"""
Tests for LAN embedded HTTP server.
"""

import json
import time
import threading
from http.client import HTTPConnection
from unittest.mock import patch, MagicMock

import pytest

from hsync_client.lan_server import LANServer, LANRequestHandler


class TestLANServer:
    """Tests for LANServer class."""

    def test_server_creation(self):
        """Test creating a server instance."""
        server = LANServer(port=9999)
        assert server.port == 9999
        assert server.is_running() is False

    def test_server_start_stop(self):
        """Test starting and stopping the server."""
        server = LANServer(port=0)  # Use random port
        server.start()
        assert server.is_running() is True
        time.sleep(0.1)  # Give server time to start
        server.stop()
        assert server.is_running() is False


class TestLANRequestHandler:
    """Tests for LANRequestHandler endpoints."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""
        from hsync_client.lan import get_free_port
        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)  # Give server time to start
        yield server, port
        server.stop()

    def test_health_endpoint(self, server, tmp_path):
        """Test health check endpoint."""
        srv, port = server

        with patch("hsync_client.lan_server.CONFIG", {
            "LOCAL_BUCKET": str(tmp_path),
            "LAN_KEY": None,
        }):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/health")
            response = conn.getresponse()

            assert response.status == 200
            data = json.loads(response.read().decode())
            assert data["status"] == "ok"
            assert data["mode"] == "lan_master"
            conn.close()

    def test_health_endpoint_with_auth(self, server, tmp_path):
        """Test health check with authentication."""
        srv, port = server

        with patch("hsync_client.lan_server.CONFIG", {
            "LOCAL_BUCKET": str(tmp_path),
            "LAN_KEY": "secret123",
        }):
            # Without auth - should fail
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/health")
            response = conn.getresponse()
            assert response.status == 401
            conn.close()

            # With correct auth - should succeed
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/health", headers={
                "Authorization": "Bearer secret123"
            })
            response = conn.getresponse()
            assert response.status == 200
            conn.close()

            # With wrong auth - should fail
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/health", headers={
                "Authorization": "Bearer wrongkey"
            })
            response = conn.getresponse()
            assert response.status == 401
            conn.close()

    def test_list_files_endpoint(self, server, tmp_path):
        """Test list files endpoint."""
        srv, port = server

        # Create test files
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "file2.txt").write_text("content2")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "file3.txt").write_text("content3")

        with patch("hsync_client.lan_server.CONFIG", {
            "LOCAL_BUCKET": str(tmp_path),
            "LAN_KEY": None,
        }):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/buckets/test/files")
            response = conn.getresponse()

            assert response.status == 200
            data = json.loads(response.read().decode())
            assert isinstance(data, list)
            assert len(data) == 3
            conn.close()

    def test_download_file_endpoint(self, server, tmp_path):
        """Test file download endpoint."""
        srv, port = server

        # Create test file
        test_file = tmp_path / "download_test.txt"
        test_file.write_text("download content")

        with patch("hsync_client.lan_server.CONFIG", {
            "LOCAL_BUCKET": str(tmp_path),
            "LAN_KEY": None,
        }):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/buckets/test/files/download?filename=download_test.txt")
            response = conn.getresponse()

            assert response.status == 200
            content = response.read().decode()
            assert content == "download content"
            conn.close()

    def test_download_file_not_found(self, server, tmp_path):
        """Test download of non-existent file."""
        srv, port = server

        with patch("hsync_client.lan_server.CONFIG", {
            "LOCAL_BUCKET": str(tmp_path),
            "LAN_KEY": None,
        }):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/buckets/test/files/download?filename=nonexistent.txt")
            response = conn.getresponse()

            assert response.status == 404
            conn.close()

    def test_not_found_endpoint(self, server, tmp_path):
        """Test 404 for unknown endpoints."""
        srv, port = server

        with patch("hsync_client.lan_server.CONFIG", {
            "LOCAL_BUCKET": str(tmp_path),
            "LAN_KEY": None,
        }):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/unknown/path")
            response = conn.getresponse()

            assert response.status == 404
            conn.close()

