# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for the P2P master download checksum cache (``p2p_server``).

The cache removes one full disk read per served download by keying each
file's SHA-256 on its stat identity. These tests pin: cache behavior
(hit/miss/LRU eviction), the ``_stat_identity``/_same_open_file contract,
and the end-to-end behavior of ``_handle_download`` (checksum reused for
an unchanged file, recomputed after a content change, header values match
the actual content).
"""

import time
from hashlib import sha256 as _sha256
from http.client import HTTPConnection
from unittest.mock import patch

import pytest

from hsync_client.p2p_peers import get_free_port
from hsync_client.p2p_server import (
    P2PServer,
    _ChecksumCache,
    _hash_open_file,
    _same_open_file,
    _stat_identity,
)

from .p2p_helpers import unauth_write_config


def _fake_stat(dev: int, ino: int, size: int, mtime_ns: int, ctime_ns: int):
    stat = type("FakeStat", (), {})()
    stat.st_dev = dev
    stat.st_ino = ino
    stat.st_size = size
    stat.st_mtime_ns = mtime_ns
    stat.st_ctime_ns = ctime_ns
    return stat


class TestChecksumCache:
    def test_put_get_roundtrip(self):
        cache = _ChecksumCache(maxsize=4)
        identity = (1, 2, 3, 4, 5)
        cache.put(identity, "ab" * 32)
        assert cache.get(identity) == "ab" * 32

    def test_get_unknown_is_none(self):
        cache = _ChecksumCache(maxsize=4)
        assert cache.get((1, 2, 3, 4, 5)) is None

    def test_different_identity_is_miss(self):
        cache = _ChecksumCache(maxsize=4)
        cache.put((1, 2, 3, 4, 5), "ab" * 32)
        assert cache.get((1, 2, 3, 4, 6)) is None

    def test_lru_eviction(self):
        cache = _ChecksumCache(maxsize=2)
        cache.put((1, 0, 0, 0, 0), "a" * 64)
        cache.put((2, 0, 0, 0, 0), "b" * 64)
        cache.get((1, 0, 0, 0, 0))  # refresh entry 1 → entry 2 becomes LRU
        cache.put((3, 0, 0, 0, 0), "c" * 64)
        assert cache.get((2, 0, 0, 0, 0)) is None
        assert cache.get((1, 0, 0, 0, 0)) == "a" * 64
        assert cache.get((3, 0, 0, 0, 0)) == "c" * 64


class TestStatIdentity:
    def test_same_open_file_matches_stat_identity(self):
        stat_a = _fake_stat(1, 2, 3, 4, 5)
        stat_b = _fake_stat(1, 2, 3, 4, 5)
        assert _same_open_file(stat_a, stat_b)
        assert _stat_identity(stat_a) == (1, 2, 3, 4, 5)

    def test_content_metadata_change_breaks_identity(self):
        stat_a = _fake_stat(1, 2, 3, 4, 5)
        for field, value in (
            ("st_dev", 9),
            ("st_ino", 9),
            ("st_size", 9),
            ("st_mtime_ns", 9),
            ("st_ctime_ns", 9),
        ):
            changed = _fake_stat(1, 2, 3, 4, 5)
            setattr(changed, field, value)
            assert not _same_open_file(stat_a, changed), field


class TestDownloadChecksumCacheIntegration:
    """End-to-end: repeated downloads reuse the cached checksum."""

    @pytest.fixture
    def fresh_cache(self):
        cache = _ChecksumCache(maxsize=8)
        with patch("hsync_client.p2p_server._CHECKSUM_CACHE", cache):
            yield cache

    @pytest.fixture
    def server(self):
        port = get_free_port()
        srv = P2PServer(port=port)
        srv.start()
        time.sleep(0.1)
        yield srv, port
        srv.stop()

    def _download(self, port: int, filename: str, tmp_path) -> tuple[int, bytes, str | None]:
        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
            conn = HTTPConnection("localhost", port, timeout=5)
            conn.request("GET", f"/api/v1/buckets/test/files/download?filename={filename}")
            response = conn.getresponse()
            body = response.read()
            checksum = response.headers.get("X-Checksum-Sha256")
            status = response.status
            conn.close()
            return status, body, checksum

    def test_repeated_download_hashes_once(self, server, tmp_path, fresh_cache):
        """An unchanged file is hashed on the first request only."""
        _srv, port = server
        (tmp_path / "cached.txt").write_bytes(b"stable content")

        real_hash = _hash_open_file
        calls: list[int] = []

        def counting_hash(stream):
            calls.append(1)
            return real_hash(stream)

        with patch("hsync_client.p2p_server._hash_open_file", counting_hash):
            status1, body1, checksum1 = self._download(port, "cached.txt", tmp_path)
            status2, body2, checksum2 = self._download(port, "cached.txt", tmp_path)
            status3, body3, checksum3 = self._download(port, "cached.txt", tmp_path)

        assert (status1, status2, status3) == (200, 200, 200)
        assert body1 == body2 == body3 == b"stable content"
        assert checksum1 == checksum2 == checksum3 == _sha256(b"stable content").hexdigest()
        assert len(calls) == 1, f"expected 1 hash pass, got {len(calls)}"

    def test_content_change_recomputes_checksum(self, server, tmp_path, fresh_cache):
        """A content change (new stat identity) evicts the stale entry."""
        _srv, port = server
        target = tmp_path / "mutating.txt"
        target.write_bytes(b"version one")

        _status1, _body1, checksum1 = self._download(port, "mutating.txt", tmp_path)
        target.write_bytes(b"version two!")
        _status2, _body2, checksum2 = self._download(port, "mutating.txt", tmp_path)

        assert checksum1 == _sha256(b"version one").hexdigest()
        assert checksum2 == _sha256(b"version two!").hexdigest()
        assert checksum1 != checksum2

    def test_checksum_header_matches_served_content(self, server, tmp_path, fresh_cache):
        _srv, port = server
        (tmp_path / "h.txt").write_bytes(b"header check")
        _status, body, checksum = self._download(port, "h.txt", tmp_path)
        assert checksum == _sha256(body).hexdigest()
