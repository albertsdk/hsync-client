# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

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
from hsync_common.protocol.tls_trust import TrustPolicy

from hsync_client.boot_client import (
    BootnodeClient,
    BootnodeFingerprintMismatch,
    BootnodeNotTrusted,
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
        # ``insecure`` (policy ``none``) skips the trust check so this test stays
        # focused on the reply contract; trust is covered by its own tests.
        client = BootnodeClient("127.0.0.1", 4433, policy=TrustPolicy.none, timeout=5.0)
        client.leave(build_leave_frame(cid=CID, bucket="shared"))
        assert writer.closed is True
        assert b'"type":"leave"' in writer.written
        assert "read" not in calls


class TestQuicConfiguration:
    def test_alpn_and_no_verification_mode(self):
        # The bootnode is self-signed by contract: the handshake never verifies
        # WebPKI, trust comes from the fingerprint policy.
        config = BootnodeClient(
            "boot.example.net", 443, policy=TrustPolicy.none
        )._quic_configuration()
        assert config.alpn_protocols == [BOOT_ALPN]
        assert config.verify_mode == ssl.CERT_NONE

    def test_default_policy_is_ask(self):
        client = BootnodeClient("boot.example.net", 443)
        assert client.policy is TrustPolicy.ask
        assert client._quic_configuration().verify_mode == ssl.CERT_NONE


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
        client = BootnodeClient(
            "boot.example.net", 443, fingerprints=(fp,), policy=TrustPolicy.strict
        )
        client._verify_peer(_FakePeer(cert))  # no raise

    def test_explicit_pin_mismatch_raises(self, tmp_path, monkeypatch):
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV

        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        cert, _ = _fake_cert()
        client = BootnodeClient(
            "boot.example.net", 443, fingerprints=("f" * 64,), policy=TrustPolicy.strict
        )
        with pytest.raises(BootnodeFingerprintMismatch):
            client._verify_peer(_FakePeer(cert))

    def test_rotation_accepts_second_pin(self):
        cert, fp = _fake_cert()
        client = BootnodeClient(
            "boot.example.net", 443, fingerprints=("0" * 64, fp), policy=TrustPolicy.strict
        )
        client._verify_peer(_FakePeer(cert))  # second pin matches

    def test_tofu_first_use_records(self, tmp_path, monkeypatch):
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV, BootnodeTrustStore

        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        cert, fp = _fake_cert()
        client = BootnodeClient("boot.example.net", 443, policy=TrustPolicy.accept_new)
        client._verify_peer(_FakePeer(cert))  # first use: trusted
        assert BootnodeTrustStore(tmp_path / "trust.json").get("boot.example.net", 443) == fp

    def test_tofu_changed_raises(self, tmp_path, monkeypatch):
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV, BootnodeTrustStore

        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        store = BootnodeTrustStore(tmp_path / "trust.json")
        store.set("boot.example.net", 443, "0" * 64, source="cli")
        cert, _ = _fake_cert()
        client = BootnodeClient("boot.example.net", 443, policy=TrustPolicy.accept_new)
        with pytest.raises(BootnodeFingerprintMismatch):
            client._verify_peer(_FakePeer(cert))

    def test_ask_unknown_prompts_and_pins(self, tmp_path, monkeypatch):
        """Policy ``ask`` prompts on an unknown node and records the source."""
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV, BootnodeTrustStore

        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        cert, fp = _fake_cert()
        prompts: list[tuple[str, int, str]] = []

        def prompt(host, port, observed):
            prompts.append((host, port, observed))
            return True

        client = BootnodeClient("boot.example.net", 443, policy=TrustPolicy.ask, prompt=prompt)
        client._verify_peer(_FakePeer(cert))  # accepted
        assert prompts == [("boot.example.net", 443, fp)]
        entry = BootnodeTrustStore(tmp_path / "trust.json").entries()[0]
        assert entry["fingerprint"] == fp
        assert entry["source"] == "prompt"

    def test_ask_unknown_without_a_terminal_fails_closed(self, tmp_path, monkeypatch):
        """Non-interactive ``ask`` never auto-accepts (SSH ``BatchMode``)."""
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV

        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        cert, _ = _fake_cert()
        client = BootnodeClient("boot.example.net", 443, policy=TrustPolicy.ask)
        with pytest.raises(BootnodeNotTrusted) as excinfo:
            client._verify_peer(_FakePeer(cert))
        assert excinfo.value.non_interactive is True

    def test_ask_declined_prompt_refuses(self, tmp_path, monkeypatch):
        """A declined prompt is fatal and does not pin the fingerprint."""
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV, BootnodeTrustStore

        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        cert, _ = _fake_cert()
        client = BootnodeClient(
            "boot.example.net", 443, policy=TrustPolicy.ask, prompt=lambda *_a: False
        )
        with pytest.raises(BootnodeNotTrusted) as excinfo:
            client._verify_peer(_FakePeer(cert))
        assert excinfo.value.non_interactive is False
        assert BootnodeTrustStore(tmp_path / "trust.json").entries() == []

    def test_ask_matched_stored_proceeds_without_prompting(self, tmp_path, monkeypatch):
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV, BootnodeTrustStore

        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        cert, fp = _fake_cert()
        BootnodeTrustStore(tmp_path / "trust.json").set("boot.example.net", 443, fp, source="cli")

        def _never(*_a):
            raise AssertionError("must not prompt when the pin matches")

        client = BootnodeClient("boot.example.net", 443, policy=TrustPolicy.ask, prompt=_never)
        client._verify_peer(_FakePeer(cert))  # no raise

    def test_missing_peer_certificate_fails_closed(self):
        client = BootnodeClient(
            "boot.example.net", 443, fingerprints=("a" * 64,), policy=TrustPolicy.strict
        )
        peer = type(
            "_NoCert", (), {"_quic": type("_Quic", (), {"tls": type("_Tls", (), {})()})()}
        )()
        with pytest.raises(BootnodeFingerprintMismatch):
            client._verify_peer(peer)

    def test_verify_runs_before_create_stream(self, monkeypatch, tmp_path):
        """A fingerprint mismatch must close before any frame is written."""
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV

        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        cert, fp = _fake_cert()
        client = BootnodeClient(
            "boot.example.net", 443, fingerprints=("f" * 64,), policy=TrustPolicy.strict
        )
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


class TestTrustFailureGuidance:
    """A refused fingerprint tells the operator how to unblock (SSH-style).

    ``str`` is the concise case-specific reason used by the fatal propagation
    line; ``guidance`` is the recovery block the lease loop logs once, with the
    presented fingerprint, the known-hosts file and the exact recovery commands.
    """

    def test_changed_pin_names_store_and_recovery_commands(self, tmp_path, monkeypatch):
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV, BootnodeTrustStore

        trust_file = tmp_path / "trust.json"
        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(trust_file))
        BootnodeTrustStore(trust_file).set("boot.example.net", 443, "f" * 64, source="cli")
        cert, fp = _fake_cert()
        client = BootnodeClient("boot.example.net", 443, policy=TrustPolicy.accept_new)
        with pytest.raises(BootnodeFingerprintMismatch) as excinfo:
            client._verify_peer(_FakePeer(cert))
        message = str(excinfo.value)
        guidance = excinfo.value.guidance
        assert "does not match the pinned fingerprint (possible man-in-the-middle)" in message
        assert guidance is not None
        assert guidance.startswith(
            "Bootnode 'boot.example.net:443' trust failed: the certificate fingerprint "
            "does not match the accepted pin (possible man-in-the-middle)."
        )
        assert f"sha256:{fp}" in guidance
        assert str(trust_file) in guidance
        assert f"hsync trust bootnode add boot.example.net sha256:{fp} --port 443" in guidance
        assert "hsync trust bootnode remove boot.example.net --port 443" in guidance
        assert "ssh-keygen -R" in guidance

    def test_strict_unknown_node_offers_pinning_and_tofu(self, tmp_path, monkeypatch):
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV

        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        cert, fp = _fake_cert()
        client = BootnodeClient("boot.example.net", 443, policy=TrustPolicy.strict)
        with pytest.raises(BootnodeFingerprintMismatch) as excinfo:
            client._verify_peer(_FakePeer(cert))
        assert "is not pinned and the trust policy is 'strict'" in str(excinfo.value)
        guidance = excinfo.value.guidance or ""
        assert f"hsync trust bootnode add boot.example.net sha256:{fp} --port 443" in guidance
        assert "--bootnode-tofu" in guidance

    def test_explicit_pin_mismatch_points_at_the_flag(self, tmp_path, monkeypatch):
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV

        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        cert, _ = _fake_cert()
        client = BootnodeClient(
            "boot.example.net", 443, fingerprints=("0" * 64,), policy=TrustPolicy.strict
        )
        with pytest.raises(BootnodeFingerprintMismatch) as excinfo:
            client._verify_peer(_FakePeer(cert))
        assert "--bootnode-fingerprint" in str(excinfo.value)
        guidance = excinfo.value.guidance or ""
        assert "hsync trust bootnode fingerprint boot.example.net --port 443" in guidance
        assert "--bootnode-fingerprint" in guidance

    def test_prompt_accept_reports_where_the_pin_was_saved(self, tmp_path, monkeypatch):
        from loguru import logger as log

        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV, BootnodeTrustStore

        trust_file = tmp_path / "trust.json"
        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(trust_file))
        cert, fp = _fake_cert()
        messages: list[str] = []
        sink_id = log.add(messages.append, level="INFO", format="{message}")
        try:
            client = BootnodeClient(
                "boot.example.net", 443, policy=TrustPolicy.ask, prompt=lambda *_a: True
            )
            client._verify_peer(_FakePeer(cert))
        finally:
            log.remove(sink_id)
        assert BootnodeTrustStore(trust_file).get("boot.example.net", 443) == fp
        assert any(
            "Permanently added 'boot.example.net:443' (SHA256) to the list of known "
            f"hosts ({trust_file})." in message
            for message in messages
        )

    def test_tofu_reports_where_the_pin_was_saved(self, tmp_path, monkeypatch):
        from loguru import logger as log

        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV

        trust_file = tmp_path / "trust.json"
        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(trust_file))
        cert, _ = _fake_cert()
        messages: list[str] = []
        sink_id = log.add(messages.append, level="INFO", format="{message}")
        try:
            client = BootnodeClient("boot.example.net", 443, policy=TrustPolicy.accept_new)
            client._verify_peer(_FakePeer(cert))
        finally:
            log.remove(sink_id)
        assert any(
            "Permanently added 'boot.example.net:443' (SHA256) to the list of known "
            f"hosts ({trust_file})." in message
            for message in messages
        )

    def test_declined_prompt_says_nothing_was_saved(self, tmp_path, monkeypatch):
        from hsync_client.bootnode_trust import BOOTNODE_TRUST_FILE_ENV

        monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        cert, _ = _fake_cert()
        client = BootnodeClient(
            "boot.example.net", 443, policy=TrustPolicy.ask, prompt=lambda *_a: False
        )
        with pytest.raises(BootnodeNotTrusted) as excinfo:
            client._verify_peer(_FakePeer(cert))
        message = str(excinfo.value)
        assert "nothing was saved" in message
        assert "--bootnode-tofu" in message
        assert "--bootnode-fingerprint" in message


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
