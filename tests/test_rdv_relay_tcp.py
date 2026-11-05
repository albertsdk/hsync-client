"""Relay-over-TCP tests: the transport seam and the transport choice.

``rdv_relay`` rides the client's transport selector: ``quic`` (the historical
path), ``tcp`` (one connection per relay stream, pinned to one resolved IP) and
``auto`` (reuses the client's memoized winner). These tests exercise the seam
that maps TLS/TCP connections to relay streams and the choice function without
dialing sockets.
"""

from __future__ import annotations

import asyncio

from hsync_client.rdv_relay import _relay_transport, _TcpRelaySession

CID = "A1b2C3d4E5f6G7h8I9j0K"


class _Client:
    """A minimal relay client shape (only the transport selector matters here)."""

    def __init__(self, transport: str = "auto") -> None:
        self.transport = transport
        self.memo: tuple[str, int, float] | None = None
        self.host = "rdv.example.net"
        self.port = 443

    def _memoized(self) -> tuple[str, int] | None:
        if self.memo is None:
            return None
        transport, _port, _expires = self.memo
        return (transport, 443)

    def _remember(self, transport: str) -> None:
        self.memo = (transport, 443, float("inf") if transport == "quic" else 1.0)


class TestRelayTransportChoice:
    def test_explicit_quic(self) -> None:
        assert _relay_transport(_Client("quic")) == "quic"

    def test_explicit_tcp(self) -> None:
        assert _relay_transport(_Client("tcp")) == "tcp"

    def test_auto_defaults_to_quic_without_memo(self) -> None:
        assert _relay_transport(_Client("auto")) == "quic"

    def test_auto_reuses_a_tcp_memo(self) -> None:
        client = _Client("auto")
        client._remember("tcp")
        assert _relay_transport(client) == "tcp"

    def test_auto_reuses_a_quic_memo(self) -> None:
        client = _Client("auto")
        client._remember("quic")
        assert _relay_transport(client) == "quic"

    def test_legacy_client_without_transport_defaults_to_quic(self) -> None:
        # Tests (and older fakes) may expose no ``transport``: QUIC is the
        # historical default and must stay the fallback.
        class _Legacy:  # noqa: N801
            pass

        assert _relay_transport(_Legacy()) == "quic"


class TestTcpRelaySession:
    def test_open_stream_verifies_peer_before_first_write(self, monkeypatch) -> None:
        """The TCP seam runs ``verify_peer_der`` before returning (pre-write)."""
        calls: list[str] = []

        class _ClientSeam:  # noqa: N801
            transport = "tcp"
            host = "rdv.example.net"
            port = 443

            def candidate_ports(self):
                return (443,)

            async def dial_relay_tcp(self, port, *, ip):
                calls.append(f"dial:{port}:{ip}")
                return object(), object(), b"cert-der"

            def verify_peer_der(self, der):
                calls.append(f"verify:{der}")

        session = _TcpRelaySession(_ClientSeam(), ip="203.0.113.7")
        reader, writer = asyncio.run(session.open_stream())
        assert reader is not None and writer is not None
        assert calls == ["dial:443:203.0.113.7", "verify:b'cert-der'"]

    def test_open_stream_pins_every_connection_to_the_resolved_ip(self, monkeypatch) -> None:
        """Every attach dials the same IP (a multi-A host cannot reach another node)."""
        dialed: list[tuple[int, str]] = []

        class _ClientSeam:  # noqa: N801
            transport = "tcp"
            host = "rdv.example.net"
            port = 443

            def candidate_ports(self):
                return (443,)

            async def dial_relay_tcp(self, port, *, ip):
                dialed.append((port, ip))
                return object(), object(), None

            def verify_peer_der(self, _der):
                return None

        session = _TcpRelaySession(_ClientSeam(), ip="198.51.100.9")
        for _ in range(3):  # control + two attaches
            asyncio.run(session.open_stream())
        assert dialed == [(443, "198.51.100.9")] * 3

    def test_close_clears_every_connection(self, monkeypatch) -> None:
        closed: list[bool] = []

        class _Writer:
            def is_closing(self):
                return False

            def close(self):
                closed.append(True)

        class _ClientSeam:  # noqa: N801
            transport = "tcp"
            host = "rdv.example.net"
            port = 443

            def candidate_ports(self):
                return (443,)

            async def dial_relay_tcp(self, port, *, ip):
                return object(), _Writer(), None

            def verify_peer_der(self, _der):
                return None

        session = _TcpRelaySession(_ClientSeam(), ip="203.0.113.7")
        asyncio.run(session.open_stream())
        asyncio.run(session.close())
        assert closed == [True]

    def test_open_stream_fallbacks_across_candidate_ports(self, monkeypatch) -> None:
        """A blocked candidate port falls back to the next one (as on QUIC)."""
        attempts: list[int] = []

        class _ClientSeam:  # noqa: N801
            transport = "tcp"
            host = "rdv.example.net"
            port = 443

            def candidate_ports(self):
                return (443, 53)

            async def dial_relay_tcp(self, port, *, ip):
                attempts.append(port)
                if port == 443:
                    raise OSError("blocked by egress firewall")
                return object(), object(), None

            def verify_peer_der(self, _der):
                return None

        session = _TcpRelaySession(_ClientSeam(), ip="198.51.100.9")
        asyncio.run(session.open_stream())
        assert attempts == [443, 53]
