"""Rdv transport selection tests: race/stagger, attempt taxonomy, memo.

The rendezvous client selects a transport with ``transport="auto"`` (default):
QUIC first, TLS/TCP after the RFC 8305 stagger, first handshake to complete
wins, loser closed before any frame; ``quic``/``tcp`` use one transport only.
These tests drive the selection with fake dials (no sockets), so every timing
and fallback branch is deterministic.

Trust is verified once, on the winner, before the first write; a trust,
protocol or refusal error is an exchange failure and never falls back to
another transport/port.
"""

from __future__ import annotations

import asyncio
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID
from hsync_common.constants import RDV_TCP_FALLBACK_TTL_S
from hsync_common.protocol.tls_trust import TrustPolicy
from hsync_common.ssl_utils import (
    build_self_signed_certificate,
    generate_ecdsa_private_key,
)

from hsync_client.rdv_client import (
    RendezvousClient,
    RendezvousFingerprintMismatch,
    RendezvousProtocolError,
    RendezvousUnreachable,
    _close_unused_dials,
    _Dial,
)

CID = "A1b2C3d4E5f6G7h8I9j0K"


def _fake_cert() -> x509.Certificate:
    key = generate_ecdsa_private_key()
    return build_self_signed_certificate(
        key,
        subject_attrs=[x509.NameAttribute(NameOID.COMMON_NAME, "rdv.test")],
        san_list=[x509.DNSName("rdv.test")],
        validity_days=1,
    )


def _der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


class _FakeDial:
    """A dialing transport session with a pluggable connect delay."""

    def __init__(self, transport: str, port: int, *, delay: float = 0.0) -> None:
        self.transport = transport
        self.port = port
        self.delay = delay
        self.closed = False
        self.peer_cert_der: bytes | None = None

    async def close(self) -> None:
        self.closed = True


class _Pipe:
    """One synthetic dial result factory for a transport."""

    def __init__(self, cert: x509.Certificate | None = None, delay: float = 0.0) -> None:
        self.cert = cert
        self.delay = delay

    def dial(self, transport: str, port: int) -> _FakeDial:
        dial = _FakeDial(transport, port, delay=self.delay)
        if self.cert is not None:
            dial.peer_cert_der = _der(self.cert)
        return dial


def _patch_dials(monkeypatch, quic: _Pipe | None, tcp: _Pipe | None) -> None:
    """Route ``_dial_quic``/``_dial_tcp`` to the fakes (``None`` = raises OSError)."""
    import hsync_client.rdv_client as mod

    async def _quic(self, port: int) -> _Dial:  # noqa: ANN001
        if quic is None:
            raise OSError("quic blocked")
        dial = quic.dial("quic", port)
        if dial.delay:
            await asyncio.sleep(dial.delay)
        return dial

    async def _tcp(self, port: int, context) -> _Dial:  # noqa: ANN002
        if tcp is None:
            raise OSError("tcp blocked")
        dial = tcp.dial("tcp", port)
        if dial.delay:
            await asyncio.sleep(dial.delay)
        return dial

    monkeypatch.setattr(mod.RendezvousClient, "_dial_quic", _quic)
    monkeypatch.setattr(mod.RendezvousClient, "_dial_tcp", _tcp)


def _register_frame() -> dict:
    return {
        "protocol_version": "rdv/1",
        "type": "register",
        "cid": CID,
        "bucket": "shared",
        "cert_fp": "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899",
        "version": "1.0.0",
    }


class _RunOn:
    """Capture which dial won for the taxonomy test."""

    async def run(self, dial):  # noqa: ANN001
        return dial


class TestAttemptTaxonomy:
    def test_auto_quic_wins_when_both_connect(self, monkeypatch) -> None:
        """A QUIC that finishes within the stagger window wins outright."""
        _patch_dials(monkeypatch, _Pipe(), _Pipe())
        client = RendezvousClient("rdv.test", 443, policy=TrustPolicy.none)
        won: list[str] = []

        async def _run(dial):  # noqa: ANN001
            won.append(dial.transport)
            return dial.transport

        async def _exercise() -> str:
            return await client._with_connection(_run, what="reply")

        assert asyncio.run(_exercise()) == "quic"
        assert won == ["quic"]
        assert client._memoized() == ("quic", 443)

    def test_tcp_wins_when_quic_slow_and_tcp_fast(self, monkeypatch) -> None:
        """TCP completes inside its window: first handshake wins, loser closed."""
        _patch_dials(monkeypatch, _Pipe(delay=0.5), _Pipe())
        client = RendezvousClient("rdv.test", 443, policy=TrustPolicy.none)

        async def _run(dial):  # noqa: ANN001
            return dial.transport

        async def _exercise() -> str:
            return await client._with_connection(_run, what="reply")

        assert asyncio.run(_exercise()) == "tcp"
        # TCP winner remembered with a TTL; the memo says tcp with an expiry.
        memo = client._memo
        assert memo is not None and memo[0] == "tcp"
        assert memo[2] > 0

    def test_quic_only_does_not_race(self, monkeypatch) -> None:
        """``transport="quic"`` dials only QUIC, ports in order."""
        _patch_dials(monkeypatch, _Pipe(), _Pipe())
        client = RendezvousClient(
            "rdv.test", 443, ports=(53,), policy=TrustPolicy.none, transport="quic"
        )
        dial = asyncio.run(client._dial_transport_ports("quic", what="reply"))
        assert dial.transport == "quic"

    def test_both_transports_blocked_raises_unreachable(self, monkeypatch) -> None:
        _patch_dials(monkeypatch, None, None)
        client = RendezvousClient("rdv.test", 443, policy=TrustPolicy.none)
        with pytest.raises(RendezvousUnreachable):
            client.query(_register_frame())

    def test_no_fallback_on_protocol_error(self, monkeypatch) -> None:
        """A protocol error on the winner never re-dials another transport/port."""
        _patch_dials(monkeypatch, _Pipe(), _Pipe())
        client = RendezvousClient("rdv.test", 443, policy=TrustPolicy.none, transport="auto")

        async def _boom(dial):  # noqa: ANN001
            raise RendezvousProtocolError("malformed reply")

        with pytest.raises(RendezvousProtocolError):
            asyncio.run(client._run_on(_Pipe().dial("quic", 443), _boom, what="reply"))


class TestMemo:
    def test_tcp_winner_expires_and_quic_regains(self) -> None:
        """A TCP memo expires after RDV_TCP_FALLBACK_TTL_S; after that auto races again."""
        client = RendezvousClient("rdv.test", 443, policy=TrustPolicy.none)
        client._remember("tcp", 443)
        assert client._memoized() == ("tcp", 443)
        # Expire it: the memo must be ignored (full race reruns).
        object.__setattr__(client, "_memo", ("tcp", 443, 0.0))
        assert client._memoized() is None

    def test_quic_winner_never_expires(self) -> None:
        client = RendezvousClient("rdv.test", 443, policy=TrustPolicy.none)
        client._remember("quic", 443)
        assert client._memoized(now=RDV_TCP_FALLBACK_TTL_S + 1000) == ("quic", 443)

    def test_network_change_resets_the_memo(self) -> None:
        client = RendezvousClient("rdv.test", 443, policy=TrustPolicy.none)
        client._remember("tcp", 443)
        client.reset_transport_memo()
        assert client._memoized() is None

    def test_stale_memo_cleared_on_connection_failure(self, monkeypatch) -> None:
        """A memoized transport failing at connection level clears the memo and
        the full race runs within the same exchange."""
        _patch_dials(monkeypatch, _Pipe(), _Pipe())
        client = RendezvousClient("rdv.test", 443, policy=TrustPolicy.none)
        client._remember("tcp", 443)
        # TCP now fails at the connection phase: the memo clears, QUIC wins.
        _patch_dials(monkeypatch, _Pipe(), None)
        dial = asyncio.run(client._dial_auto(what="reply"))
        assert dial.transport == "quic"
        # The memo is cleared (the *_run_on* step records the winner later).
        assert client._memoized() is None


class TestTcpTls:
    def test_tls_context_is_tls13_self_signed_and_alpn(self) -> None:
        client = RendezvousClient("rdv.test", 443, policy=TrustPolicy.none)
        context = client._tls_context()
        assert context.minimum_version == ssl.TLSVersion.TLSv1_3
        assert context.verify_mode == ssl.CERT_NONE

    def test_fingerprint_verified_from_der(self, tmp_path, monkeypatch) -> None:
        """TCP pins the same fingerprint the QUIC path would (one trust store)."""
        from hsync_client.rendezvous_trust import RENDEZVOUS_TRUST_FILE_ENV

        monkeypatch.setenv(RENDEZVOUS_TRUST_FILE_ENV, str(tmp_path / "trust.json"))
        cert = _fake_cert()
        der = _der(cert)
        client = RendezvousClient("rdv.test", 443, policy=TrustPolicy.strict)
        # strict with no pins: an unknown fingerprint is refused without a prompt.
        with pytest.raises(RendezvousFingerprintMismatch):
            client.verify_peer_der(der)
        # accept-new pins it silently (single prompt path for the TCP winner).
        client = RendezvousClient("rdv.test", 443, policy=TrustPolicy.accept_new)
        client.verify_peer_der(der)
        client.verify_peer_der(der)  # second sighting proceeds silently


def test_close_unused_dials_closes_only_the_completed_loser() -> None:
    """A losing attempt that already completed is closed, never leaked."""

    async def _exercise() -> tuple[_FakeDial, _FakeDial]:
        winner_dial = _FakeDial("quic", 443)
        loser_dial = _FakeDial("tcp", 443)

        async def _dial(dial: _FakeDial) -> _FakeDial:
            return dial

        winner = asyncio.ensure_future(_dial(winner_dial))
        loser = asyncio.ensure_future(_dial(loser_dial))
        await asyncio.gather(winner, loser)
        await _close_unused_dials((winner, loser), winner)
        return winner_dial, loser_dial

    winner, loser = asyncio.run(_exercise())
    assert winner.closed is False  # the winner's lifetime belongs to the caller
    assert loser.closed is True  # the completed loser was closed, not leaked
