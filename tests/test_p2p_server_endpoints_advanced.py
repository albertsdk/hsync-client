"""
Tests for P2P embedded HTTP server endpoints.
"""

import json
import time
from hashlib import sha256 as _sha256
from http.client import HTTPConnection
from unittest.mock import patch

import pytest
from hsync_common.models.tombstone import tombstone_key

from hsync_client import tombstones as ts_mod
from hsync_client.p2p_peers import get_free_port
from hsync_client.p2p_server import P2PServer

from .p2p_helpers import unauth_write_config


class TestLANRequestHandlerInboundOriginMarking:
    """Tests that POST/DELETE/PATCH record the originator CID so the master's
    filesystem watcher rebroadcasts the resulting event with that CID,
    letting the sender suppress its own echo.

    The CID is only honoured on authenticated requests (P2P_KEY configured):
    without authentication any P2P peer could spoof another peer's CID and
    suppress its events, so an unauthenticated CID must be ignored.
    """

    _KEY = "secret-lan-key"

    @pytest.fixture
    def server(self):
        from hsync_client.p2p_peers import get_free_port

        port = get_free_port()
        server = P2PServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_upload_marks_inbound_origin(self, server, tmp_path):
        import requests

        from hsync_client.p2p_websocket import (
            consume_inbound_origin,
        )

        srv, port = server

        with patch(
            "hsync_client.p2p_server.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "P2P_KEY": self._KEY},
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

        from hsync_client.p2p_websocket import (
            consume_inbound_origin,
        )

        srv, port = server
        target = tmp_path / "gone.txt"
        target.write_text("bye")

        with patch(
            "hsync_client.p2p_server.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "P2P_KEY": self._KEY},
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

        from hsync_client.p2p_websocket import (
            consume_inbound_origin,
        )

        srv, port = server
        src = tmp_path / "src.txt"
        src.write_text("x")
        dst = tmp_path / "dst.txt"

        with patch(
            "hsync_client.p2p_server.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "P2P_KEY": self._KEY},
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
        """Without P2P_KEY the CID is untrusted: the upload still succeeds but
        no inbound origin is recorded (spoofing mitigation)."""
        import requests

        from hsync_client.p2p_websocket import (
            consume_inbound_origin,
        )

        srv, port = server

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            data = {
                "cid": "SpoofCid1234567890abc",
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
        """Without P2P_KEY the delete still happens but the CID is not recorded."""
        import requests

        from hsync_client.p2p_websocket import (
            consume_inbound_origin,
        )

        srv, port = server
        target = tmp_path / "gone.txt"
        target.write_text("bye")

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.delete(
                url, params={"path": "gone.txt", "cid": "SpoofCid1234567890abc"}, timeout=5
            )
            assert response.status_code == 200
            assert not target.exists()
            assert consume_inbound_origin(str(target)) is None

    def test_rename_ignores_cid_when_unauthenticated(self, server, tmp_path):
        """Without P2P_KEY the rename still happens but the CID is not recorded."""
        import requests

        from hsync_client.p2p_websocket import (
            consume_inbound_origin,
        )

        srv, port = server
        src = tmp_path / "src.txt"
        src.write_text("x")
        dst = tmp_path / "dst.txt"

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.patch(
                url,
                params={"src": "src.txt", "dst": "dst.txt", "cid": "SpoofCid1234567890abc"},
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
        from hsync_client.p2p_peers import get_free_port

        port = get_free_port()
        server = P2PServer(port=port)
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

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
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

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
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

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
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
        from hsync_client.p2p_peers import get_free_port

        port = get_free_port()
        server = P2PServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_time_endpoint(self, server, tmp_path):
        """Test time endpoint returns timestamp."""
        import requests

        srv, port = server

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
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
        from hsync_client.p2p_peers import get_free_port

        port = get_free_port()
        server = P2PServer(port=port)
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

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
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
        from hsync_client.p2p_peers import get_free_port

        port = get_free_port()
        server = P2PServer(port=port)
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

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
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

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
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
        server = P2PServer(port=port)
        server.start()
        time.sleep(0.1)

        with patch("hsync_client.p2p_server.log") as mock_log:
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
        server = P2PServer(port=9999)
        server.stop()  # Should not raise


class TestLANRequestHandlerTombstones:
    """Tests for the tombstone gossip endpoints (GET serve + POST merge)."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""
        port = get_free_port()
        server = P2PServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_get_empty_returns_empty_object(self, server, tmp_path):
        """With no recorded deletions the endpoint serves an empty mapping."""
        srv, port = server

        with (
            patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))),
            patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
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
            patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))),
            patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
        ):
            ts_mod.record_deletion(str(tmp_path / "gone.txt"), sha256sum="a" * 64, timestamp=ts)

            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", "/api/v1/buckets/test/tombstones")
            response = conn.getresponse()

            assert response.status == 200
            data = json.loads(response.read().decode())
            entry = data[tombstone_key("gone.txt")]
            # Causal shape (ADR-0003): hashed key -> {sha256, version_id,
            # origin_cid, ts, vclock}. version_id is minted, so assert the
            # deterministic fields and the presence of the causal ones.
            assert entry["ts"] == ts
            assert entry["sha256"] == "a" * 64
            assert entry["origin_cid"] is None
            assert entry["vclock"] == {}
            assert set(entry) == {"sha256", "version_id", "origin_cid", "ts", "vclock"}
            conn.close()

    def test_post_merges_pushed_payload(self, server, tmp_path):
        """A pushed payload is merged and then visible through the GET endpoint."""
        import requests

        srv, port = server
        key = tombstone_key("pushed.txt")
        ts = int(time.time())

        with (
            patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))),
            patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/tombstones"
            payload = {
                key: {
                    "ts": ts,
                    "sha256": "d" * 64,
                    "version_id": "v1",
                    "origin_cid": None,
                    "vclock": {"TestClientA1234567890": 1},
                }
            }

            response = requests.post(url, json=payload, timeout=5)
            assert response.status_code == 200
            assert response.json()["merged"] == 1

            # The merged marker is now served back with its causal fields.
            merged = ts_mod.get_payload()[key]
            assert merged["ts"] == ts
            assert merged["sha256"] == "d" * 64
            assert merged["vclock"] == {"TestClientA1234567890": 1}

    def test_post_empty_body_merges_nothing(self, server, tmp_path):
        """An empty body is accepted and merges zero entries."""
        import requests

        srv, port = server

        with (
            patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))),
            patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
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
            patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))),
            patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/tombstones"
            response = requests.post(url, data=b"not-json", timeout=5)

            assert response.status_code == 400
