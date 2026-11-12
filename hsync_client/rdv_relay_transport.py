# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Relay transport primitives (substream framing, QUIC/TCP sessions, probes).

The framing and session layer the relay dialer/acceptor build on; the dialer and
acceptor policy live in ``hsync_client.rdv_relay``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from collections.abc import AsyncIterator
from typing import Any

from aioquic.asyncio import connect
from hsync_common.protocol.rendezvous import (
    validate_rdv_relay_error,
    validate_rdv_relay_ok,
)
from loguru import logger as log

from hsync_client.constants import (
    RELAY_BUFFER_BYTES,
)
from hsync_client.rdv_client import (
    RendezvousClient,
    frame_line,
)

# One relay tunnel carries many loopback connections over its single paired
# session, framed by a small header the two peers' relay endpoints understand and
# the node never inspects (it stays an opaque byte pump):
#   kind (1) | stream_id (4, BE) | payload length (4, BE) | payload
# A relay_connect per loopback connection exhausted the node's per-IP budget,
# which is why the tunnel multiplexes instead.
_RELAY_SUBSTREAM_HEADER = struct.Struct("!BII")
_RELAY_SUBSTREAM_DATA = 1
_RELAY_SUBSTREAM_CLOSE = 2
# An idle data session would be reaped by the node's per-session pump idle
# timeout; this keepalive keeps it active even when the sync is idle.
_RELAY_SUBSTREAM_PING = 3


def _encode_substream(kind: int, stream_id: int, payload: bytes = b"") -> bytes:
    """Encode one multiplexed frame (see the header layout above)."""
    return _RELAY_SUBSTREAM_HEADER.pack(kind, stream_id, len(payload)) + payload


async def _read_substream(reader: Any) -> tuple[int, int, bytes] | None:
    """Read one multiplexed frame, or ``None`` on EOF/error/oversize."""
    try:
        header = await reader.readexactly(_RELAY_SUBSTREAM_HEADER.size)
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        return None
    kind, stream_id, length = _RELAY_SUBSTREAM_HEADER.unpack(header)
    if length > RELAY_BUFFER_BYTES:
        return None
    payload = b""
    if length:
        try:
            payload = await reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return None
    return kind, stream_id, payload


def refusal_code(raw: bytes) -> str | None:
    """Return the uniform refusal code carried by a ``relay_error`` line."""
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "relay_unavailable"
    refusal = validate_rdv_relay_error(decoded)
    return None if refusal is None else str(refusal["error"])


def refusal_retry_after(raw: bytes) -> int | None:
    """Return the ``retry_after`` a ``relay_error`` line carries, if any.

    The node answers ``rate_limited`` with the wait it wants: honouring it is what
    keeps a client from deepening its own refusal, and it is the only signal that
    the ``relay_connect`` budget is spent.
    """
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    refusal = validate_rdv_relay_error(decoded)
    if refusal is None:
        return None
    retry_after = refusal.get("retry_after")
    return retry_after if isinstance(retry_after, int) else None


async def _probe_relay_connect(
    session: _RelaySession, frame: dict[str, object], timeout: float
) -> tuple:
    """Pair the tunnel's relay session and return ``(reader, writer, refusal, retry_after)``.

    Success keeps the stream/connection open and returns it with ``refusal=None``:
    it is the tunnel's data plane for its whole life, not a throwaway check, so no
    ``relay_connect`` is spent beyond one per tunnel. On refusal, protocol error
    or transport error the stream is closed and ``(None, None, code, retry_after)``
    is returned, so the caller can tell a node without the relay from a torn
    connection and can honour the wait a rate limit asked for.
    """
    try:
        stream_reader, stream_writer = await _session_open_stream(session)
    except (ConnectionError, OSError) as exc:
        return None, None, f"stream_failed: {exc}", None
    try:
        stream_writer.write(frame_line(frame))
        await stream_writer.drain()
        reply = await asyncio.wait_for(stream_reader.readline(), timeout)
    except (TimeoutError, ConnectionError, OSError):
        _close_writer(stream_writer)
        return None, None, "relay_unavailable", None
    raw = bytes(reply).rstrip(b"\n")
    refusal = refusal_code(raw)
    if refusal is not None or (raw and validate_rdv_relay_ok(json.loads(raw)) is None):
        _close_writer(stream_writer)
        return None, None, refusal or "relay_protocol", refusal_retry_after(raw)
    return stream_reader, stream_writer, None, None


async def _session_open_stream(session: Any) -> tuple[Any, Any]:
    """Open one relay stream/connection on either transport seam.

    A :class:`_RelaySession` exposes ``open_stream``; the historical seam (a bare
    aioquic connection, or a test fake carrying ``create_stream``) is accepted
    too, so tests that stub ``_quic_session`` keep working unchanged.
    """
    open_stream = getattr(session, "open_stream", None)
    if open_stream is not None:
        return await open_stream()
    return await session.create_stream()


class _RelaySession:
    """One long-lived relay transport to the node.

    Wraps either a QUIC connection (one connection, many streams) or a TLS/TCP
    connection (one connection per relay stream: the control/data connection for
    the first :meth:`open_stream`, a fresh connection per ``relay_attach``
    after). ``transport`` tells a caller which flavour it holds.
    """

    transport: str

    async def open_stream(self) -> tuple[Any, Any]:
        """One relay stream (QUIC) or one TLS/TCP connection to the node."""
        raise NotImplementedError

    async def close(self) -> None:
        """Release every connection this session holds (best-effort)."""
        raise NotImplementedError


class _QuicRelaySession(_RelaySession):
    """QUIC flavour: the aioquic connection, one stream per call."""

    transport = "quic"

    def __init__(self, quic: Any) -> None:
        self._quic = quic

    async def open_stream(self) -> tuple[Any, Any]:
        return await self._quic.create_stream()

    async def close(self) -> None:
        # The connection is owned by the ``connect()`` context manager that
        # created this session; nothing to do here (state below is stream-side).
        return None


class _TcpRelaySession(_RelaySession):
    """TLS/TCP flavour: one connection per relay stream, pinned to one IP.

    The node host is resolved **once** (``pinned_ip``): with several A/AAAA
    records an attach dialing the hostname could reach another node instance
    that does not hold the pending ``relay_id``, or leave from another address
    family and fail the node's ``peer_ip`` check. Every connection therefore
    dials the same IP the control/data connection used.
    """

    transport = "tcp"

    def __init__(self, client: RendezvousClient, *, ip: str) -> None:
        self._client = client
        self._ip = ip
        self._writers: list[Any] = []

    async def open_stream(self) -> tuple[Any, Any]:
        """Dial one TLS/TCP connection to the pinned IP.

        The peer certificate is verified **before the first write** (exactly as
        the discovery path verifies the winner): the token/tunnel frames only
        travel after the fingerprint matched, so a changed identity is refused
        with zero application bytes sent.
        """
        last_exc: BaseException | None = None
        for port in self._client.candidate_ports():
            try:
                reader, writer, cert_der = await self._client.dial_relay_tcp(port, ip=self._ip)
            except (OSError, TimeoutError) as exc:
                last_exc = exc
                continue
            try:
                self._client.verify_peer_der(cert_der)
            except BaseException:
                # A refused identity is fatal, but the connection must not leak:
                # it is not tracked yet, so close it here before propagating.
                _close_writer(writer)
                raise
            self._writers.append(writer)
            return reader, writer
        raise ConnectionError(
            f"relay cannot reach {self._client.host} on any candidate port"
        ) from last_exc

    async def close(self) -> None:
        for writer in self._writers:
            _close_writer(writer)
        self._writers.clear()


def _close_writer(writer: Any) -> None:
    """Close a stream writer, tolerating an already-closed stream."""
    try:
        if not writer.is_closing():
            writer.close()
    except Exception:  # noqa: BLE001 - teardown must never raise
        pass


@contextlib.asynccontextmanager
async def _quic_session(client: RendezvousClient) -> AsyncIterator[Any]:
    """Dial the node for a long-lived QUIC relay session, first reachable port first.

    Mirrors ``RendezvousClient._with_connection``: only a failed **connection**
    falls back to the next candidate port (a client whose egress firewall blocks
    UDP 443 but allows 53 still relays), and once the handshake succeeded any
    later transport error is the session's own failure, not a reason to redial.
    """
    configuration = client.relay_configuration()
    last_exc: BaseException | None = None
    for port in client.candidate_ports():
        connected = False
        try:
            async with connect(
                client.host,
                port,
                configuration=configuration,
                wait_connected=True,
            ) as quic:
                connected = True
                client.verify_peer(quic)
                yield _QuicRelaySession(quic)
                return
        except (OSError, TimeoutError) as exc:
            if connected:
                raise
            last_exc = exc
    log.debug(f"Relay cannot reach the discovery servers: {last_exc}")


def _relay_transport(client: RendezvousClient) -> str:
    """The transport the relay sessions use, from the client's selector.

    ``quic``/``tcp`` pick that transport; ``auto`` reuses the client's memoized
    winner (the discovery path already raced and remembered it), defaulting to
    QUIC when nothing has been memoized yet — the preferred transport, and the
    one the next discovery exchange would try. Established tunnels keep their
    transport: this is consulted once per ``_hold_session``/``_run``, so a memo
    change only affects the next re-dial.
    """
    selector = getattr(client, "transport", "quic") or "quic"
    if selector in ("quic", "tcp"):
        return selector
    memoized = getattr(client, "_memoized", lambda: None)()
    return "tcp" if memoized is not None and memoized[0] == "tcp" else "quic"


@contextlib.asynccontextmanager
async def _relay_session(client: RendezvousClient) -> AsyncIterator[_RelaySession]:
    """Dial a long-lived relay session over the client's selected transport.

    QUIC flavour: one connection, many streams (control, data, attach) —
    the historical path. TLS/TCP flavour: one connection per relay stream,
    pinned to a single resolved IP (see :class:`_TcpRelaySession`).
    """
    if _relay_transport(client) == "tcp":
        session = _TcpRelaySession(client, ip=client.resolved_relay_ip())
        try:
            yield session
            return
        finally:
            await session.close()
    async with _quic_session(client) as session:
        yield session
