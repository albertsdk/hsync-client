# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Regression tests for the upload/download bandwidth limits.

They cover the paths that previously bypassed ``MAX_BANDWIDTH_*``:
compressed downloads (wire-byte counting reader) and P2P serving, plus the
runtime ``SET_BANDWIDTH`` command.
"""

import asyncio
import io

import hsync_client.p2p_server as p2p_server
import hsync_client.preview as preview
import hsync_client.transfer as transfer


class TestCompressedDownloadThrottle:
    def test_read_throttles_wire_bytes(self, monkeypatch):
        calls: list[tuple[int, int, str]] = []
        monkeypatch.setattr(
            transfer,
            "get_throttle_delay",
            lambda n, limit, key: calls.append((n, limit, key)) or 0.0,
        )
        reader = transfer._CountingReader(io.BytesIO(b"x" * 100), bandwidth_limit=1000)

        assert reader.read(100) == b"x" * 100

        assert reader.bytes_read == 100
        assert calls == [(100, 1000, "download")]

    def test_readinto_throttles_wire_bytes(self, monkeypatch):
        calls: list[tuple[int, int, str]] = []
        monkeypatch.setattr(
            transfer,
            "get_throttle_delay",
            lambda n, limit, key: calls.append((n, limit, key)) or 0.0,
        )
        reader = transfer._CountingReader(io.BytesIO(b"y" * 64), bandwidth_limit=512)

        buffer = bytearray(64)
        assert reader.readinto(buffer) == 64

        assert calls == [(64, 512, "download")]

    def test_no_limit_does_not_throttle(self, monkeypatch):
        calls: list[tuple[int, int, str]] = []
        monkeypatch.setattr(
            transfer,
            "get_throttle_delay",
            lambda n, limit, key: calls.append((n, limit, key)) or 0.0,
        )
        reader = transfer._CountingReader(io.BytesIO(b"z" * 10))

        assert reader.read(10) == b"z" * 10
        assert calls == []


class TestP2PServingThrottle:
    def test_uses_the_configured_upload_limit(self, monkeypatch):
        calls: list[tuple[int, int, str]] = []
        slept: list[float] = []
        monkeypatch.setitem(p2p_server.CONFIG, "MAX_BANDWIDTH_UPLOAD", 2048)
        monkeypatch.setattr(
            p2p_server,
            "get_throttle_delay",
            lambda n, limit, key: calls.append((n, limit, key)) or 0.5,
        )
        monkeypatch.setattr(p2p_server.time, "sleep", lambda delay: slept.append(delay))

        p2p_server._throttle_upload(1024)

        assert calls == [(1024, 2048, "upload")]
        assert slept == [0.5]

    def test_no_limit_is_a_noop(self, monkeypatch):
        calls: list[tuple[int, int, str]] = []
        monkeypatch.setitem(p2p_server.CONFIG, "MAX_BANDWIDTH_UPLOAD", None)
        monkeypatch.setattr(
            p2p_server,
            "get_throttle_delay",
            lambda n, limit, key: calls.append((n, limit, key)) or 0.0,
        )

        p2p_server._throttle_upload(1024)

        assert calls == []


class TestRuntimeSetBandwidth:
    def test_updates_both_limits(self, monkeypatch):
        class _Log:
            def info(self, *_args, **_kwargs):
                pass

            def warning(self, *_args, **_kwargs):
                pass

        monkeypatch.setitem(preview.CONFIG, "MAX_BANDWIDTH_UPLOAD", None)
        monkeypatch.setitem(preview.CONFIG, "MAX_BANDWIDTH_DOWNLOAD", None)

        asyncio.run(preview._handle_set_bandwidth(_Log(), upload=2048, download=0))

        assert preview.CONFIG["MAX_BANDWIDTH_UPLOAD"] == 2048
        assert preview.CONFIG["MAX_BANDWIDTH_DOWNLOAD"] is None

    def test_rejects_an_invalid_value_without_touching_the_other(self, monkeypatch):
        class _Log:
            def info(self, *_args, **_kwargs):
                pass

            def warning(self, *_args, **_kwargs):
                pass

        monkeypatch.setitem(preview.CONFIG, "MAX_BANDWIDTH_UPLOAD", 4096)
        monkeypatch.setitem(preview.CONFIG, "MAX_BANDWIDTH_DOWNLOAD", 8192)

        asyncio.run(preview._handle_set_bandwidth(_Log(), upload="not-a-number"))

        assert preview.CONFIG["MAX_BANDWIDTH_UPLOAD"] == 4096
        assert preview.CONFIG["MAX_BANDWIDTH_DOWNLOAD"] == 8192
