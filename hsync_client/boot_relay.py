# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Data relay over ``boot/1``: reach a peer that no inbound NAT mapping reaches.

Two peers that are both behind carrier-grade NAT cannot dial each other: neither
accepts an inbound TCP connection. The bootnode is the one element of the group
that is publicly reachable, so it can pair them and forward their TLS bytes
opaque (``boot/1`` relay frames). This module is the client half:

* :class:`RelayDialer` — the requester side. It opens a relay session and then
  exposes it as a **loopback TCP listener**: the regular HTTPS/WSS client dials
  ``127.0.0.1`` and never learns that the bytes travel through the node. The
  master's certificate pin still authenticates the peer, so the relay is a
  transport detail, never an identity shortcut.
* :class:`RelayAcceptor` — the serving side. It holds the listener control
  stream and, for each announced session, bridges the relayed stream to the
  local ``listen_port``, where the master's own HTTPS/WSS server terminates TLS
  exactly as it does for a direct connection.

Both run their own asyncio loop in a background thread (the sync engine is
threaded) and reuse the bootnode client's trust machinery
(``BootnodeClient.relay_configuration``/``verify_peer``), so pins, TOFU and the
SSH-style prompt behave exactly as they do for discovery.

Consumers: ``hsync-client`` (``p2p_handler`` through the discovery plan), tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

from aioquic.asyncio import connect
from hsync_common.protocol.bootnode import (
    validate_boot_relay_error,
    validate_boot_relay_incoming,
    validate_boot_relay_listening,
    validate_boot_relay_ok,
)
from loguru import logger as log

from hsync_client.boot_client import (
    BootnodeClient,
    BootnodeProtocolError,
    build_relay_attach_frame,
    build_relay_connect_frame,
    build_relay_listen_frame,
    build_relay_ping_frame,
    frame_line,
)
from hsync_client.constants import (
    RELAY_BUFFER_BYTES,
    RELAY_IDLE_TIMEOUT_S,
    RELAY_KEEPALIVE_INTERVAL,
)

__all__ = [
    "FailoverRelayDialer",
    "GroupRelayAcceptor",
    "RelayAcceptor",
    "RelayDialer",
    "RelayOpenError",
]


class RelayOpenError(RuntimeError):
    """The node refused, or failed, to pair a relay session."""


def refusal_code(raw: bytes) -> str | None:
    """Return the uniform refusal code carried by a ``relay_error`` line."""
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "relay_unavailable"
    refusal = validate_boot_relay_error(decoded)
    return None if refusal is None else str(refusal["error"])


async def _probe_relay_connect(quic: Any, frame: dict[str, object], timeout: float) -> tuple:
    """Open one relay session and return ``(reader, writer, refusal)``.

    Success keeps the fresh stream open and returns it with ``refusal=None``;
    the caller owns both ends and must close them. On refusal, protocol error or
    transport error the stream is closed and ``(None, None, code)`` is returned,
    so the caller can tell a node without the relay from a torn connection.
    """
    try:
        stream_reader, stream_writer = await quic.create_stream()
    except (ConnectionError, OSError) as exc:
        return None, None, f"stream_failed: {exc}"
    try:
        stream_writer.write(frame_line(frame))
        await stream_writer.drain()
        reply = await asyncio.wait_for(stream_reader.readline(), timeout)
    except (TimeoutError, ConnectionError, OSError):
        stream_writer.close()
        return None, None, "relay_unavailable"
    raw = bytes(reply).rstrip(b"\n")
    refusal = refusal_code(raw)
    if refusal is not None or (raw and validate_boot_relay_ok(json.loads(raw)) is None):
        stream_writer.close()
        return None, None, refusal or "relay_protocol"
    return stream_reader, stream_writer, None


async def _pump(reader: Any, writer: Any, idle_timeout: float, done: asyncio.Event) -> None:
    """Copy one direction until EOF, an idle gap or a transport error."""
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(RELAY_BUFFER_BYTES), idle_timeout)
            if not chunk:
                return
            writer.write(chunk)
            await writer.drain()
    except (TimeoutError, ConnectionError, OSError, asyncio.CancelledError):
        return
    finally:
        done.set()


@contextlib.asynccontextmanager
async def _quic_session(client: BootnodeClient) -> AsyncIterator[Any]:
    """Dial the node for a long-lived relay session, first reachable port first.

    Mirrors ``BootnodeClient._with_connection``: only a failed **connection**
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
                yield quic
                return
        except (OSError, TimeoutError) as exc:
            if connected:
                raise
            last_exc = exc
    log.debug(f"Relay cannot reach the discovery server: {last_exc}")


async def bridge(
    left_reader: Any,
    left_writer: Any,
    right_reader: Any,
    right_writer: Any,
    idle_timeout: float = RELAY_IDLE_TIMEOUT_S,
) -> None:
    """Pump bytes both ways until one side ends, then close both."""
    done = asyncio.Event()
    tasks = [
        asyncio.create_task(_pump(left_reader, right_writer, idle_timeout, done)),
        asyncio.create_task(_pump(right_reader, left_writer, idle_timeout, done)),
    ]
    try:
        await done.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for writer in (left_writer, right_writer):
            try:
                if not writer.is_closing():
                    writer.close()
            except Exception:  # noqa: BLE001 - teardown must never raise
                continue


class RelayDialer:
    """Requester side: one relay tunnel exposed as a loopback TCP endpoint.

    A tunnel is long-lived (until :meth:`close`): every TCP connection accepted
    on loopback opens its **own** ``relay_connect`` session with the node, so the
    data plane (REST + WSS, several connections per sync cycle) rides the same
    tunnel. The old one-session-per-tunnel design closed the loopback listener
    as soon as the first bridged connection ended, so any later connection (the
    WebSocket channel, a file download) hit ``Connection refused`` and a
    cross-network sync never completed.
    """

    def __init__(
        self,
        client: BootnodeClient,
        *,
        cid: str,
        bucket: str,
        target_cid: str,
        group: str | None = None,
        bucket_token: str | None = None,
    ) -> None:
        self._client = client
        self._cid = cid
        self._bucket = bucket
        self._target_cid = target_cid
        self._group = group
        self._token = bucket_token
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._endpoint: tuple[str, int] | None = None

    def open(self, timeout: float = 15.0) -> tuple[str, int] | None:
        """Pair the session and return the loopback ``(host, port)`` to dial.

        ``None`` means the node could not pair it (relay disabled, no listener
        for the target, capacity reached): the caller then keeps its normal
        "no reachable endpoint, retry discovery" path.
        """
        self._thread = threading.Thread(
            target=self._thread_main, name="hsync-relay-dialer", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            log.debug("Relay session open timed out")
            self.close()
            return None
        return self._endpoint

    def close(self) -> None:
        """Tear the tunnel down (idempotent)."""
        loop, stop, thread = self._loop, self._stop, self._thread
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._stop = asyncio.Event()
        try:
            loop.run_until_complete(self._run())
        except BaseException as exc:  # noqa: BLE001 - reported through the log
            log.debug(f"Relay dialer stopped: {exc}")
        finally:
            self._ready.set()
            loop.close()

    async def _run(self) -> None:
        async with _quic_session(self._client) as quic:
            # Pre-flight: prove the node pairs a session for this target before
            # exposing a loopback endpoint, so ``open()`` keeps returning ``None``
            # for a node without the relay (the caller backs off instead of
            # re-dialling on every discovery tick). The probe stream is closed
            # right after the reply; real sessions are paired per connection.
            probe_reader, probe_writer, refusal = await _probe_relay_connect(
                quic, self._connect_frame(), self._client.timeout
            )
            if probe_reader is None:
                if refusal is not None:
                    log.debug(f"Relay session refused by the node: {refusal}")
                return
            probe_writer.close()
            server = await asyncio.start_server(self._handle(quic), "127.0.0.1", 0)
            try:
                socket = server.sockets[0]
                self._endpoint = ("127.0.0.1", int(socket.getsockname()[1]))
                self._ready.set()
                await self._stop.wait()  # type: ignore[union-attr]
            finally:
                server.close()
                await server.wait_closed()

    def _connect_frame(self) -> dict[str, object]:
        """The ``relay_connect`` frame for this tunnel's target, built once."""
        return build_relay_connect_frame(
            cid=self._cid,
            bucket=self._bucket,
            target_cid=self._target_cid,
            group=self._group,
            bucket_token=self._token,
        )

    def _handle(self, quic: Any) -> Any:
        """Loopback accept callback: one relay session per HTTPS connection.

        A sync cycle opens several TCP connections to the loopback endpoint
        (REST requests plus the WSS channel); the tunnel must survive each of
        them, so every accepted connection pairs its **own** ``relay_connect``
        session and is bridged over it. The previous single-session design died
        with the first connection and the next one hit ``Connection refused``.
        """

        async def _handle(tcp_reader: Any, tcp_writer: Any) -> None:
            stream_reader, stream_writer, refusal = await _probe_relay_connect(
                quic, self._connect_frame(), self._client.timeout
            )
            if stream_reader is None:
                if refusal is not None:
                    log.debug(f"Relay session refused by the node: {refusal}")
                tcp_writer.close()
                return
            log.info("Relay tunnel established with the master (via the discovery server)")
            await bridge(tcp_reader, tcp_writer, stream_reader, stream_writer)

        return _handle


class RelayAcceptor:
    """Serving side: accept relayed sessions and hand them to the local master."""

    def __init__(
        self,
        client: BootnodeClient,
        *,
        cid: str,
        bucket: str,
        local_port: int,
        group: str | None = None,
        bucket_token: str | None = None,
    ) -> None:
        self._client = client
        self._cid = cid
        self._bucket = bucket
        self._local_port = local_port
        self._group = group
        self._token = bucket_token
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._registered = False

    @property
    def active(self) -> bool:
        """True while the node holds this node's relay listener."""
        return self._registered and self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 10.0) -> bool:
        """Register the listener with the node; ``False`` when it is unavailable."""
        self._thread = threading.Thread(
            target=self._thread_main, name="hsync-relay-acceptor", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            log.debug("Relay listener registration timed out")
            self.stop()
            return False
        return self._registered

    def stop(self) -> None:
        """Withdraw the listener and drop every live session (idempotent)."""
        loop, stop, thread = self._loop, self._stop, self._thread
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        self._registered = False

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._stop = asyncio.Event()
        try:
            loop.run_until_complete(self._run())
        except BaseException as exc:  # noqa: BLE001 - reported through the log
            log.debug(f"Relay acceptor stopped: {exc}")
        finally:
            self._ready.set()
            loop.close()

    async def _run(self) -> None:
        tasks: list[asyncio.Task[Any]] = []
        sessions: set[asyncio.Task[Any]] = set()
        try:
            async with _quic_session(self._client) as quic:
                reader, writer = await quic.create_stream()
                writer.write(
                    frame_line(
                        build_relay_listen_frame(
                            cid=self._cid,
                            bucket=self._bucket,
                            group=self._group,
                            bucket_token=self._token,
                        )
                    )
                )
                await writer.drain()
                reply = await asyncio.wait_for(reader.readline(), self._client.timeout)
                raw = bytes(reply).rstrip(b"\n")
                refusal = refusal_code(raw)
                if refusal is not None:
                    log.debug(f"Relay listener refused by the node: {refusal}")
                    return
                if raw and validate_boot_relay_listening(json.loads(raw)) is None:
                    raise BootnodeProtocolError("expected a 'relay_listening' frame")
                self._registered = True
                self._ready.set()
                log.info("Relay listener registered with the discovery server")
                tasks = [
                    asyncio.create_task(self._read_incoming(quic, reader, sessions)),
                    asyncio.create_task(self._keepalive(writer)),
                ]
                await self._stop.wait()  # type: ignore[union-attr]
        finally:
            self._registered = False
            for task in (*tasks, *sessions):
                task.cancel()
            if tasks or sessions:
                await asyncio.gather(*tasks, *sessions, return_exceptions=True)

    async def _read_incoming(
        self, quic: Any, reader: Any, sessions: set[asyncio.Task[Any]]
    ) -> None:
        """Turn every ``relay_incoming`` into a bridged session."""
        while True:
            try:
                line = await reader.readline()
            except (ConnectionError, OSError):
                return
            if not line:
                return
            try:
                frame = validate_boot_relay_incoming(json.loads(bytes(line).rstrip(b"\n")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            if frame is None:
                return
            task = asyncio.create_task(self._serve_session(quic, str(frame["relay_id"])))
            sessions.add(task)
            task.add_done_callback(sessions.discard)

    async def _serve_session(self, quic: Any, relay_id: str) -> None:
        """Attach one relayed stream and bridge it to the local data plane."""
        try:
            reader, writer = await quic.create_stream()
            writer.write(
                frame_line(
                    build_relay_attach_frame(
                        cid=self._cid,
                        bucket=self._bucket,
                        relay_id=relay_id,
                        bucket_token=self._token,
                    )
                )
            )
            await writer.drain()
            local_reader, local_writer = await asyncio.open_connection(
                "127.0.0.1", self._local_port
            )
        except (ConnectionError, OSError):
            return
        log.info("Relay session bridged to the local master")
        await bridge(reader, writer, local_reader, local_writer)

    async def _keepalive(self, writer: Any) -> None:
        """Keep the control stream (and therefore the QUIC connection) alive."""
        while True:
            await asyncio.sleep(RELAY_KEEPALIVE_INTERVAL)
            writer.write(frame_line(build_relay_ping_frame(cid=self._cid, bucket=self._bucket)))
            await writer.drain()


class FailoverRelayDialer:
    """Relay dialer that pairs through the first discovery server that accepts it.

    With several discovery servers configured, the peer a relay session targets
    may be listening on any of them and a ``query`` record does not say which
    one paired it. Each server is asked in order and the first ``relay_ok``
    wins, so the caller keeps its single "open, then close" contract.
    """

    def __init__(self, dialers: Sequence[RelayDialer]) -> None:
        self._dialers = list(dialers)
        self._winner: RelayDialer | None = None

    def open(self, timeout: float = 15.0) -> tuple[str, int] | None:
        """Try each server in order within one deadline; ``None`` when none pairs.

        The budget covers the whole attempt, not each server: N servers must not
        multiply the caller's timeout (this runs in the discovery loop, where a
        blocked call delays serving). Servers are tried in order, so a slow first
        one can consume the remaining budget of the others.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        for dialer in self._dialers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            endpoint = dialer.open(remaining)
            if endpoint is not None:
                self._winner = dialer
                return endpoint
        return None

    def close(self) -> None:
        """Tear down the winning tunnel and any attempt that is still open."""
        self._winner = None
        for dialer in self._dialers:
            dialer.close()


class GroupRelayAcceptor:
    """Relay listener registered on **every** discovery server of the group.

    A peer reaches this node through whichever server it is paired with, so the
    serving side must be present on all of them; a single successful
    registration is enough for the relay to be considered available.
    """

    def __init__(self, acceptors: Sequence[RelayAcceptor]) -> None:
        self._acceptors = list(acceptors)

    @property
    def active(self) -> bool:
        """True while at least one server still holds this node's listener."""
        return any(acceptor.active for acceptor in self._acceptors)

    def start(self, timeout: float = 10.0) -> bool:
        """Register on every server within one deadline; ``True`` if any accepted.

        The budget is shared, not per server, so the master-ready path stays
        bounded with N servers; a slow first server can use up the budget of the
        others (the listener is registered where it answered in time).
        """
        deadline = time.monotonic() + max(0.0, timeout)
        started = False
        for acceptor in self._acceptors:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if acceptor.start(remaining):
                started = True
        return started

    def stop(self) -> None:
        """Withdraw the listener from every server (idempotent)."""
        for acceptor in self._acceptors:
            acceptor.stop()
