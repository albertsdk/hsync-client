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


class TestLANUploadAbsoluteCap:
    """MAX_DOWNLOAD_SIZE caps LAN uploads on top of the peer-declared size."""

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

    def _post_file(self, port, data, content=b"A" * 1000, name="big.bin"):
        import requests

        url = f"http://localhost:{port}/api/v1/buckets/test/files"
        return requests.post(url, data=data, files={"file": (name, content)}, timeout=5)

    def test_upload_beyond_absolute_cap_rejected(self, server, tmp_path):
        """A peer declaring 1 GB is still capped at MAX_DOWNLOAD_SIZE."""
        import hashlib

        srv, port = server
        content = b"A" * 1000

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
                "MIN_FREE_DISK_SPACE": 0,
                "MAX_DOWNLOAD_SIZE": 50,
            },
        ):
            # A valid sha256sum makes the cap the ONLY possible rejection
            # reason (no format-gate or checksum-mismatch fallback).
            response = self._post_file(
                port,
                {
                    "cid": "t",
                    "path": "",
                    "size": "1000000",
                    "sha256sum": hashlib.sha256(content).hexdigest(),
                },
                content=content,
            )

            assert response.status_code == 400
            assert "maximum allowed size" in response.text
            assert not (tmp_path / "big.bin").exists()
            assert not (tmp_path / "big.bin.part").exists()

    def test_upload_without_declared_size_capped(self, server, tmp_path):
        """An uncompressed upload with no declared size is capped too."""
        import hashlib

        srv, port = server
        content = b"A" * 1000

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
                "MIN_FREE_DISK_SPACE": 0,
                "MAX_DOWNLOAD_SIZE": 50,
            },
        ):
            response = self._post_file(
                port,
                {"cid": "t", "path": "", "sha256sum": hashlib.sha256(content).hexdigest()},
                content=content,
            )

            assert response.status_code == 400
            assert "maximum allowed size" in response.text
            assert not (tmp_path / "big.bin").exists()
            assert not (tmp_path / "big.bin.part").exists()

    def test_upload_within_cap_accepted(self, server, tmp_path):
        """An upload under the absolute cap round-trips (cap is not over-eager)."""
        import hashlib

        srv, port = server
        content = b"small legit upload"

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
                "MIN_FREE_DISK_SPACE": 0,
                "MAX_DOWNLOAD_SIZE": 1024,
            },
        ):
            response = self._post_file(
                port,
                {
                    "cid": "t",
                    "path": "",
                    "size": str(len(content)),
                    "sha256sum": hashlib.sha256(content).hexdigest(),
                },
                content=content,
                name="ok.bin",
            )

            assert response.status_code == 201
            assert (tmp_path / "ok.bin").read_bytes() == content


class TestLANUploadCidValidation:
    """The echo-suppression CID is honoured only when it passes is_valid_cid
    *and* the request is authenticated (LAN_KEY set)."""

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

    def _upload(self, port, cid):
        import hashlib

        import requests

        content = b"cid test"
        url = f"http://localhost:{port}/api/v1/buckets/test/files"
        return requests.post(
            url,
            data={
                "cid": cid,
                "path": "",
                "size": str(len(content)),
                "sha256sum": hashlib.sha256(content).hexdigest(),
            },
            files={"file": ("cid.bin", content)},
            headers={"Authorization": "Bearer secret"},
            timeout=5,
        )

    def _config(self, tmp_path):
        return {
            "LOCAL_BUCKET": str(tmp_path),
            "LAN_KEY": "secret",
            "MIN_FREE_DISK_SPACE": 0,
        }

    def test_malformed_cid_not_honoured(self, server, tmp_path):
        """A cid failing is_valid_cid is ignored even on authenticated requests."""
        srv, port = server

        with (
            patch("hsync_client.lan_server.CONFIG", self._config(tmp_path)),
            patch("hsync_client.lan_websocket.mark_inbound_origin") as mock_mark,
        ):
            response = self._upload(port, "bad cid!")

            assert response.status_code == 201
            mock_mark.assert_not_called()

    def test_valid_cid_honoured(self, server, tmp_path):
        """A well-formed cid on an authenticated request is honoured."""
        srv, port = server

        with (
            patch("hsync_client.lan_server.CONFIG", self._config(tmp_path)),
            patch("hsync_client.lan_websocket.mark_inbound_origin") as mock_mark,
        ):
            response = self._upload(port, "good-cid_1")

            assert response.status_code == 201
            mock_mark.assert_called_once()


class TestLANMultipartCaps:
    """Multipart form fields / header lines are memory-capped (the file part
    is the only part allowed to be large, and it streams to disk)."""

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

    def test_oversized_form_field_rejected(self, server, tmp_path):
        """A 1 MB form field must not be buffered in master RAM."""
        import requests

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
                "MIN_FREE_DISK_SPACE": 0,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {"cid": "t", "path": "x" * (1024 * 1024), "sha256sum": "x" * 64}
            response = requests.post(url, data=data, files={"file": ("f.bin", b"data")}, timeout=5)

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
