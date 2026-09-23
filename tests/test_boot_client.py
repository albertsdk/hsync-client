# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Unit tests for the ``boot/1`` QUIC client and the STUN-echo extraction.

These tests never open a socket: they cover the frame builders (validated
against the ``hsync-common`` mirror), the fail-fast refusal of an invalid frame
before any connection, the QUIC configuration (ALPN, verification mode) and the
server-reflexive address extraction from a validated ``ack``.
"""

from __future__ import annotations

import asyncio
import ssl

import pytest
from hsync_common.protocol.bootnode import (
    BOOT_ALPN,
    CAPACITY_MAX,
    validate_boot_ack,
    validate_boot_frame,
)

from hsync_client.boot_client import (
    BootnodeClient,
    BootnodeProtocolError,
    BootnodeUnreachable,
    build_leave_frame,
    build_query_frame,
    build_refresh_frame,
    build_register_frame,
)
from hsync_client.stun import ObservedAddress, observed_address

CID = "A1b2C3d4E5f6G7h8I9j0K"
TOKEN = "a" * 64
ACK = {
    "protocol_version": "boot/1",
    "type": "ack",
    "observed_endpoint": {"proto": "udp", "ip": "198.51.100.7", "port": 54321},
    "ttl": 60,
    "server_time": "2026-01-01T00:00:00Z",
}


class TestFrameBuilders:
    def test_register_frame_validates(self):
        frame = build_register_frame(
            cid=CID,
            bucket="shared",
            cert_fp="AA:BB",
            version="1.0.0",
            group="home",
            bucket_token=TOKEN,
            capacity=5,
            endpoints=[{"proto": "tcp", "host": "198.51.100.7", "port": 8443}],
            connected=3,
        )
        assert validate_boot_frame(frame) is not None
        assert frame["capacity"] == 5

    def test_refresh_frame_validates(self):
        frame = build_refresh_frame(cid=CID, bucket="shared", group="home", connected=2)
        assert validate_boot_frame(frame) is not None
        assert "endpoints" not in frame  # refresh is TTL + connected only

    def test_leave_frame_validates(self):
        frame = build_leave_frame(cid=CID, bucket="shared", bucket_token=TOKEN)
        assert validate_boot_frame(frame) is not None

    def test_query_frame_validates(self):
        frame = build_query_frame(bucket="shared", group="home", limit=10)
        assert validate_boot_frame(frame) is not None
        assert frame["limit"] == 10

    def test_optional_fields_omitted_when_none(self):
        frame = build_register_frame(cid=CID, bucket="shared", cert_fp="AA:BB", version="1.0.0")
        assert "group" not in frame
        assert "bucket_token" not in frame
        assert "connected" not in frame
        assert frame["capacity"] == 0
        assert frame["endpoints"] == []


class TestFailFast:
    def test_invalid_register_is_refused_before_connecting(self):
        """An out-of-range capacity never reaches the network."""
        client = BootnodeClient("127.0.0.1", 1)
        frame = build_register_frame(
            cid=CID,
            bucket="shared",
            cert_fp="AA:BB",
            version="1.0.0",
            capacity=CAPACITY_MAX + 1,
        )
        with pytest.raises(BootnodeProtocolError):
            client.register(frame)

    def test_unknown_frame_type_is_refused(self):
        client = BootnodeClient("127.0.0.1", 1)
        with pytest.raises(BootnodeProtocolError):
            client._exchange({"protocol_version": "boot/1", "type": "bogus"}, expect=None)


class TestUnreachableError:
    def test_textless_connection_error_yields_a_non_empty_message(self, monkeypatch):
        """aioquic raises ``ConnectionError`` with no text; the message must still identify it."""
        client = BootnodeClient("boot.example.net", 443)

        async def boom(self, payload, *, expect):
            raise ConnectionError()

        monkeypatch.setattr(BootnodeClient, "_exchange_async", boom)
        with pytest.raises(BootnodeUnreachable) as excinfo:
            client.query(build_query_frame(bucket="shared"))
        message = str(excinfo.value)
        assert "boot.example.net:443" in message
        assert message.rstrip().endswith("ConnectionError")


class TestLeave:
    def test_leave_does_not_wait_for_a_reply(self, monkeypatch):
        """``leave`` has no response frame: never block on a read."""
        calls: dict[str, bool] = {}

        class _Reader:
            async def readline(self):
                calls["read"] = True
                await asyncio.sleep(60)

        class _Writer:
            def __init__(self):
                self.closed = False
                self.written = b""

            def write(self, data):
                self.written = data

            async def drain(self):
                return None

            def close(self):
                self.closed = True

        writer = _Writer()

        class _Manager:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def create_stream(self):
                return _Reader(), writer

        monkeypatch.setattr("hsync_client.boot_client.connect", lambda *_a, **_k: _Manager())
        client = BootnodeClient("127.0.0.1", 4433, timeout=5.0)
        client.leave(build_leave_frame(cid=CID, bucket="shared"))
        assert writer.closed is True
        assert b'"type":"leave"' in writer.written
        assert "read" not in calls


class TestQuicConfiguration:
    def test_alpn_and_insecure(self):
        config = BootnodeClient("boot.example.net", 443, insecure=True)._quic_configuration()
        assert config.alpn_protocols == [BOOT_ALPN]
        assert config.verify_mode == ssl.CERT_NONE

    def test_verified_by_default(self):
        config = BootnodeClient("boot.example.net", 443)._quic_configuration()
        assert config.verify_mode == ssl.CERT_REQUIRED


class TestStunEcho:
    def test_observed_address(self):
        ack = validate_boot_ack(ACK)
        assert ack is not None
        assert observed_address(ack) == ObservedAddress(host="198.51.100.7", port=54321)

    def test_malformed_ack_is_rejected_by_the_mirror(self):
        bad = {**ACK, "observed_endpoint": {"proto": "udp", "ip": "198.51.100.7"}}
        assert validate_boot_ack(bad) is None
