"""
Tests for LAN embedded HTTP server endpoints.
"""

import json
import os
import time
from hashlib import sha256 as _sha256
from http.client import HTTPConnection
from unittest.mock import patch

import pytest
from hsync_common.models.tombstone import tombstone_key

from hsync_client import tombstones as ts_mod
from hsync_client.lan import get_free_port
from hsync_client.lan_server import LANServer


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

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
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

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": "secret123",
            },
        ):
            # Without auth - should fail
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/health")
            response = conn.getresponse()
            assert response.status == 401
            conn.close()

            # With correct auth - should succeed
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/health", headers={"Authorization": "Bearer secret123"})
            response = conn.getresponse()
            assert response.status == 200
            conn.close()

            # With wrong auth - should fail
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/health", headers={"Authorization": "Bearer wrongkey"})
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

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
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

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
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

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/buckets/test/files/download?filename=nonexistent.txt")
            response = conn.getresponse()

            assert response.status == 404
            conn.close()

    def test_not_found_endpoint(self, server, tmp_path):
        """Test 404 for unknown endpoints."""
        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/unknown/path")
            response = conn.getresponse()

            assert response.status == 404
            conn.close()

    def test_upload_file_endpoint(self, server, tmp_path):
        """Test file upload endpoint using requests (like transfer.py)."""
        import requests

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            # Simulate upload like transfer.py does
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {
                "cid": "test-client-id",
                "path": "subdir",
                "ctime": "1700000000.0",
                "mtime": "1700000001.0",
                "size": "12",
                "sha256sum": _sha256(b"test content").hexdigest(),
                "permissions": "644",
            }
            files = {"file": ("uploaded.txt", b"test content")}

            response = requests.post(url, data=data, files=files, timeout=5)

            assert response.status_code == 201
            assert "File uploaded successfully" in response.json()["info"]

            # Verify file was created
            uploaded_file = tmp_path / "subdir" / "uploaded.txt"
            assert uploaded_file.exists()
            assert uploaded_file.read_text() == "test content"

    def test_upload_file_with_auth(self, server, tmp_path):
        """Test file upload with authentication."""
        import requests

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": "secret123",
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {
                "cid": "test",
                "path": "",
                "mtime": "",
                "ctime": "",
                "sha256sum": _sha256(b"content").hexdigest(),
            }
            files = {"file": ("test.txt", b"content")}

            # Without auth - should fail
            response = requests.post(url, data=data, files=files, timeout=5)
            assert response.status_code == 401

            # With auth - should succeed
            headers = {"Authorization": "Bearer secret123"}
            response = requests.post(url, data=data, files=files, headers=headers, timeout=5)
            assert response.status_code == 201


class TestLANSyncIntegration:
    """Integration tests for LAN sync flow."""

    @pytest.fixture
    def master_server(self, tmp_path):
        """Create a LAN server acting as master with test files."""
        from hsync_client.lan import get_free_port
        from hsync_client.lan_server import LANServer

        # Create master bucket with files
        master_bucket = tmp_path / "master"
        master_bucket.mkdir()
        (master_bucket / "file1.txt").write_text("master content 1")
        (master_bucket / "file2.txt").write_text("master content 2")
        (master_bucket / "subdir").mkdir()
        (master_bucket / "subdir" / "file3.txt").write_text("master content 3")

        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)

        yield server, port, master_bucket

        server.stop()

    def test_client_download_from_master(self, master_server, tmp_path):
        """Test client downloading files from master via sync."""
        import requests

        srv, port, master_bucket = master_server

        # Create client bucket (empty)
        client_bucket = tmp_path / "client"
        client_bucket.mkdir()

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(master_bucket),
                "LAN_KEY": None,
            },
        ):
            # Simulate what sync.get_files_remote() does
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.get(url, timeout=5)
            assert response.status_code == 200
            files = response.json()
            assert len(files) == 3

            # Simulate download for each file
            for file_info in files:
                filepath = file_info["filepath"]
                download_url = f"http://localhost:{port}/api/v1/buckets/test/files/download"
                response = requests.get(download_url, params={"filename": filepath}, timeout=5)
                assert response.status_code == 200

    def test_client_upload_to_master(self, master_server, tmp_path):
        """Test client uploading files to master."""
        import requests

        srv, port, master_bucket = master_server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(master_bucket),
                "LAN_KEY": None,
            },
        ):
            # Simulate upload like transfer.py does
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {
                "cid": "test-client",
                "path": "",
                "mtime": str(time.time()),
                "ctime": str(time.time()),
                "sha256sum": _sha256(b"client content").hexdigest(),
            }
            files = {"file": ("new_from_client.txt", b"client content")}

            response = requests.post(url, data=data, files=files, timeout=5)
            assert response.status_code == 201

            # Verify file exists on master
            assert (master_bucket / "new_from_client.txt").exists()
            assert (master_bucket / "new_from_client.txt").read_text() == "client content"

    def test_streaming_upload_binary_file(self, master_server, tmp_path):
        """Streaming multipart upload of a large binary file succeeds.

        Regression for 'multipart header line too long': the parser's
        _read_line checked the ENTIRE buffer size (which could be 64KB after
        a single _read_more) against the 8KB header-line cap, even when the
        buffer contained valid short CRLF-terminated lines. The fix checks
        the cap only while no CRLF is in the buffer (i.e. a single
        unterminated line is genuinely too long).
        """
        import requests
        from hsync_common.hashing import compute_sha256

        from hsync_client.transfer import _build_multipart_streaming

        srv, port, master_bucket = master_server

        # 500KB of random binary data (like an mp3/flac)
        file_data = os.urandom(500_000)
        test_file = tmp_path / "binary.mp3"
        test_file.write_bytes(file_data)

        sha = compute_sha256(str(test_file))

        with patch(
            "hsync_client.lan_server.CONFIG",
            {"LOCAL_BUCKET": str(master_bucket), "LAN_KEY": None},
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            fields = {
                "cid": "test-client",
                "path": "",
                "ctime": str(time.time()),
                "mtime": str(time.time()),
                "size": str(len(file_data)),
                "sha256sum": sha,
                "permissions": "664",
                "offset": "0",
            }

            boundary = "testboundary123456"
            body, content_type = _build_multipart_streaming(
                str(test_file), fields, boundary=boundary, filename="binary.mp3"
            )

            response = requests.post(
                url,
                headers={"Content-Type": content_type},
                data=body,
                timeout=10,
            )

            assert response.status_code == 201, response.text
            uploaded = master_bucket / "binary.mp3"
            assert uploaded.exists()
            assert uploaded.read_bytes() == file_data


class TestLANRequestHandlerDELETE:
    """Tests for DELETE endpoint."""

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
        from hsync_client.lan import get_free_port

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


class TestLANRequestHandlerInboundOriginMarking:
    """Tests that POST/DELETE/PATCH record the originator CID so the master's
    filesystem watcher rebroadcasts the resulting event with that CID,
    letting the sender suppress its own echo.

    The CID is only honoured on authenticated requests (LAN_KEY configured):
    without authentication any LAN peer could spoof another peer's CID and
    suppress its events, so an unauthenticated CID must be ignored.
    """

    _KEY = "secret-lan-key"

    @pytest.fixture
    def server(self):
        from hsync_client.lan import get_free_port

        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_upload_marks_inbound_origin(self, server, tmp_path):
        import requests

        from hsync_client.lan_websocket import (
            consume_inbound_origin,
        )

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": self._KEY},
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {
                "cid": "PeerCID0123456789abcd",
                "path": "",
                "mtime": "",
                "ctime": "",
                "sha256sum": _sha256(b"hello").hexdigest(),
            }
            files = {"file": ("inbound.txt", b"hello")}
            headers = {"Authorization": f"Bearer {self._KEY}"}
            response = requests.post(url, data=data, files=files, headers=headers, timeout=5)
            assert response.status_code == 201
            assert consume_inbound_origin(str(tmp_path / "inbound.txt")) == "PeerCID0123456789abcd"

    def test_delete_marks_inbound_origin(self, server, tmp_path):
        import requests

        from hsync_client.lan_websocket import (
            consume_inbound_origin,
        )

        srv, port = server
        target = tmp_path / "gone.txt"
        target.write_text("bye")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": self._KEY},
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            headers = {"Authorization": f"Bearer {self._KEY}"}
            response = requests.delete(
                url,
                params={"path": "gone.txt", "cid": "PeerCID1123456789abcd"},
                headers=headers,
                timeout=5,
            )
            assert response.status_code == 200
            assert consume_inbound_origin(str(target)) == "PeerCID1123456789abcd"

    def test_rename_marks_inbound_origin_for_both_paths(self, server, tmp_path):
        import requests

        from hsync_client.lan_websocket import (
            consume_inbound_origin,
        )

        srv, port = server
        src = tmp_path / "src.txt"
        src.write_text("x")
        dst = tmp_path / "dst.txt"

        with patch(
            "hsync_client.lan_server.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": self._KEY},
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            headers = {"Authorization": f"Bearer {self._KEY}"}
            response = requests.patch(
                url,
                params={"src": "src.txt", "dst": "dst.txt", "cid": "PeerCID2123456789abcd"},
                headers=headers,
                timeout=5,
            )
            assert response.status_code == 200
            assert consume_inbound_origin(str(src)) == "PeerCID2123456789abcd"
            assert consume_inbound_origin(str(dst)) == "PeerCID2123456789abcd"

    def test_upload_ignores_cid_when_unauthenticated(self, server, tmp_path):
        """Without LAN_KEY the CID is untrusted: the upload still succeeds but
        no inbound origin is recorded (spoofing mitigation)."""
        import requests

        from hsync_client.lan_websocket import (
            consume_inbound_origin,
        )

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None},
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {
                "cid": "spoofed",
                "path": "",
                "mtime": "",
                "ctime": "",
                "sha256sum": _sha256(b"hello").hexdigest(),
            }
            files = {"file": ("inbound.txt", b"hello")}
            response = requests.post(url, data=data, files=files, timeout=5)
            assert response.status_code == 201
            assert (tmp_path / "inbound.txt").exists()
            assert consume_inbound_origin(str(tmp_path / "inbound.txt")) is None

    def test_delete_ignores_cid_when_unauthenticated(self, server, tmp_path):
        """Without LAN_KEY the delete still happens but the CID is not recorded."""
        import requests

        from hsync_client.lan_websocket import (
            consume_inbound_origin,
        )

        srv, port = server
        target = tmp_path / "gone.txt"
        target.write_text("bye")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None},
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.delete(
                url, params={"path": "gone.txt", "cid": "spoofed"}, timeout=5
            )
            assert response.status_code == 200
            assert not target.exists()
            assert consume_inbound_origin(str(target)) is None

    def test_rename_ignores_cid_when_unauthenticated(self, server, tmp_path):
        """Without LAN_KEY the rename still happens but the CID is not recorded."""
        import requests

        from hsync_client.lan_websocket import (
            consume_inbound_origin,
        )

        srv, port = server
        src = tmp_path / "src.txt"
        src.write_text("x")
        dst = tmp_path / "dst.txt"

        with patch(
            "hsync_client.lan_server.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None},
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.patch(
                url,
                params={"src": "src.txt", "dst": "dst.txt", "cid": "spoofed"},
                timeout=5,
            )
            assert response.status_code == 200
            assert dst.exists()
            assert consume_inbound_origin(str(src)) is None
            assert consume_inbound_origin(str(dst)) is None


class TestLANRequestHandlerRange:
    """Tests for Range request handling."""

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

    def test_download_with_range(self, server, tmp_path):
        """Test downloading file with Range header."""
        import requests

        srv, port = server

        # Create test file
        test_file = tmp_path / "range_test.txt"
        test_file.write_text("0123456789")  # 10 bytes

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files/download"
            response = requests.get(
                url,
                params={"filename": "range_test.txt"},
                headers={"Range": "bytes=0-4"},
                timeout=5,
            )

            assert response.status_code == 206
            assert response.headers.get("Content-Range") == "bytes 0-4/10"
            assert response.content == b"01234"

    def test_download_with_suffix_range(self, server, tmp_path):
        """Test downloading file with suffix Range (last N bytes)."""
        import requests

        srv, port = server

        # Create test file
        test_file = tmp_path / "suffix_test.txt"
        test_file.write_text("0123456789")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files/download"
            response = requests.get(
                url,
                params={"filename": "suffix_test.txt"},
                headers={"Range": "bytes=-3"},
                timeout=5,
            )

            assert response.status_code == 206
            assert response.content == b"789"

    def test_download_with_invalid_range(self, server, tmp_path):
        """Test downloading with invalid Range returns 416."""
        import requests

        srv, port = server

        # Create test file
        test_file = tmp_path / "invalid_range.txt"
        test_file.write_text("0123456789")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files/download"
            response = requests.get(
                url,
                params={"filename": "invalid_range.txt"},
                headers={"Range": "bytes=100-200"},
                timeout=5,
            )

            assert response.status_code == 416


class TestLANRequestHandlerTime:
    """Tests for time endpoint."""

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

    def test_time_endpoint(self, server, tmp_path):
        """Test time endpoint returns timestamp."""
        import requests

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/time"
            response = requests.get(url, timeout=5)

            assert response.status_code == 200
            data = response.json()
            assert "timestamp" in data
            assert isinstance(data["timestamp"], float)


class TestLANRequestHandlerListFilesFilter:
    """Tests for list files filtering."""

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

    def test_list_files_with_path_filter(self, server, tmp_path):
        """Test listing files with path filter."""
        import requests

        srv, port = server

        # Create test files
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "file2.txt").write_text("content2")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.get(url, params={"path": "subdir/"}, timeout=5)

            assert response.status_code == 200
            files = response.json()
            # Should only return files in subdir
            assert len(files) == 1
            assert "subdir" in files[0]["filepath"]


class TestLANRequestHandlerUpload:
    """Tests for upload endpoint edge cases."""

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

    def test_upload_with_compressed_flag(self, server, tmp_path):
        """Test uploading a compressed file."""
        import requests
        import zstandard as zstd

        srv, port = server

        # Compress some content
        original_content = b"test content for compression"
        compressor = zstd.ZstdCompressor()
        compressed_content = compressor.compress(original_content)

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {
                "cid": "test",
                "path": "",
                "mtime": "1700000000.0",
                "compressed": "true",
                "size": str(len(original_content)),
                "sha256sum": _sha256(original_content).hexdigest(),
            }
            files = {"file": ("compressed.txt", compressed_content)}

            response = requests.post(url, data=data, files=files, timeout=5)
            assert response.status_code == 201

            # Verify file was decompressed
            uploaded_file = tmp_path / "compressed.txt"
            assert uploaded_file.exists()
            assert uploaded_file.read_bytes() == original_content

    def test_upload_without_file(self, server, tmp_path):
        """Test uploading without file returns error."""
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
            data = {"cid": "test", "path": "", "mtime": ""}
            files = {}

            response = requests.post(url, data=data, files=files, timeout=5)
            assert response.status_code == 400


class TestLANServerEdgeCases:
    """Tests for edge cases and error handling."""

    def test_server_start_already_running(self):
        """Test starting an already running server logs warning."""
        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)

        with patch("hsync_client.lan_server.log") as mock_log:
            server.start()
            warning_calls = [
                call
                for call in mock_log.warning.call_args_list
                if "already running" in str(call).lower()
            ]
            assert len(warning_calls) > 0

        server.stop()

    def test_server_stop_not_running(self):
        """Test stopping a not running server is safe."""
        server = LANServer(port=9999)
        server.stop()  # Should not raise


class TestLANRequestHandlerTombstones:
    """Tests for the tombstone gossip endpoints (GET serve + POST merge)."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""
        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_get_empty_returns_empty_object(self, server, tmp_path):
        """With no recorded deletions the endpoint serves an empty mapping."""
        srv, port = server

        with (
            patch(
                "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None}
            ),
            patch("hsync_client.tombstones.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
        ):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/buckets/test/tombstones")
            response = conn.getresponse()

            assert response.status == 200
            assert json.loads(response.read().decode()) == {}
            conn.close()

    def test_get_serves_recorded_tombstones(self, server, tmp_path):
        """A locally recorded deletion is returned by the GET endpoint."""
        srv, port = server
        ts = int(time.time())

        with (
            patch(
                "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None}
            ),
            patch("hsync_client.tombstones.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
        ):
            ts_mod.record_deletion(str(tmp_path / "gone.txt"), sha256sum="a" * 64, timestamp=ts)

            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/buckets/test/tombstones")
            response = conn.getresponse()

            assert response.status == 200
            data = json.loads(response.read().decode())
            assert data[tombstone_key("gone.txt")] == {"ts": ts, "sha256": "a" * 64}
            conn.close()

    def test_post_merges_pushed_payload(self, server, tmp_path):
        """A pushed payload is merged and then visible through the GET endpoint."""
        import requests

        srv, port = server
        key = tombstone_key("pushed.txt")
        ts = int(time.time())

        with (
            patch(
                "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None}
            ),
            patch("hsync_client.tombstones.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/tombstones"
            payload = {key: {"ts": ts, "sha256": "d" * 64}}

            response = requests.post(url, json=payload, timeout=5)
            assert response.status_code == 200
            assert response.json()["merged"] == 1

            # The merged marker is now served back.
            assert ts_mod.get_payload()[key] == {"ts": ts, "sha256": "d" * 64}

    def test_post_empty_body_merges_nothing(self, server, tmp_path):
        """An empty body is accepted and merges zero entries."""
        import requests

        srv, port = server

        with (
            patch(
                "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None}
            ),
            patch("hsync_client.tombstones.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/tombstones"
            response = requests.post(url, data=b"", timeout=5)

            assert response.status_code == 200
            assert response.json()["merged"] == 0

    def test_post_invalid_json_returns_400(self, server, tmp_path):
        """A malformed body is rejected with a 400."""
        import requests

        srv, port = server

        with (
            patch(
                "hsync_client.lan_server.CONFIG", {"LOCAL_BUCKET": str(tmp_path), "LAN_KEY": None}
            ),
            patch("hsync_client.tombstones.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/tombstones"
            response = requests.post(url, data=b"not-json", timeout=5)

            assert response.status_code == 400
