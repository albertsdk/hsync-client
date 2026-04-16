# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for download transfer functionality."""

import io
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from hsync_common.models.file import File
from requests import RequestException

from hsync_client.events import UiEventName
from hsync_client.exceptions import PermanentDownloadError
from hsync_client.lan_sources import LanPeerSource, PeerFailureKind
from hsync_client.transfer import download_file


class TestDownloadFile:
    """Tests for download_file function."""

    def test_network_error_logs_and_returns(self, tmp_path):
        """Network error during download is logged."""
        file = File(filepath="path/to/file.txt", sha256sum="abc123", size=100)

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                    # Disable retry/backoff so a single attempt is asserted.
                    "TRANSFER_RETRY_ATTEMPTS": 1,
                },
            ),
            patch("hsync_client.transfer.get") as mock_get,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            mock_get.side_effect = RequestException("Connection error")
            download_file(file)
            mock_get.assert_called_once()

    def test_auth_error_401_returns(self, tmp_path):
        """401 response returns without downloading."""
        file = File(filepath="path/to/file.txt", sha256sum="abc123", size=100)

        mock_response = MagicMock()
        mock_response.status_code = 401

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            download_file(file)

    def test_not_found_404_returns(self, tmp_path):
        """404 response returns without downloading."""
        file = File(filepath="path/to/file.txt", sha256sum="abc123", size=100)

        mock_response = MagicMock()
        mock_response.status_code = 404

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            download_file(file)

    def test_server_error_500_returns(self, tmp_path):
        """500 response returns without downloading."""
        file = File(filepath="path/to/file.txt", sha256sum="abc123", size=100)

        mock_response = MagicMock()
        mock_response.status_code = 500

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                    # Disable retry/backoff: a 5xx is raised and surfaced once.
                    "TRANSFER_RETRY_ATTEMPTS": 1,
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            download_file(file)

    def test_successful_download_with_sha256_verification(self, tmp_path):
        """Successful download verifies SHA-256 and moves file."""
        expected_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        file = File(
            filepath="subdir/file.txt",
            sha256sum=expected_sha,  # SHA-256 of empty file
            size=0,
            mtime=1000,
            ctime=1000,
            permissions="644",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_content = MagicMock(return_value=[b""])
        # Real dict so headers.get() returns proper defaults instead of MagicMock
        mock_response.headers = {"X-Checksum-SHA256": expected_sha}

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch(
                "hsync_client.transfer.compute_sha256",
                return_value=expected_sha,
            ),
            patch("hsync_client.transfer.replace") as mock_replace,
            patch("hsync_client.transfer.utime") as mock_utime,
            patch("hsync_client.transfer.set_permissions") as mock_set_perms,
            patch("hsync_client.transfer.remove"),
        ):
            download_file(file)
            mock_replace.assert_called_once()
            mock_utime.assert_called_once()
            # atime mirrors mtime; ctime is not restored (POSIX ctime is not settable)
            assert mock_utime.call_args.args[1] == (file.mtime, file.mtime)
            mock_set_perms.assert_called_once()

    def test_download_skipped_when_insufficient_disk_space(self, tmp_path):
        """Size-aware guard skips the download when free space is too low."""
        file = File(
            filepath="big.bin",
            sha256sum="abc123",
            size=10 * 1024 * 1024,
            mtime=1000,
            ctime=1000,
        )

        fake_usage = MagicMock()
        fake_usage.free = 1 * 1024 * 1024  # only 1 MB free

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                    "MIN_FREE_DISK_SPACE": 100,  # 100 MB reserve
                },
            ),
            patch("hsync_client.transfer.disk_usage", return_value=fake_usage),
            patch("hsync_client.transfer.get") as mock_get,
            patch("hsync_client.transfer.replace") as mock_replace,
        ):
            download_file(file)
            # Guard fires before any network request or final commit
            mock_get.assert_not_called()
            mock_replace.assert_not_called()

    def test_sha256_mismatch_returns_without_saving(self, tmp_path):
        """SHA-256 mismatch logs error and returns."""
        file = File(filepath="file.txt", sha256sum="expected_sha256", size=100)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.iter_content = MagicMock(return_value=[b"test content"])

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.compute_sha256", return_value="different_sha256"),
            patch("hsync_client.transfer.replace") as mock_replace,
            patch("hsync_client.transfer.remove"),
        ):
            download_file(file)
            mock_replace.assert_not_called()


class TestDownloadRetry:
    """Retry/backoff and transfer_failed surfacing for downloads."""

    def test_transient_error_retries_then_emits_transfer_failed(self, tmp_path):
        """A persistent transient error is retried then surfaced as transfer_failed."""
        file = File(filepath="file.txt", sha256sum="abc123", size=100)

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                    "TRANSFER_RETRY_ATTEMPTS": 3,
                    "TRANSFER_RETRY_MIN_WAIT": 0,
                    "TRANSFER_RETRY_MAX_WAIT": 0,
                },
            ),
            patch("hsync_client.transfer.get") as mock_get,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.emit_event") as mock_emit,
        ):
            mock_get.side_effect = RequestException("flaky network")
            download_file(file)
            # Exhausted all attempts before giving up.
            assert mock_get.call_count == 3
            # Surfaced exactly one transfer_failed event for the UI.
            failed = [
                c for c in mock_emit.call_args_list if c.args[0] == UiEventName.transfer_failed
            ]
            assert len(failed) == 1
            assert failed[0].kwargs["op"] == "download"


class TestDownloadFileResume:
    """Tests for download resume functionality."""

    def test_download_with_existing_partial_file(self, tmp_path):
        """Test download resumes from existing partial file.

        The checksum is verified with the real incremental digest (no
        ``compute_sha256`` mock — the hash is computed while streaming), so
        ``sha256sum`` is the true hash of the resumed file: the on-disk
        partial prefix plus the newly downloaded chunk.
        """
        import hashlib

        partial_prefix = b"partial content"
        new_chunk = b"more content"
        file = File(
            filepath="file.txt",
            sha256sum=hashlib.sha256(partial_prefix + new_chunk).hexdigest(),
            size=len(partial_prefix + new_chunk),
            mtime=1000,
            ctime=1000,
        )

        # Create partial file
        partial_file = tmp_path / "file.txt.part"
        partial_file.write_bytes(partial_prefix)

        mock_response = MagicMock()
        mock_response.status_code = 206
        mock_response.headers = {
            "Content-Encoding": "",
            "Content-Range": (
                f"bytes {len(partial_prefix)}-{len(partial_prefix + new_chunk) - 1}/"
                f"{len(partial_prefix + new_chunk)}"
            ),
            "Content-Length": str(len(new_chunk)),
        }
        mock_response.iter_content = MagicMock(return_value=[new_chunk])

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.replace"),
            patch("hsync_client.transfer.remove") as mock_remove,
            patch("hsync_client.transfer.utime"),
            patch("hsync_client.transfer.set_permissions"),
        ):
            download_file(file)
            # Should append to partial file, not restart it
            mock_remove.assert_not_called()
            assert partial_file.read_bytes() == partial_prefix + new_chunk

    def test_download_partial_file_larger_than_expected(self, tmp_path):
        """Test download restarts when partial file is too large."""
        file = File(
            filepath="file.txt",
            sha256sum="abc123def",
            size=100,
            mtime=1000,
            ctime=1000,
        )

        # Create partial file larger than expected
        partial_file = tmp_path / "file.txt.part"
        partial_file.write_text("x" * 200)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Encoding": ""}
        mock_response.iter_content = MagicMock(return_value=[b"content"])
        mock_response.content = b"content"

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.compute_sha256", return_value="abc123def"),
            patch("hsync_client.transfer.replace"),
            patch("hsync_client.transfer.remove") as mock_remove,
            patch("hsync_client.transfer.utime"),
            patch("hsync_client.transfer.set_permissions"),
        ):
            download_file(file)
            # Should remove the too-large partial file
            mock_remove.assert_called()

    def test_download_local_file_up_to_date(self, tmp_path):
        """Test download skips when local file is up to date."""
        # Create local file with newer mtime
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")

        file = File(
            filepath="file.txt",
            sha256sum="abc123",
            size=7,
            mtime=1000,  # Older than actual file
            ctime=1000,
        )

        import os

        # Set mtime to be newer than file.mtime
        os.utime(test_file, (2000, 2000))

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get") as mock_get,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            download_file(file)
            # Should not download
            mock_get.assert_not_called()

    def test_download_416_range_not_satisfiable(self, tmp_path):
        """Test download restarts when 416 is received."""
        file = File(
            filepath="file.txt",
            sha256sum="abc123def",
            size=1000,
            mtime=1000,
            ctime=1000,
        )

        # Create partial file
        partial_file = tmp_path / "file.txt.part"
        partial_file.write_text("partial")

        mock_response_416 = MagicMock()
        mock_response_416.status_code = 416

        # Second call succeeds
        mock_response_200 = MagicMock()
        mock_response_200.status_code = 200
        mock_response_200.headers = {"Content-Encoding": ""}
        mock_response_200.iter_content = MagicMock(return_value=[b"content"])
        mock_response_200.content = b"content"

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", side_effect=[mock_response_416, mock_response_200]),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.compute_sha256", return_value="abc123def"),
            patch("hsync_client.transfer.replace"),
            patch("hsync_client.transfer.remove") as mock_remove,
            patch("hsync_client.transfer.utime"),
            patch("hsync_client.transfer.set_permissions"),
        ):
            download_file(file)
            # Should remove partial file and retry
            mock_remove.assert_called()


class TestDownloadCompressed:
    """Tests for compressed download."""

    def test_fresh_compressed_download_writes_content(self, tmp_path):
        """Regression: a fresh (non-resumed) compressed download must write the file.

        The decompression body was once nested under the resume-offset branch,
        so a first-attempt compressed download wrote nothing and the checksum
        read then failed with ENOENT on the ``.part`` file (surfaced by the
        LAN black-box large-file test). Unlike ``test_download_compressed_response``
        this keeps the real filesystem — no ``compute_sha256``/``replace``
        mocks — so the write path is exercised end to end.
        """
        import hashlib

        import zstandard as zstd

        content = b"fresh compressed download content" * 100
        compressed = zstd.ZstdCompressor().compress(content)

        file = File(
            filepath="file.txt",
            sha256sum=hashlib.sha256(content).hexdigest(),
            size=len(content),
            mtime=1000,
            ctime=1000,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Encoding": "zstd"}
        mock_response.raw = io.BytesIO(compressed)

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            download_file(file)

        assert (tmp_path / "file.txt").read_bytes() == content

    def test_download_compressed_response(self, tmp_path):
        """Test download handles compressed response.

        The checksum is verified with the real incremental digest (no
        ``compute_sha256`` mock — the hash is computed while decompressing),
        so ``sha256sum`` is the true hash of the decompressed content.
        """
        import hashlib

        import zstandard as zstd

        original_content = b"test content for compression"
        file = File(
            filepath="file.txt",
            sha256sum=hashlib.sha256(original_content).hexdigest(),
            size=len(original_content),
            mtime=1000,
            ctime=1000,
        )

        # Compress test content
        compressor = zstd.ZstdCompressor()
        compressed_content = compressor.compress(original_content)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Encoding": "zstd"}
        mock_response.raw = io.BytesIO(compressed_content)

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {
                    "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
                    "REMOTE_BUCKET": "test",
                    "REQUEST_HEADERS": {"X-API-Key": "test"},
                    "LOCAL_BUCKET": str(tmp_path),
                },
            ),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer.replace") as mock_replace,
            patch("hsync_client.transfer.utime"),
            patch("hsync_client.transfer.set_permissions"),
        ):
            download_file(file)
            mock_replace.assert_called()


class TestDownloadChecksumFormat:
    """Non-ASCII / malformed wire checksums must not crash compare_digest."""

    def _config(self, tmp_path):
        return {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "LOCAL_BUCKET": str(tmp_path),
            "TRANSFER_RETRY_ATTEMPTS": 1,
        }

    def test_non_ascii_server_checksum_header_ignored(self, tmp_path):
        """A malformed X-Checksum-SHA256 header is skipped (check 2 still
        verifies integrity against the listing)."""
        import hashlib

        content = b"download me"
        file = File(
            filepath="f.txt",
            sha256sum=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"X-Checksum-SHA256": "é" * 64}
        mock_response.iter_content.return_value = [content]

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            download_file(file)

        assert (tmp_path / "f.txt").read_bytes() == content

    def test_non_ascii_listing_checksum_rejected_without_crash(self, tmp_path):
        """A listing carrying a non-ASCII sha256sum fails closed (no file,
        no TypeError from compare_digest)."""
        content = b"download me"
        file = File(filepath="f.txt", sha256sum="é" * 64, size=len(content))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.iter_content.return_value = [content]

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            download_file(file)

        assert not (tmp_path / "f.txt").exists()


class TestDownloadSizeCap:
    """MAX_INBOUND_SIZE is an absolute cap applied on top of the peer-declared
    size: a hostile server/master can no longer expand a decompression bomb
    (or stream an undeclared body) up to an arbitrary declaration."""

    def _config(self, tmp_path, max_inbound_size):
        return {
            "API": {"buckets": "http://localhost:8000/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {"X-API-Key": "test"},
            "LOCAL_BUCKET": str(tmp_path),
            # Disable retry/backoff so a single attempt is asserted.
            "TRANSFER_RETRY_ATTEMPTS": 1,
            "MAX_INBOUND_SIZE": max_inbound_size,
        }

    def test_write_cap_disabled_uses_declared_only(self):
        from hsync_client.utils import inbound_size_cap

        assert inbound_size_cap(100, 0) == 100
        assert inbound_size_cap(0, 0) is None
        assert inbound_size_cap(None, 0) is None

    def test_write_cap_enabled_bounds_declaration(self):
        from hsync_client.utils import inbound_size_cap

        assert inbound_size_cap(100, 50) == 50  # min(declared, cap)
        assert inbound_size_cap(30, 50) == 30
        assert inbound_size_cap(None, 50) == 50  # cap is the only bound
        assert inbound_size_cap(0, 50) == 50

    def test_compressed_bomb_beyond_absolute_cap_aborted(self, tmp_path):
        """A peer declaring a huge size is capped at MAX_INBOUND_SIZE."""
        import zstandard as zstd

        original_content = b"A" * 1000
        compressed_content = zstd.ZstdCompressor().compress(original_content)
        # Lie: the listing declares 1 GB so the declared-size cap would allow it.
        file = File(filepath="bomb.txt", sha256sum="abc123", size=1_000_000_000)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Encoding": "zstd"}
        mock_response.raw = io.BytesIO(compressed_content)

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path, 50)),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            with pytest.raises(PermanentDownloadError):
                download_file(file)

        assert not (tmp_path / "bomb.txt").exists()
        assert not (tmp_path / "bomb.txt.part").exists()

    def test_uncompressed_beyond_absolute_cap_aborted(self, tmp_path):
        """An uncompressed body larger than the cap is aborted and dropped."""
        content = b"A" * 1000
        file = File(filepath="big.txt", sha256sum="abc123", size=1000)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.iter_content.return_value = [content]

        # A permanent over-cap failure now raises PermanentDownloadError
        # (instead of returning silently) so the caller can skip the file on
        # subsequent resync cycles.
        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path, 50)),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            with pytest.raises(PermanentDownloadError, match="exceeds the maximum download size"):
                download_file(file)

        assert not (tmp_path / "big.txt").exists()
        assert not (tmp_path / "big.txt.part").exists()

    def test_download_within_cap_succeeds(self, tmp_path):
        """A download under the absolute cap is unaffected."""
        import hashlib

        content = b"legit content within the cap"
        file = File(
            filepath="ok.txt",
            sha256sum=hashlib.sha256(content).hexdigest(),
            size=len(content),
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.iter_content.return_value = [content]

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path, 1024)),
            patch("hsync_client.transfer.get", return_value=mock_response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
        ):
            download_file(file)

        assert (tmp_path / "ok.txt").read_bytes() == content


class TestPeerDownloadFallback:
    """Exact-hash peer downloads share resume state and fall back safely."""

    @staticmethod
    def _config(tmp_path):
        return {
            "API": {"buckets": "https://master:8443/api/v1/buckets"},
            "REMOTE_BUCKET": "test",
            "REQUEST_HEADERS": {"Authorization": "Bearer lan-key"},
            "LOCAL_BUCKET": str(tmp_path),
            "CID": "r" * 21,
            "HTTP_TIMEOUT": 5,
            "COMPRESSION_ENABLED": True,
            "MIN_FREE_DISK_SPACE": 0,
            "MAX_INBOUND_SIZE": 0,
            "TRANSFER_RETRY_ATTEMPTS": 1,
        }

    @staticmethod
    def _source(cid: str, *, compression: bool = False) -> LanPeerSource:
        return LanPeerSource(
            cid=cid,
            download_url="https://10.0.0.2:8443/api/v1/buckets/test/files/download",
            cert_fingerprint="a" * 64,
            compression=compression,
        )

    @staticmethod
    def _response(content: bytes, digest: str, *, status: int = 200) -> MagicMock:
        response = MagicMock()
        response.status_code = status
        response.headers = {
            "Content-Length": str(len(content)),
            "X-Checksum-SHA256": digest,
        }
        response.iter_content.return_value = [content]
        return response

    @staticmethod
    def _resolver(sources: tuple[LanPeerSource, ...]) -> MagicMock:
        resolver = MagicMock()
        resolver.candidates_for.return_value = sources
        resolver.authorize.return_value = True
        resolver.request_headers.return_value = {"Accept-Encoding": "identity"}
        return resolver

    def test_peer_success_uses_explicit_pin_and_emits_source_cid(self, tmp_path):
        """A verified peer body never contacts the master and identifies its source."""
        import hashlib

        content = b"peer payload"
        digest = hashlib.sha256(content).hexdigest()
        file = File(filepath="peer.txt", sha256sum=digest, size=len(content))
        source = self._source("s" * 21)
        resolver = self._resolver((source,))
        response = self._response(content, digest)

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
            patch("hsync_client.transfer.get", return_value=response) as mock_get,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer._record_synced_base"),
            patch("hsync_client.transfer.emit_event") as mock_emit,
            patch("hsync_client.transfer.log.info") as mock_log,
        ):
            download_file(file, source_resolver=resolver)

        assert (tmp_path / "peer.txt").read_bytes() == content
        assert mock_get.call_count == 1
        request = mock_get.call_args
        assert request.kwargs["cert_fingerprint"] == source.cert_fingerprint
        assert request.kwargs["params"] == {
            "filename": "peer.txt",
            "sha256": digest,
            "cid": "r" * 21,
        }
        assert any(call.kwargs.get("source_cid") == source.cid for call in mock_emit.call_args_list)
        assert any(
            f"source_cid={source.cid}" in str(call.args[0]) for call in mock_log.call_args_list
        )
        resolver.record_success.assert_called_once()

    def test_delta_is_attempted_before_peer_and_master_full_fallback(self):
        """The frozen order is master delta, ranked peer full, then master full."""
        from hsync_client.transfer import _download_file_attempt, _PeerSourceFailure

        file = File(filepath="ordered.bin", sha256sum="d" * 64, size=10)
        source = self._source("s" * 21)
        resolver = self._resolver((source,))
        calls: list[tuple[bool, LanPeerSource | None, bool]] = []

        def attempt(_file, _observer, **kwargs):
            calls.append(
                (
                    bool(kwargs.get("delta_only")),
                    kwargs.get("source"),
                    bool(kwargs.get("allow_delta", True)),
                )
            )
            if kwargs.get("delta_only"):
                return False
            if kwargs.get("source") is not None:
                raise _PeerSourceFailure(PeerFailureKind.conflict, "stale")
            return True

        with patch("hsync_client.transfer._download_from_source_attempt", side_effect=attempt):
            _download_file_attempt(file, source_resolver=resolver)

        assert calls == [
            (True, None, True),
            (False, source, False),
            (False, None, False),
        ]
        resolver.record_failure.assert_called_once_with(source, PeerFailureKind.conflict)

    def test_peer_404_advances_to_master_without_retrying_peer(self, tmp_path):
        """A peer-local 404 advances immediately and master fallback stays unpinned to it."""
        import hashlib

        content = b"master payload"
        digest = hashlib.sha256(content).hexdigest()
        file = File(filepath="fallback.txt", sha256sum=digest, size=len(content))
        source = self._source("s" * 21)
        resolver = self._resolver((source,))
        peer_response = MagicMock(status_code=404, headers={})
        master_response = self._response(content, digest)

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
            patch(
                "hsync_client.transfer.get",
                side_effect=[peer_response, master_response],
            ) as mock_get,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer._record_synced_base"),
        ):
            download_file(file, source_resolver=resolver)

        assert (tmp_path / "fallback.txt").read_bytes() == content
        assert mock_get.call_count == 2
        assert "cert_fingerprint" in mock_get.call_args_list[0].kwargs
        assert "cert_fingerprint" not in mock_get.call_args_list[1].kwargs
        resolver.record_failure.assert_called_once_with(source, PeerFailureKind.not_found)

    def test_peer_disconnect_resumes_same_partial_from_master(self, tmp_path):
        """An interrupted exact body preserves its prefix for the next source."""
        import hashlib

        prefix = b"shared-prefix-"
        suffix = b"master-tail"
        content = prefix + suffix
        digest = hashlib.sha256(content).hexdigest()
        file = File(filepath="resume.txt", sha256sum=digest, size=len(content))
        source = self._source("s" * 21)
        resolver = self._resolver((source,))
        peer_response = self._response(content, digest)

        def interrupted_chunks(*_args, **_kwargs):
            yield prefix
            raise RequestException("connection reset")

        peer_response.iter_content.side_effect = interrupted_chunks
        master_response = self._response(suffix, digest, status=206)
        master_response.headers["Content-Range"] = (
            f"bytes {len(prefix)}-{len(content) - 1}/{len(content)}"
        )

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
            patch(
                "hsync_client.transfer.get",
                side_effect=[peer_response, master_response],
            ) as mock_get,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer._record_synced_base"),
        ):
            download_file(file, source_resolver=resolver)

        assert (tmp_path / "resume.txt").read_bytes() == content
        master_headers = mock_get.call_args_list[1].kwargs["headers"]
        assert master_headers["Range"] == f"bytes={len(prefix)}-"
        assert master_headers["Accept-Encoding"] == "identity"
        resolver.record_failure.assert_called_once_with(source, PeerFailureKind.transport)

    def test_bad_peer_hash_is_deleted_before_next_peer(self, tmp_path):
        """A complete wrong body cannot become a resume prefix for another source."""
        import hashlib

        content = b"correct-data"
        digest = hashlib.sha256(content).hexdigest()
        file = File(filepath="hash.txt", sha256sum=digest, size=len(content))
        first = self._source("a" * 21)
        second = self._source("b" * 21)
        resolver = self._resolver((first, second))
        bad_response = self._response(b"wrong--data!", digest)
        good_response = self._response(content, digest)

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
            patch(
                "hsync_client.transfer.get",
                side_effect=[bad_response, good_response],
            ) as mock_get,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer._record_synced_base"),
        ):
            download_file(file, source_resolver=resolver)

        assert (tmp_path / "hash.txt").read_bytes() == content
        assert "Range" not in mock_get.call_args_list[1].kwargs["headers"]
        resolver.record_failure.assert_called_once_with(first, PeerFailureKind.hash_mismatch)
        assert resolver.record_success.call_args.args[0] == second

    def test_complete_short_peer_body_is_deleted_before_next_peer(self, tmp_path):
        """A clean short EOF is malformed, not an interrupted resumable prefix."""
        import hashlib

        content = b"complete payload"
        digest = hashlib.sha256(content).hexdigest()
        file = File(filepath="short.txt", sha256sum=digest, size=len(content))
        first = self._source("a" * 21)
        second = self._source("b" * 21)
        resolver = self._resolver((first, second))
        short_response = self._response(content[:5], digest)
        good_response = self._response(content, digest)

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
            patch(
                "hsync_client.transfer.get",
                side_effect=[short_response, good_response],
            ) as mock_get,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer._record_synced_base"),
        ):
            download_file(file, source_resolver=resolver)

        assert (tmp_path / "short.txt").read_bytes() == content
        assert "Range" not in mock_get.call_args_list[1].kwargs["headers"]
        resolver.record_failure.assert_called_once_with(first, PeerFailureKind.malformed)

    def test_zero_byte_listing_rejects_peer_body_before_fallback(self, tmp_path):
        """An exact empty file cannot consume the generic inbound-size allowance."""
        import hashlib

        digest = hashlib.sha256(b"").hexdigest()
        file = File(filepath="empty.txt", sha256sum=digest, size=0)
        first = self._source("a" * 21)
        second = self._source("b" * 21)
        resolver = self._resolver((first, second))
        oversized = self._response(b"unexpected", digest)
        empty = self._response(b"", digest)

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
            patch("hsync_client.transfer.get", side_effect=[oversized, empty]),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer._record_synced_base"),
        ):
            download_file(file, source_resolver=resolver)

        assert (tmp_path / "empty.txt").read_bytes() == b""
        resolver.record_failure.assert_called_once_with(first, PeerFailureKind.malformed)

    def test_resumed_master_hash_failure_retries_once_from_zero(self, tmp_path):
        """A poisoned prefix is discarded before one clean canonical-master retry."""
        import hashlib

        content = b"good"
        digest = hashlib.sha256(content).hexdigest()
        file = File(filepath="master.txt", sha256sum=digest, size=len(content))
        (tmp_path / "master.txt.part").write_bytes(b"x")
        resumed = self._response(b"ood", digest, status=206)
        resumed.headers["Content-Range"] = "bytes 1-3/4"
        fresh = self._response(content, digest)

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
            patch("hsync_client.transfer.get", side_effect=[resumed, fresh]) as mock_get,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer._record_synced_base"),
        ):
            download_file(file)

        assert (tmp_path / "master.txt").read_bytes() == content
        assert mock_get.call_args_list[0].kwargs["headers"]["Range"] == "bytes=1-"
        assert "Range" not in mock_get.call_args_list[1].kwargs["headers"]

    def test_peer_zstd_content_length_counts_wire_bytes(self, tmp_path):
        """Compressed Content-Length is compared with wire, not decoded, bytes."""
        import hashlib

        import zstandard as zstd

        content = b"compressible" * 100
        compressed = zstd.ZstdCompressor().compress(content)
        digest = hashlib.sha256(content).hexdigest()
        file = File(filepath="compressed.txt", sha256sum=digest, size=len(content))
        source = self._source("s" * 21, compression=True)
        resolver = self._resolver((source,))
        resolver.request_headers.return_value = {"Accept-Encoding": "zstd"}
        response = MagicMock()
        response.status_code = 200
        response.headers = {
            "Content-Encoding": "zstd",
            "Content-Length": str(len(compressed)),
            "X-Checksum-SHA256": digest,
        }
        response.raw = io.BytesIO(compressed)

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
            patch("hsync_client.transfer.get", return_value=response),
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer._record_synced_base"),
        ):
            download_file(file, source_resolver=resolver)

        assert (tmp_path / "compressed.txt").read_bytes() == content
        assert resolver.record_success.call_args.kwargs["bytes_transferred"] == len(compressed)

    def test_complete_malformed_zstd_body_is_discarded_before_fallback(self, tmp_path):
        """A complete invalid zstd frame cannot be reused as a resume prefix."""
        import hashlib

        content = b"valid peer payload"
        digest = hashlib.sha256(content).hexdigest()
        file = File(filepath="malformed.txt", sha256sum=digest, size=len(content))
        first = self._source("a" * 21, compression=True)
        second = self._source("b" * 21)
        resolver = self._resolver((first, second))
        resolver.request_headers.side_effect = [
            {"Accept-Encoding": "zstd"},
            {"Accept-Encoding": "identity"},
        ]
        malformed = b"not-a-zstd-frame"
        bad_response = MagicMock(
            status_code=200,
            headers={
                "Content-Encoding": "zstd",
                "Content-Length": str(len(malformed)),
                "X-Checksum-SHA256": digest,
            },
        )
        bad_response.raw = io.BytesIO(malformed)
        good_response = self._response(content, digest)

        with (
            patch("hsync_client.transfer.CONFIG", self._config(tmp_path)),
            patch(
                "hsync_client.transfer.get",
                side_effect=[bad_response, good_response],
            ) as mock_get,
            patch("hsync_client.transfer.get_ssl_verify", return_value=False),
            patch("hsync_client.transfer._record_synced_base"),
        ):
            download_file(file, source_resolver=resolver)

        assert (tmp_path / "malformed.txt").read_bytes() == content
        assert "Range" not in mock_get.call_args_list[1].kwargs["headers"]
        resolver.record_failure.assert_called_once_with(first, PeerFailureKind.malformed)


class TestDownloadPathLocking:
    """Concurrency tests for per-path serialization of downloads."""

    def _run_concurrent(self, files, tmp_path):
        """Run download_file on *files* from threads, tracking max concurrency."""
        guard = threading.Lock()
        start = threading.Barrier(len(files))
        active = 0
        max_active = 0

        def fake_attempt(_file):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with guard:
                active -= 1

        with (
            patch(
                "hsync_client.transfer.CONFIG",
                {"LOCAL_BUCKET": str(tmp_path), "TRANSFER_RETRY_ATTEMPTS": 1},
            ),
            patch("hsync_client.transfer._download_file_attempt", side_effect=fake_attempt),
        ):

            def worker(f):
                start.wait()
                download_file(f)

            threads = [threading.Thread(target=worker, args=(f,)) for f in files]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        return max_active

    def test_same_path_downloads_are_serialized(self, tmp_path):
        """Two downloads of the same path never overlap their critical section."""
        files = [
            File(filepath="dir/file.txt", sha256sum="abc", size=10),
            File(filepath="dir/file.txt", sha256sum="abc", size=10),
        ]
        assert self._run_concurrent(files, tmp_path) == 1

    def test_distinct_path_downloads_run_concurrently(self, tmp_path):
        """Downloads of distinct paths are not serialized by the per-path lock."""
        files = [
            File(filepath="dir/a.txt", sha256sum="abc", size=10),
            File(filepath="dir/b.txt", sha256sum="abc", size=10),
        ]
        assert self._run_concurrent(files, tmp_path) == 2
