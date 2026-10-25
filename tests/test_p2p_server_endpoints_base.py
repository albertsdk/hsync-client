"""
Tests for P2P embedded HTTP server endpoints.
"""

import json
import os
import time
from hashlib import sha256 as _sha256
from http.client import HTTPConnection
from unittest.mock import patch

import pytest

from hsync_client.p2p_peers import get_free_port
from hsync_client.p2p_server import P2PServer
from hsync_client.p2p_websocket import SlaveCidRegistry

from .p2p_helpers import unauth_write_config


class TestLANRequestHandler:
    """Tests for the P2P data-plane endpoints."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""

        port = get_free_port()
        server = P2PServer(port=port)
        server.start()
        yield server, port
        server.stop()

    def test_health_endpoint(self, server, tmp_path):
        """Test health check endpoint."""
        srv, port = server

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/health")
            response = conn.getresponse()

            assert response.status == 200
            data = json.loads(response.read().decode())
            assert data["status"] == "ok"
            assert data["mode"] == "p2p_master"
            conn.close()

    def test_health_endpoint_with_auth(self, server, tmp_path):
        """Test health check with authentication."""
        srv, port = server

        with patch(
            "hsync_client.p2p_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "P2P_KEY": "secret123",
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

    def test_no_key_reads_allowed_but_mutations_default_deny(self, server, tmp_path):
        """No-key mode is read-only unless the dangerous opt-in is explicit."""
        _, port = server
        with patch(
            "hsync_client.p2p_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "P2P_KEY": None,
                "P2P_ALLOW_UNAUTHENTICATED_WRITES": False,
            },
        ):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/health")
            response = conn.getresponse()
            assert response.status == 200
            response.read()
            conn.close()

            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("POST", "/api/v1/buckets/test/dirs?path=blocked")
            response = conn.getresponse()
            assert response.status == 403
            response.read()
            conn.close()

        assert not (tmp_path / "blocked").exists()

    def test_list_files_endpoint(self, server, tmp_path):
        """Test list files endpoint."""
        srv, port = server

        # Create test files
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "file2.txt").write_text("content2")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "file3.txt").write_text("content3")

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
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

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/buckets/test/files/download?filename=download_test.txt")
            response = conn.getresponse()

            assert response.status == 200
            content = response.read().decode()
            assert content == "download content"
            conn.close()

    def test_download_reports_the_outgoing_transfer(self, server, tmp_path, monkeypatch):
        """Serving a file counts as an outgoing transfer (Sent files and bytes)."""
        srv, port = server
        test_file = tmp_path / "served.txt"
        test_file.write_text("served content")
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "hsync_client.p2p_server.emit_event",
            lambda event, **payload: events.append((str(event), payload)),
        )

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/buckets/test/files/download?filename=served.txt")
            response = conn.getresponse()
            assert response.status == 200
            response.read()
            conn.close()

        time.sleep(0.1)  # the handler emits right after the response body
        uploads = [payload for event, payload in events if event == "transfer"]
        assert uploads == [{"op": "upload", "file": str(test_file), "size": len("served content")}]

    def test_download_file_not_found(self, server, tmp_path):
        """Test download of non-existent file."""
        srv, port = server

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/buckets/test/files/download?filename=nonexistent.txt")
            response = conn.getresponse()

            assert response.status == 404
            conn.close()

    def test_not_found_endpoint(self, server, tmp_path):
        """Test 404 for unknown endpoints."""
        srv, port = server

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/unknown/path")
            response = conn.getresponse()

            assert response.status == 404
            conn.close()

    def test_upload_file_endpoint(self, server, tmp_path):
        """Test file upload endpoint using requests (like transfer.py)."""
        import requests

        srv, port = server

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
            # Simulate upload like transfer.py does
            url = f"http://127.0.0.1:{port}/api/v1/buckets/test/files"
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
            "hsync_client.p2p_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "P2P_KEY": "secret123",
            },
        ):
            url = f"http://127.0.0.1:{port}/api/v1/buckets/test/files"
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
    """Integration tests for P2P sync flow."""

    @pytest.fixture
    def master_server(self, tmp_path):
        """Create a P2P server acting as master with test files."""
        from hsync_client.p2p_server import P2PServer

        # Create master bucket with files
        master_bucket = tmp_path / "master"
        master_bucket.mkdir()
        (master_bucket / "file1.txt").write_text("master content 1")
        (master_bucket / "file2.txt").write_text("master content 2")
        (master_bucket / "subdir").mkdir()
        (master_bucket / "subdir" / "file3.txt").write_text("master content 3")

        port = get_free_port()
        server = P2PServer(port=port)
        server.start()

        yield server, port, master_bucket

        server.stop()

    def test_client_download_from_master(self, master_server, tmp_path):
        """Test client downloading files from master via sync."""
        import requests

        srv, port, master_bucket = master_server

        # Create client bucket (empty)
        client_bucket = tmp_path / "client"
        client_bucket.mkdir()

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(master_bucket))):
            # Simulate what sync.get_files_remote() does
            url = f"http://127.0.0.1:{port}/api/v1/buckets/test/files"
            response = requests.get(url, timeout=5)
            assert response.status_code == 200
            files = response.json()
            assert len(files) == 3

            # Simulate download for each file
            for file_info in files:
                filepath = file_info["filepath"]
                download_url = f"http://127.0.0.1:{port}/api/v1/buckets/test/files/download"
                response = requests.get(download_url, params={"filename": filepath}, timeout=5)
                assert response.status_code == 200

    def test_client_upload_to_master(self, master_server, tmp_path):
        """Test client uploading files to master."""
        import requests

        srv, port, master_bucket = master_server

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(master_bucket))):
            # Simulate upload like transfer.py does
            url = f"http://127.0.0.1:{port}/api/v1/buckets/test/files"
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

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(master_bucket))):
            url = f"http://127.0.0.1:{port}/api/v1/buckets/test/files"
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


class TestLANDuplicateCidGuard:
    """The HTTP write endpoints refuse duplicated device identities (409)."""

    @pytest.fixture
    def guarded_server(self):
        """A P2P server wired to a shared duplicate-identity registry."""
        registry = SlaveCidRegistry(master_cid="m" * 21)
        port = get_free_port()
        server = P2PServer(port=port, cid_guard=registry)
        server.start()
        yield server, port, registry
        server.stop()

    def _upload(self, port: int, tmp_path, cid: str) -> int:
        """POST a file to the master with the given cid; return the status."""
        import requests

        url = f"http://127.0.0.1:{port}/api/v1/buckets/test/files"
        data = {
            "cid": cid,
            "path": "subdir",
            "size": "12",
            "sha256sum": _sha256(b"test content").hexdigest(),
            "permissions": "644",
        }
        files = {"file": ("uploaded.txt", b"test content")}
        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
            return requests.post(url, data=data, files=files, timeout=5).status_code

    def test_upload_with_master_cid_is_refused(self, guarded_server, tmp_path):
        """Writing with the master's own CID is a duplicate identity (409)."""
        srv, port, registry = guarded_server
        assert self._upload(port, tmp_path, "m" * 21) == 409
        assert (tmp_path / "subdir" / "uploaded.txt").exists() is False

    def test_upload_with_cid_bound_to_other_host_is_refused(self, guarded_server, tmp_path):
        """A CID bound to a different source address is refused (409)."""
        srv, port, registry = guarded_server
        registry.try_reserve("a" * 21, "192.168.1.50", object())
        # The upload comes from localhost, not from the registered address:
        # a cloned device presenting the same CID is refused (409).
        assert self._upload(port, tmp_path, "a" * 21) == 409
        assert (tmp_path / "subdir" / "uploaded.txt").exists() is False

    def test_upload_with_unregistered_cid_still_works(self, guarded_server, tmp_path):
        """An unregistered CID (one-shot slave) is not blocked."""
        srv, port, registry = guarded_server
        # A legitimate (one-shot, never-WSS) slave with an unregistered CID is
        # not blocked: the guard only refuses in-use CIDs from other hosts.
        assert self._upload(port, tmp_path, "u" * 21) == 201
        assert (tmp_path / "subdir" / "uploaded.txt").read_text() == "test content"

    def test_upload_same_host_registered_cid_allowed(self, guarded_server, tmp_path):
        """A CID bound to this host's address passes the guard."""
        srv, port, registry = guarded_server
        # Reserve the CID for this host's loopback address: its writes pass.
        registry.try_reserve("a" * 21, "127.0.0.1", object())
        assert self._upload(port, tmp_path, "a" * 21) == 201
