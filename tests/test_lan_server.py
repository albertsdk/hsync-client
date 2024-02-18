"""
Tests for LAN embedded HTTP server.
"""

import json
import time
from http.client import HTTPConnection
from unittest.mock import patch

import pytest

from hsync_client.lan_server import LANServer


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
                "md5sum": "abc123",
                "permissions": "-rw-r--r--",
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
            data = {"cid": "test", "path": "", "mtime": "", "ctime": ""}
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
            }
            files = {"file": ("new_from_client.txt", b"client content")}

            response = requests.post(url, data=data, files=files, timeout=5)
            assert response.status_code == 201

            # Verify file exists on master
            assert (master_bucket / "new_from_client.txt").exists()
            assert (master_bucket / "new_from_client.txt").read_text() == "client content"


class TestMultipartParser:
    """Tests for multipart form-data parser."""

    def test_parse_multipart_basic(self):
        """Test basic multipart parsing."""
        from hsync_client.lan_server import _parse_multipart

        body = b"""--boundary\r
Content-Disposition: form-data; name="field1"\r
\r
value1\r
--boundary--\r
"""
        form_data, file_data = _parse_multipart(body, "multipart/form-data; boundary=boundary")
        assert form_data["field1"] == "value1"
        assert file_data == {}

    def test_parse_multipart_with_file(self):
        """Test multipart parsing with file upload."""
        from hsync_client.lan_server import _parse_multipart

        body = b"""--boundary\r
Content-Disposition: form-data; name="file"; filename="test.txt"\r
\r
file content\r
--boundary--\r
"""
        form_data, file_data = _parse_multipart(body, "multipart/form-data; boundary=boundary")
        assert form_data == {}
        assert "file" in file_data
        filename, content = file_data["file"]
        assert filename == "test.txt"
        assert content == b"file content"

    def test_parse_multipart_multiple_fields(self):
        """Test multipart parsing with multiple fields."""
        from hsync_client.lan_server import _parse_multipart

        body = b"""--boundary\r
Content-Disposition: form-data; name="field1"\r
\r
value1\r
--boundary\r
Content-Disposition: form-data; name="field2"\r
\r
value2\r
--boundary--\r
"""
        form_data, file_data = _parse_multipart(body, "multipart/form-data; boundary=boundary")
        assert form_data["field1"] == "value1"
        assert form_data["field2"] == "value2"

    def test_parse_multipart_no_boundary(self):
        """Test that missing boundary raises error."""
        from hsync_client.lan_server import _parse_multipart

        with pytest.raises(ValueError, match="No boundary"):
            _parse_multipart(b"some data", "multipart/form-data")

    def test_parse_multipart_quoted_boundary(self):
        """Test multipart parsing with quoted boundary."""
        from hsync_client.lan_server import _parse_multipart

        body = b'--boundary\r\nContent-Disposition: form-data; name="field1"\r\n\r\nvalue1\r\n--boundary--\r\n'
        form_data, file_data = _parse_multipart(body, 'multipart/form-data; boundary="boundary"')
        assert form_data["field1"] == "value1"


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
        from hsync_client.lan import get_free_port

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


class TestSSLUtils:
    """Tests for SSL certificate generation utilities."""

    def test_generate_self_signed_cert(self):
        """Test generating a self-signed certificate."""
        from hsync_client.ssl_utils import CertificateInfo, generate_self_signed_cert

        cert_info = generate_self_signed_cert()

        assert isinstance(cert_info, CertificateInfo)
        assert cert_info.cert_pem is not None
        assert cert_info.key_pem is not None
        assert cert_info.fingerprint is not None
        # Fingerprint should be a hex string (SHA-256 = 64 characters)
        assert len(cert_info.fingerprint) == 64
        assert cert_info.cert_file is None  # Not saved yet
        assert cert_info.key_file is None

    def test_generate_cert_with_custom_cn(self):
        """Test generating a certificate with custom common name."""
        from hsync_client.ssl_utils import generate_self_signed_cert

        cert_info = generate_self_signed_cert(common_name="test-hsync")
        assert cert_info.cert_pem is not None
        assert b"BEGIN CERTIFICATE" in cert_info.cert_pem

    def test_save_cert_to_temp_files(self):
        """Test saving certificate to temporary files."""
        import os

        from hsync_client.ssl_utils import generate_self_signed_cert, save_cert_to_temp_files

        cert_info = generate_self_signed_cert()
        saved_info = save_cert_to_temp_files(cert_info)

        assert saved_info.cert_file is not None
        assert saved_info.key_file is not None
        assert os.path.exists(saved_info.cert_file)
        assert os.path.exists(saved_info.key_file)

        # Verify file contents
        with open(saved_info.cert_file, "rb") as f:
            assert f.read() == cert_info.cert_pem
        with open(saved_info.key_file, "rb") as f:
            assert f.read() == cert_info.key_pem

        # Cleanup
        os.unlink(saved_info.cert_file)
        os.unlink(saved_info.key_file)

    def test_fingerprint_is_deterministic(self):
        """Test that same cert produces same fingerprint."""
        import hashlib

        from cryptography import x509

        # Generate a cert and verify fingerprint calculation
        from hsync_client.ssl_utils import generate_self_signed_cert

        cert_info = generate_self_signed_cert()

        # Parse the cert and recalculate fingerprint
        cert = x509.load_pem_x509_certificate(cert_info.cert_pem)
        from cryptography.hazmat.primitives import serialization

        expected_fp = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()

        assert cert_info.fingerprint == expected_fp


class TestLANServerHTTPS:
    """Tests for LANServer HTTPS functionality."""

    def test_server_https_requires_cert(self):
        """Test that HTTPS mode requires certificate files."""
        with pytest.raises(ValueError, match="cert_file and key_file are required"):
            LANServer(port=9999, use_https=True)

    def test_server_https_property(self):
        """Test use_https property."""
        server = LANServer(port=9999, use_https=False)
        assert server.use_https is False

    def test_server_https_with_cert(self):
        """Test creating HTTPS server with certificate."""
        import os

        from hsync_client.ssl_utils import generate_self_signed_cert, save_cert_to_temp_files

        cert_info = generate_self_signed_cert()
        saved_info = save_cert_to_temp_files(cert_info)

        try:
            server = LANServer(
                port=0,
                use_https=True,
                cert_file=saved_info.cert_file,
                key_file=saved_info.key_file,
            )
            assert server.use_https is True
            server.start()
            time.sleep(0.1)
            assert server.is_running() is True
            server.stop()
        finally:
            if saved_info.cert_file:
                os.unlink(saved_info.cert_file)
            if saved_info.key_file:
                os.unlink(saved_info.key_file)
