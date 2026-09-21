"""
Regression tests for resumable upload support in P2P master mode.

Verifies that the P2P master's _handle_upload supports offset-based resume,
mirroring the central hsync-server's behavior:
- GET /files/upload-status reports .part size for resume
- POST /files with offset > 0 appends to .part (not truncate)
- Incomplete uploads return 202 with the new offset
- Complete resumed uploads return 201 with correct SHA-256
- Compressed + offset > 0 is rejected
- Offset mismatch is rejected
"""

import time
from unittest.mock import patch

import pytest

from hsync_client.p2p_peers import get_free_port
from hsync_client.p2p_server import P2PServer

from .p2p_helpers import unauth_write_config


class TestLANResumableUpload:
    """Tests for resumable upload in P2P master mode."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""
        port = get_free_port()
        server = P2PServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    @pytest.fixture
    def config(self, tmp_path):
        """Base CONFIG for P2P master tests."""
        return unauth_write_config(tmp_path)

    def _upload_url(self, port: int) -> str:
        return f"http://localhost:{port}/api/v1/buckets/test/files"

    def _status_url(self, port: int) -> str:
        return f"http://localhost:{port}/api/v1/buckets/test/files/upload-status"

    def test_upload_status_new_file(self, server, tmp_path, config):
        """upload-status returns 'new' when no file or .part exists."""
        import requests

        _, port = server
        with patch("hsync_client.p2p_server.CONFIG", config):
            resp = requests.get(
                self._status_url(port),
                params={"filename": "newfile.txt", "sha256sum": "a" * 64, "size": 100},
                timeout=5,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "new"
            assert data["offset"] == 0

    def test_upload_status_complete(self, server, tmp_path, config):
        """upload-status returns 'complete' when file exists with matching SHA."""
        import requests

        _, port = server
        content = b"hello world"
        test_file = tmp_path / "complete.txt"
        test_file.write_bytes(content)

        import hashlib

        sha = hashlib.sha256(content).hexdigest()

        with patch("hsync_client.p2p_server.CONFIG", config):
            resp = requests.get(
                self._status_url(port),
                params={"filename": "complete.txt", "sha256sum": sha, "size": len(content)},
                timeout=5,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "complete"

    def test_partial_upload_returns_202(self, server, tmp_path, config):
        """Uploading less data than declared size returns 202 with offset."""
        import requests

        _, port = server
        content = b"partial content chunk 1"
        declared_size = 1000  # much larger than actual content

        import hashlib

        sha = hashlib.sha256(
            b"partial content chunk 1" + b"\x00" * (declared_size - len(content))
        ).hexdigest()

        with patch("hsync_client.p2p_server.CONFIG", config):
            # Upload first chunk with offset=0
            files = {"file": ("bigfile.txt", content)}
            data = {
                "cid": "test_cid_12345678901",
                "path": "",
                "size": str(declared_size),
                "sha256sum": sha,
                "permissions": "644",
                "offset": "0",
                "mtime": str(int(time.time())),
                "ctime": str(int(time.time())),
                "vclock": "",
                "version_id": "",
                "corrected_mtime": "0",
            }
            resp = requests.post(self._upload_url(port), files=files, data=data, timeout=5)

            # Should return 202 (partial)
            assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
            result = resp.json()
            assert "offset" in result
            assert result["offset"] == len(content)
            assert "remaining" in result

            # .part file should exist
            part_file = tmp_path / "bigfile.txt.part"
            assert part_file.exists()
            assert part_file.stat().st_size == len(content)

    def test_resume_upload_completes(self, server, tmp_path, config):
        """Resuming an interrupted upload completes the file."""
        import requests

        _, port = server
        chunk1 = b"first chunk of data "
        chunk2 = b"second chunk of data"
        full_content = chunk1 + chunk2

        import hashlib

        sha = hashlib.sha256(full_content).hexdigest()

        with patch("hsync_client.p2p_server.CONFIG", config):
            # Step 1: Upload first chunk (partial)
            files = {"file": ("resume.txt", chunk1)}
            data = {
                "cid": "test_cid_12345678901",
                "path": "",
                "size": str(len(full_content)),
                "sha256sum": sha,
                "permissions": "644",
                "offset": "0",
                "mtime": str(int(time.time())),
                "ctime": str(int(time.time())),
                "vclock": "",
                "version_id": "",
                "corrected_mtime": "0",
            }
            resp = requests.post(self._upload_url(port), files=files, data=data, timeout=5)
            assert resp.status_code == 202

            # Step 2: Check upload-status
            resp = requests.get(
                self._status_url(port),
                params={"filename": "resume.txt", "sha256sum": sha, "size": len(full_content)},
                timeout=5,
            )
            assert resp.status_code == 200
            status_data = resp.json()
            assert status_data["status"] == "resumable"
            assert status_data["offset"] == len(chunk1)

            # Step 3: Resume with second chunk
            files = {"file": ("resume.txt", chunk2)}
            data["offset"] = str(len(chunk1))
            resp = requests.post(self._upload_url(port), files=files, data=data, timeout=5)

            # Should return 201 (created)
            assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"

            # Final file should exist with correct content
            final_file = tmp_path / "resume.txt"
            assert final_file.exists()
            assert final_file.read_bytes() == full_content

            # .part file should be gone
            part_file = tmp_path / "resume.txt.part"
            assert not part_file.exists()

    def test_compressed_offset_rejected(self, server, tmp_path, config):
        """Compressed upload with offset > 0 is rejected."""
        import requests

        _, port = server
        content = b"some compressed content"

        with patch("hsync_client.p2p_server.CONFIG", config):
            files = {"file": ("compressed.txt", content)}
            data = {
                "cid": "test_cid_12345678901",
                "path": "",
                "size": "100",
                "sha256sum": "a" * 64,
                "permissions": "644",
                "offset": "50",
                "compressed": "true",
                "compressed_size": str(len(content)),
                "mtime": str(int(time.time())),
                "ctime": str(int(time.time())),
                "vclock": "",
                "version_id": "",
                "corrected_mtime": "0",
            }
            resp = requests.post(self._upload_url(port), files=files, data=data, timeout=5)
            assert resp.status_code == 400

    def test_offset_mismatch_rejected(self, server, tmp_path, config):
        """Resume with wrong offset (size mismatch) is rejected."""
        import requests

        _, port = server

        # Create a .part file with 100 bytes
        part_file = tmp_path / "mismatch.txt.part"
        part_file.write_bytes(b"x" * 100)

        with patch("hsync_client.p2p_server.CONFIG", config):
            files = {"file": ("mismatch.txt", b"new data")}
            data = {
                "cid": "test_cid_12345678901",
                "path": "",
                "size": "200",
                "sha256sum": "a" * 64,
                "permissions": "644",
                "offset": "50",  # wrong — .part has 100 bytes
                "mtime": str(int(time.time())),
                "ctime": str(int(time.time())),
                "vclock": "",
                "version_id": "",
                "corrected_mtime": "0",
            }
            resp = requests.post(self._upload_url(port), files=files, data=data, timeout=5)
            assert resp.status_code == 400

    def test_resume_without_part_rejected(self, server, tmp_path, config):
        """Resume with offset > 0 but no .part file is rejected."""
        import requests

        _, port = server

        with patch("hsync_client.p2p_server.CONFIG", config):
            files = {"file": ("noresume.txt", b"new data")}
            data = {
                "cid": "test_cid_12345678901",
                "path": "",
                "size": "200",
                "sha256sum": "a" * 64,
                "permissions": "644",
                "offset": "50",
                "mtime": str(int(time.time())),
                "ctime": str(int(time.time())),
                "vclock": "",
                "version_id": "",
                "corrected_mtime": "0",
            }
            resp = requests.post(self._upload_url(port), files=files, data=data, timeout=5)
            assert resp.status_code == 400
