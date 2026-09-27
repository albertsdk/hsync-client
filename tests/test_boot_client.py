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
from cryptography import x509
from cryptography.x509.oid import NameOID
from hsync_common import ssl_utils
from hsync_common.protocol.bootnode import (
    BOOT_ALPN,
    CAPACITY_MAX,
    validate_boot_ack,
    validate_boot_frame,
)

from hsync_client.boot_client import (
    BootnodeClient,
    BootnodeFingerprintMismatch,
    BootnodeProtocolError,
    BootnodeRefused,
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
        # The election advertisement is always part of the frame (defaults).
        assert frame["server_ready"] is False
        assert frame["force_master"] is False
        assert frame["never_master"] is False
        assert frame["startup_time"] == 0.0
        assert frame["sync_direction"] == "bidirectional"

    def test_register_frame_carries_the_election_advertisement(self):
        frame = build_register_frame(
            cid=CID,
            bucket="shared",
            cert_fp="AA:BB",
            version="1.0.0",
            server_ready=True,
            never_master=True,
            startup_time=1760000000.5,
            sync_direction="upload-only",
        )
        assert validate_boot_frame(frame) is not None
        assert frame["server_ready"] is True
        assert frame["never_master"] is True
        assert frame["startup_time"] == 1760000000.5
        assert frame["sync_direction"] == "upload-only"

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

    def test_pin_and_tofu_disable_webpki(self):
        # Trust comes from the fingerprint check, not the system CA store.
        pinned = BootnodeClient("boot.example.net", 443, fingerprints=("a" * 64,))
        assert pinned._quic_configuration().verify_mode == ssl.CERT_NONE
        tofu = BootnodeClient("boot.example.net", 443, tofu=True)
        assert tofu._quic_configuration().verify_mode == ssl.CERT_NONE


def _fake_cert():
    key = ssl_utils.generate_ecdsa_private_key()
    cert = ssl_utils.build_self_signed_certificate(
        key,
        subject_attrs=[x509.NameAttribute(NameOID.COMMON_NAME, "boot.local")],
        san_list=[x509.DNSName("boot.local")],
        validity_days=1,
    )
    return cert, ssl_utils.compute_cert_fingerprint(cert)


class _FakeTls:
    def __init__(self, cert):
        self._peer_certificate = cert


class _FakePeer:
    def __init__(self, cert):
        self._quic = type("_Quic", (), {"tls": _FakeTls(cert)})()


class TestPinVerify:
    def test_explicit_pin_match(self):
        cert, fp = _fake_cert()
        client = BootnodeClient("boot.example.net", 443, fingerprints=(fp,))
        client._verify_peer(_FakePeer(cert))  # no raise

    def test_explicit_pin_mismatch_raises(self):
        cert, _ = _fake_cert()
        client = BootnodeClient("boot.example.net", 443, fingerprints=("f" * 64,))
        with pytest.raises(BootnodeFingerprintMismatch):
            client._verify_peer(_FakePeer(cert))

    def test_rotation_accepts_second_pin(self):
        cert, fp = _fake_cert()
        client = BootnodeClient("boot.example.net", 443, fingerprints=("0" * 64, fp))
        client._verify_peer(_FakePeer(cert))  # second pin matches

    def test_tofu_first_use_records(self, tmp_path, monkeypatch):
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV, BootnodeTrustStore

        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        cert, fp = _fake_cert()
        client = BootnodeClient("boot.example.net", 443, tofu=True)
        client._verify_peer(_FakePeer(cert))  # first use: trusted
        assert BootnodeTrustStore(tmp_path / "trust.json").get("boot.example.net", 443) == fp

    def test_tofu_changed_raises(self, tmp_path, monkeypatch):
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV, BootnodeTrustStore

        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        store = BootnodeTrustStore(tmp_path / "trust.json")
        store.set("boot.example.net", 443, "0" * 64)
        cert, _ = _fake_cert()
        client = BootnodeClient("boot.example.net", 443, tofu=True)
        with pytest.raises(BootnodeFingerprintMismatch):
            client._verify_peer(_FakePeer(cert))

    def test_missing_peer_certificate_fails_closed(self):
        client = BootnodeClient("boot.example.net", 443, fingerprints=("a" * 64,))
        peer = type(
            "_NoCert", (), {"_quic": type("_Quic", (), {"tls": type("_Tls", (), {})()})()}
        )()
        with pytest.raises(BootnodeFingerprintMismatch):
            client._verify_peer(peer)

    def test_verify_runs_before_create_stream(self, monkeypatch):
        """A fingerprint mismatch must close before any frame is written."""
        cert, fp = _fake_cert()
        client = BootnodeClient("boot.example.net", 443, fingerprints=("f" * 64,))
        calls: dict[str, bool] = {}

        class _Manager:
            def __init__(self):
                self._quic = type("_Quic", (), {"tls": _FakeTls(cert)})()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def create_stream(self):
                calls["create_stream"] = True
                raise AssertionError("create_stream must not be reached")

        monkeypatch.setattr("hsync_client.boot_client.connect", lambda *_a, **_k: _Manager())
        with pytest.raises(BootnodeFingerprintMismatch):
            client.register(
                build_register_frame(cid=CID, bucket="shared", cert_fp="AA:BB", version="1.0.0")
            )
        assert "create_stream" not in calls


class TestRefusals:
    """A uniform refusal is a valid boot/1 answer, not a contract violation.

    The node replies to a rate-limited (or token-refused) ``register``/
    ``refresh`` with the same ``response`` frame it uses for ``query`` refusals;
    the client must surface it as :class:`BootnodeRefused` so the lease loop can
    keep its state instead of resetting to a full register.
    """

    @staticmethod
    def _refusal_frame(error: str, retry_after: int | None = None) -> dict:
        frame = {
            "protocol_version": "boot/1",
            "type": "response",
            "peers": [],
            "count": 0,
            "ttl": 60,
            "error": error,
        }
        if retry_after is not None:
            frame["retry_after"] = retry_after
        return frame

    def _client_returning(self, monkeypatch, reply: dict) -> BootnodeClient:
        async def fake_exchange(self, payload, *, expect):
            return reply

        # ``BootnodeClient`` is a frozen dataclass: patch the class, not the
        # instance (the network layer is what is being stubbed out).
        monkeypatch.setattr(BootnodeClient, "_exchange_async", fake_exchange)
        return BootnodeClient("127.0.0.1", 1)

    def test_register_rate_limit_is_a_refusal(self, monkeypatch):
        client = self._client_returning(monkeypatch, self._refusal_frame("rate_limited", 42))
        with pytest.raises(BootnodeRefused) as excinfo:
            client.register(
                build_register_frame(cid=CID, bucket="shared", cert_fp="AA:BB", version="1.0.0")
            )
        assert excinfo.value.error == "rate_limited"
        assert excinfo.value.retry_after == 42

    def test_refresh_token_refusal_is_a_refusal(self, monkeypatch):
        client = self._client_returning(monkeypatch, self._refusal_frame("token_required"))
        with pytest.raises(BootnodeRefused) as excinfo:
            client.refresh(build_refresh_frame(cid=CID, bucket="shared"))
        assert excinfo.value.error == "token_required"
        assert excinfo.value.retry_after is None

    def test_response_without_an_error_is_still_a_protocol_error(self, monkeypatch):
        """A ``response`` to ``register`` without ``error`` violates boot/1."""
        malformed = {
            "protocol_version": "boot/1",
            "type": "response",
            "peers": [],
            "count": 0,
            "ttl": 60,
        }
        client = self._client_returning(monkeypatch, malformed)
        with pytest.raises(BootnodeProtocolError):
            client.register(
                build_register_frame(cid=CID, bucket="shared", cert_fp="AA:BB", version="1.0.0")
            )

    def test_another_frame_type_is_still_a_protocol_error(self, monkeypatch):
        client = self._client_returning(monkeypatch, {"type": "leave"})
        with pytest.raises(BootnodeProtocolError):
            client.refresh(build_refresh_frame(cid=CID, bucket="shared"))


class TestStunEcho:
    def test_observed_address(self):
        ack = validate_boot_ack(ACK)
        assert ack is not None
        assert observed_address(ack) == ObservedAddress(host="198.51.100.7", port=54321)

    def test_malformed_ack_is_rejected_by_the_mirror(self):
        bad = {**ACK, "observed_endpoint": {"proto": "udp", "ip": "198.51.100.7"}}
        assert validate_boot_ack(bad) is None
