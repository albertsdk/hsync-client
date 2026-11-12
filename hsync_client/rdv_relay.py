# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Data relay over ``rdv/1``: reach a peer that no inbound NAT mapping reaches.

Two peers that are both behind carrier-grade NAT cannot dial each other: neither
accepts an inbound TCP connection. The rendezvous is the one element of the group
that is publicly reachable, so it can pair them and forward their TLS bytes
opaque (``rdv/1`` relay frames). This module is the client half:

* :class:`RelayDialer` — the requester side. It opens **one** relay session and
  then exposes it as a **loopback TCP listener**: the regular HTTPS/WSS client
  dials ``127.0.0.1`` and never learns that the bytes travel through the node.
  Every loopback connection is multiplexed onto that single session with a
  private framing layer the node never inspects. The master's certificate pin
  still authenticates the peer, so the relay is a transport detail, never an
  identity shortcut.
* :class:`RelayAcceptor` — the serving side. It holds the listener control
  stream and, for each announced session, demultiplexes the relayed stream onto
  its local ``listen_port``, where the master's own HTTPS/WSS server terminates
  TLS exactly as it does for a direct connection. A local connection is opened
  only when its stream carries data, so a connection that never sends bytes does
  not reach the master.

Both run their own asyncio loop in a background thread (the sync engine is
threaded) and reuse the rendezvous client's trust machinery
(``RendezvousClient.relay_configuration``/``verify_peer``), so pins, TOFU and the
SSH-style prompt behave exactly as they do for rendezvous.

Consumers: ``hsync-client`` (``p2p_handler`` through the rendezvous plan), tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import Sequence
from typing import Any

from hsync_common.protocol.rendezvous import (
    validate_rdv_relay_incoming,
    validate_rdv_relay_listening,
)
from loguru import logger as log

from hsync_client.constants import (
    RELAY_BUFFER_BYTES,
    RELAY_KEEPALIVE_INTERVAL,
    RELAY_RETRY_BACKOFF_S,
)
from hsync_client.rdv_client import (
    RendezvousClient,
    RendezvousProtocolError,
    frame_line,
)
from hsync_client.rdv_frames import (
    build_relay_attach_frame,
    build_relay_connect_frame,
    build_relay_listen_frame,
    build_relay_ping_frame,
)
from hsync_client.rdv_relay_transport import (
    _RELAY_SUBSTREAM_CLOSE,
    _RELAY_SUBSTREAM_DATA,
    _RELAY_SUBSTREAM_PING,
    _close_writer,
    _encode_substream,
    _probe_relay_connect,
    _read_substream,
    _relay_session,
    _RelaySession,
    _session_open_stream,
    refusal_code,
)

__all__ = [
    "FailoverRelayDialer",
    "GroupRelayAcceptor",
    "RelayAcceptor",
    "RelayDialer",
]


class RelayDialer:
    """Requester side: one relay tunnel exposed as a loopback TCP endpoint.

    A tunnel is long-lived (until :meth:`close`) and pairs **one** relay session
    with the node. Every TCP connection accepted on loopback is multiplexed onto
    that session with a private framing layer (:func:`_encode_substream`), so the
    data plane (REST + WSS, several connections per sync cycle) rides the tunnel
    without spending a ``relay_connect`` token per connection.
    """

    def __init__(
        self,
        client: RendezvousClient,
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
        self._server: Any | None = None
        # The QUIC session the tunnel bridges over right now, and the flag a caller
        # waits on: the endpoint outlives the session (it is bound once), so a
        # caller asks for a *live* session rather than for the tunnel's existence.
        self._session: Any | None = None
        self._session_up = threading.Event()
        # The single paired session's data stream and the loopback connections
        # multiplexed onto it, keyed by a per-session stream id.
        self._data_reader: Any | None = None
        self._data_writer: Any | None = None
        self._streams: dict[int, Any] = {}
        self._next_stream_id = 0
        self._frame_lock: asyncio.Lock | None = None
        # The wait the node asked for when it refused the last pairing attempt
        # (``retry_after``), and how many sessions dropped since the last one was
        # held: the first drop is news, the repeats are not.
        self._retry_after = 0.0
        self._drops = 0
        # Uniform refusal code from the node's last ``relay_error`` (if any), so
        # the caller can tell "the node did not pair the relay" from a tunnel
        # that opened; ``None`` before a refusal is seen.
        self._refusal: str | None = None
        # ``Relay tunnel established`` is news once per tunnel; re-pairing a
        # dropped session is routine and logged at debug.
        self._announced = False

    def open(self, timeout: float = 15.0) -> tuple[str, int] | None:
        """Pair the session and return the loopback ``(host, port)`` to dial.

        ``None`` means the node could not pair it (relay disabled, no listener
        for the target, capacity reached): the caller then keeps its normal
        "no reachable endpoint, retry rendezvous" path. The reason, when the
        node answered one, is readable afterwards through :attr:`refusal`.
        """
        self._refusal = None
        self._thread = threading.Thread(
            target=self._thread_main, name="hsync-relay-dialer", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            log.debug("Relay session open timed out")
            self.close()
            return None
        return self._endpoint

    @property
    def refusal(self) -> str | None:
        """The node's refusal code for the last pairing attempt, or ``None``.

        ``relay_unavailable`` means the node did not pair the session (its data
        relay is disabled, or the target has no listener registered); callers use
        it to surface an actionable hint instead of only "no reachable endpoint".
        """
        return self._refusal

    @property
    def endpoint(self) -> tuple[str, int] | None:
        """The loopback ``(host, port)`` this tunnel serves, or ``None``."""
        return self._endpoint

    @property
    def target_cid(self) -> str:
        """The peer this tunnel reaches."""
        return self._target_cid

    @property
    def alive(self) -> bool:
        """True while the tunnel exists: its endpoint is bound and still serving."""
        thread = self._thread
        return self._endpoint is not None and thread is not None and thread.is_alive()

    @property
    def session_up(self) -> bool:
        """True while a relay session is paired and the tunnel can serve."""
        return self._session_up.is_set()

    @property
    def repairing(self) -> bool:
        """True while the tunnel exists and is re-pairing its session.

        A caller waits for a tunnel that is repairing — the session is coming
        back — and never for one whose last attempt the node *refused*: there is
        nothing to wait for until the tunnel's own retry runs, and blocking on it
        would stall the caller's loop for nothing.
        """
        return self.alive and not self._session_up.is_set() and self._refusal is None

    def _note_session_lost(self) -> None:
        """Report a dropped session once per episode, then at DEBUG.

        A node that keeps closing the tunnel (a restart loop, a spent
        ``relay_connect`` budget) would otherwise write the same line at every
        retry: the first drop is news, the repeats are not.
        """
        self._drops += 1
        if self._drops == 1:
            log.info("Relay session lost; re-pairing the tunnel")
        else:
            log.debug(f"Relay session lost again ({self._drops}); re-pairing the tunnel")

    def wait_session(self, timeout: float) -> tuple[str, int] | None:
        """Wait up to *timeout* for a live session; ``None`` when there is none.

        The tunnel repairs itself in the background and keeps its port, so a
        caller that finds it between two sessions waits instead of replacing it:
        a new tunnel would bind a new loopback port and strand every connection
        the HTTP client and the WebSocket already hold on the old one.
        """
        if not self._session_up.wait(timeout):
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
        # Prompt for a waiter: the loop clears it too, but only once it unwinds.
        self._session_up.clear()

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
        """Hold a relay session for the tunnel's whole life, repairing it on a drop.

        The loopback endpoint is bound once (:meth:`_bind_endpoint`) and never
        moves: the HTTP client and the WebSocket keep pooled connections on its
        port, so re-binding after a dropped session would strand them on a dead
        port — the peer sees that as an SSL EOF. A session that dies is re-paired
        at once (a reset heals in milliseconds) and only a *failed* attempt arms
        the retry backoff, so a node that is down is not hammered.
        """
        try:
            while not self._stop.is_set():  # type: ignore[union-attr]
                held = await self._hold_session()
                if self._stop.is_set():  # type: ignore[union-attr]
                    break
                if held:
                    continue  # the session ended: repair immediately
                if self._server is None:
                    # Refused before any endpoint existed: ``open()`` reports
                    # ``None`` and the caller keeps its own retry path.
                    return
                # A refusal after the endpoint was exposed (the master stopped
                # listening) keeps the tunnel and its port; it retries here. The
                # node's own wait wins when it asked for one — a spent
                # ``relay_connect`` budget is answered as ``rate_limited`` with
                # ``retry_after`` — because retrying sooner only deepens it.
                await self._sleep_or_stop(max(RELAY_RETRY_BACKOFF_S, self._retry_after))
        finally:
            server, self._server = self._server, None
            self._endpoint = None if server is not None else self._endpoint
            if server is not None:
                server.close()
                await server.wait_closed()

    async def _hold_session(self) -> bool:
        """Pair the tunnel's relay session and multiplex loopback connections on it.

        ``True`` means a session was paired (and has now ended), ``False`` that
        the node refused it. The loopback endpoint is bound on the first
        successful pairing and kept for the tunnel's life. The paired stream is
        the tunnel's data plane for its whole life — no ``relay_connect`` is
        spent per loopback connection.
        """
        async with _relay_session(self._client) as session:
            stream_reader, stream_writer, refusal, retry_after = await _probe_relay_connect(
                session, self._connect_frame(), self._client.timeout
            )
            if stream_reader is None:
                self._refusal = refusal
                self._retry_after = float(retry_after or 0)
                if refusal is not None:
                    log.debug(f"Relay session refused by the node: {refusal}")
                return False
            self._refusal = None
            self._retry_after = 0.0
            self._drops = 0
            self._session = session
            self._data_reader = stream_reader
            self._data_writer = stream_writer
            self._streams = {}
            self._next_stream_id = 0
            self._frame_lock = asyncio.Lock()
            self._session_up.set()
            if not self._announced:
                log.info("Relay tunnel established with the master (via the discovery servers)")
                self._announced = True
            else:
                log.debug("Relay tunnel re-established with the master")
            dispatch = asyncio.create_task(self._dispatch_frames(stream_reader))
            data_keepalive = asyncio.create_task(self._data_keepalive())
            control_writer: Any | None = None
            keepalive: asyncio.Task[Any] | None = None
            try:
                if self._server is None:
                    await self._bind_endpoint()
                if getattr(session, "transport", "quic") == "quic":
                    # QUIC: the connection itself idles once no stream carries
                    # traffic, so the node's QUIC idle timeout would reap the
                    # tunnel during any pause; the control stream's ``relay_ping``
                    # keeps it alive and is also the session's death signal (its
                    # ``drain`` raises once the connection is gone). TLS/TCP needs
                    # no control connection (``relay_ping`` is a no-op on the
                    # node, and the in-band substream keepalive keeps the data
                    # connection alive), so only QUIC opens one.
                    control_writer = (await _session_open_stream(session))[1]
                    keepalive = asyncio.create_task(self._keepalive(control_writer))
                stop_task = asyncio.create_task(self._stop.wait())  # type: ignore[union-attr]
                watchers = [stop_task, dispatch] + ([keepalive] if keepalive is not None else [])
                try:
                    await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in watchers:
                        task.cancel()
                    await asyncio.gather(*watchers, return_exceptions=True)
                    if not self._stop.is_set():  # type: ignore[union-attr]
                        self._note_session_lost()
            finally:
                for task in (dispatch, data_keepalive):
                    task.cancel()
                await asyncio.gather(dispatch, data_keepalive, return_exceptions=True)
                # Close the QUIC streams while their connection is still open:
                # aioquic's stream writer otherwise tries to end a stream on the
                # finished connection in its deallocator (the "Cannot send data
                # on a finished stream" noise).
                _close_writer(self._data_writer)
                _close_writer(control_writer)
                for tcp_writer in list(self._streams.values()):
                    _close_writer(tcp_writer)
                self._streams.clear()
                self._data_reader = None
                self._data_writer = None
                self._frame_lock = None
                self._session = None
                self._session_up.clear()
        return True

    async def _dispatch_frames(self, reader: Any) -> None:
        """Deliver the peer's multiplexed frames to the matching loopback socket."""
        while True:
            frame = await _read_substream(reader)
            if frame is None:
                return
            kind, stream_id, payload = frame
            if kind == _RELAY_SUBSTREAM_PING:
                continue
            tcp_writer = self._streams.get(stream_id)
            if tcp_writer is None:
                continue
            if kind == _RELAY_SUBSTREAM_CLOSE:
                self._streams.pop(stream_id, None)
                _close_writer(tcp_writer)
                continue
            try:
                tcp_writer.write(payload)
                await tcp_writer.drain()
            except (ConnectionError, OSError):
                self._streams.pop(stream_id, None)
                _close_writer(tcp_writer)
                # Tell the peer this stream is done so it tears its loopback
                # connection down now, instead of one keepalive interval later.
                await self._send_frame(_RELAY_SUBSTREAM_CLOSE, stream_id)

    async def _send_frame(self, kind: int, stream_id: int, payload: bytes = b"") -> None:
        """Write one multiplexed frame, serialized against the other streams."""
        writer = self._data_writer
        lock = self._frame_lock
        if writer is None or lock is None:
            return
        async with lock:
            try:
                writer.write(_encode_substream(kind, stream_id, payload))
                await writer.drain()
            except (ConnectionError, OSError):
                pass

    async def _data_keepalive(self) -> None:
        """Keep the multiplexed data session active through the node's idle reaping."""
        while True:
            await asyncio.sleep(RELAY_KEEPALIVE_INTERVAL)
            await self._send_frame(_RELAY_SUBSTREAM_PING, 0)

    async def _bind_endpoint(self) -> None:
        """Bind the loopback endpoint the caller dials, once per tunnel."""
        server = await asyncio.start_server(self._handle(), "127.0.0.1", 0)
        socket = server.sockets[0]
        self._endpoint = ("127.0.0.1", int(socket.getsockname()[1]))
        self._server = server
        self._ready.set()

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep *seconds* unless the tunnel is stopped first."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)  # type: ignore[union-attr]
        except TimeoutError:
            pass

    async def _keepalive(self, writer: Any) -> None:
        """Keep the QUIC connection (and thus the loopback tunnel) alive."""
        while True:
            await asyncio.sleep(RELAY_KEEPALIVE_INTERVAL)
            writer.write(frame_line(build_relay_ping_frame(cid=self._cid, bucket=self._bucket)))
            await writer.drain()

    def _connect_frame(self) -> dict[str, object]:
        """The ``relay_connect`` frame for this tunnel's target, built once."""
        return build_relay_connect_frame(
            cid=self._cid,
            bucket=self._bucket,
            target_cid=self._target_cid,
            group=self._group,
            bucket_token=self._token,
        )

    def _handle(self) -> Any:
        """Loopback accept callback: multiplex the connection onto the session.

        A sync cycle opens several TCP connections to the loopback endpoint
        (REST requests plus the WSS channel); they all share the tunnel's single
        paired relay session, framed by stream id. A connection that arrives
        while the tunnel repairs its session is closed at once: the caller's own
        retry and the WebSocket reconnect recover, while holding the socket would
        only park a request on a tunnel that is not there.
        """

        async def _handle(tcp_reader: Any, tcp_writer: Any) -> None:
            if self._data_writer is None:
                _close_writer(tcp_writer)
                return
            stream_id = self._next_stream_id
            self._next_stream_id += 1
            self._streams[stream_id] = tcp_writer
            try:
                while True:
                    chunk = await tcp_reader.read(RELAY_BUFFER_BYTES)
                    if not chunk:
                        break
                    await self._send_frame(_RELAY_SUBSTREAM_DATA, stream_id, chunk)
            except (ConnectionError, OSError, asyncio.CancelledError):
                pass
            finally:
                self._streams.pop(stream_id, None)
                await self._send_frame(_RELAY_SUBSTREAM_CLOSE, stream_id)
                _close_writer(tcp_writer)

        return _handle


class RelayAcceptor:
    """Serving side: accept relayed sessions and hand them to the local master."""

    def __init__(
        self,
        client: RendezvousClient,
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
        stop_task: asyncio.Task[Any] | None = None
        # Everything is torn down *inside* the session context: the streams are
        # closed while their connection is still open, so aioquic's stream
        # writer never ends a stream on the finished connection from its
        # deallocator (and the TLS/TCP session closes every connection it owns).
        async with _relay_session(self._client) as session:
            control_writer: Any | None = None
            try:
                reader, writer = await _session_open_stream(session)
                control_writer = writer
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
                if raw and validate_rdv_relay_listening(json.loads(raw)) is None:
                    raise RendezvousProtocolError("expected a 'relay_listening' frame")
                self._registered = True
                self._ready.set()
                log.info("Relay listener registered with the discovery servers")
                tasks = [
                    asyncio.create_task(self._read_incoming(session, reader, sessions)),
                    asyncio.create_task(self._keepalive(writer)),
                ]
                # Exit as soon as the control stream/connection dies — the
                # keepalive or the incoming loop returning means the node dropped
                # the listener or the connection is gone. Waiting on ``stop``
                # alone would leave the thread alive after the node dropped the
                # listener, so ``active`` could never trigger a re-registration.
                stop_task = asyncio.create_task(self._stop.wait())  # type: ignore[union-attr]
                await asyncio.wait([stop_task, *tasks], return_when=asyncio.FIRST_COMPLETED)
            finally:
                self._registered = False
                waiters = [*tasks, *sessions]
                if stop_task is not None:
                    waiters.append(stop_task)
                for task in waiters:
                    task.cancel()
                if waiters:
                    await asyncio.gather(*waiters, return_exceptions=True)
                _close_writer(control_writer)

    async def _read_incoming(
        self, session: _RelaySession, reader: Any, sessions: set[asyncio.Task[Any]]
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
                frame = validate_rdv_relay_incoming(json.loads(bytes(line).rstrip(b"\n")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            if frame is None:
                return
            task = asyncio.create_task(self._serve_session(session, str(frame["relay_id"])))
            sessions.add(task)
            task.add_done_callback(sessions.discard)

    async def _serve_session(self, session: _RelaySession, relay_id: str) -> None:
        """Attach one relayed stream and demultiplex its loopback connections.

        The local TCP connection to the master is opened lazily on the first
        ``DATA`` frame for a stream id: one relayed stream serves many loopback
        connections, and a stream that never carries bytes (a closed or probing
        connection) must not reach the master's TLS listener at all.
        """
        try:
            reader, writer = await _session_open_stream(session)
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
        except (ConnectionError, OSError):
            return
        log.info("Relay session bridged to the local master")
        frame_lock = asyncio.Lock()
        local_writers: dict[int, Any] = {}
        # Stream ids never repeat within a session, so an id that is done is
        # recorded here: a late DATA frame for it must be dropped, never used to
        # open a fresh loopback connection. Re-opening mid-stream splices a new
        # TLS connection into the middle of the old one, which looks to the
        # master's TLS listener like garbage (``WRONG_VERSION_NUMBER``).
        closed_streams: set[int] = set()
        pumps: set[asyncio.Task[Any]] = set()

        async def _close_stream(stream_id: int) -> None:
            """Drop one loopback stream and tell the peer it is finished."""
            _close_writer(local_writers.pop(stream_id, None))
            if stream_id in closed_streams:
                return
            closed_streams.add(stream_id)
            async with frame_lock:
                with contextlib.suppress(ConnectionError, OSError):
                    writer.write(_encode_substream(_RELAY_SUBSTREAM_CLOSE, stream_id))
                    await writer.drain()

        async def _local_to_session(stream_id: int, local_reader: Any) -> None:
            try:
                while True:
                    chunk = await local_reader.read(RELAY_BUFFER_BYTES)
                    if not chunk:
                        break
                    async with frame_lock:
                        writer.write(_encode_substream(_RELAY_SUBSTREAM_DATA, stream_id, chunk))
                        await writer.drain()
            except (ConnectionError, OSError, asyncio.CancelledError):
                pass
            finally:
                with contextlib.suppress(ConnectionError, OSError, asyncio.CancelledError):
                    await _close_stream(stream_id)

        try:
            while True:
                frame = await _read_substream(reader)
                if frame is None:
                    return
                kind, stream_id, payload = frame
                if kind == _RELAY_SUBSTREAM_PING:
                    continue
                if stream_id in closed_streams:
                    continue
                if kind == _RELAY_SUBSTREAM_CLOSE:
                    await _close_stream(stream_id)
                    continue
                # kind == _RELAY_SUBSTREAM_DATA: open the loopback connection
                # lazily, on the first byte, and only for a stream that is still
                # live.
                local_writer = local_writers.get(stream_id)
                if local_writer is None:
                    try:
                        local_reader, local_writer = await asyncio.open_connection(
                            "127.0.0.1", self._local_port
                        )
                    except (ConnectionError, OSError):
                        # The master is not accepting: fail this stream now with a
                        # CLOSE instead of stranding the peer's connection.
                        await _close_stream(stream_id)
                        continue
                    local_writers[stream_id] = local_writer
                    task = asyncio.create_task(_local_to_session(stream_id, local_reader))
                    pumps.add(task)
                    task.add_done_callback(pumps.discard)
                try:
                    local_writer.write(payload)
                    await local_writer.drain()
                except (ConnectionError, OSError):
                    await _close_stream(stream_id)
        finally:
            for stream_id in list(local_writers):
                _close_writer(local_writers.pop(stream_id))
            for task in list(pumps):
                task.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            _close_writer(writer)

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
        if not dialers:
            raise ValueError("a failover dialer needs at least one server to try")
        self._dialers = list(dialers)
        self._winner: RelayDialer | None = None
        # Refusal of the last failed attempt, so the caller can report why the
        # combined dial could not pair even when no single dialer is the cause.
        self._refusal: str | None = None

    @property
    def target_cid(self) -> str:
        """The peer every dialer in this set reaches (one target per set)."""
        return self._current().target_cid

    @property
    def endpoint(self) -> tuple[str, int] | None:
        """The winning tunnel's loopback ``(host, port)``, or ``None``."""
        return self._current().endpoint

    @property
    def alive(self) -> bool:
        """True while the winning tunnel exists and its endpoint is serving."""
        winner = self._winner
        return winner is not None and winner.alive

    @property
    def session_up(self) -> bool:
        """True while the winning tunnel has a relay session paired."""
        winner = self._winner
        return winner is not None and winner.session_up

    @property
    def repairing(self) -> bool:
        """True while the winning tunnel is re-pairing its session."""
        winner = self._winner
        return winner is not None and winner.repairing

    def wait_session(self, timeout: float) -> tuple[str, int] | None:
        """Wait for the winning tunnel's session (see :meth:`RelayDialer.wait_session`)."""
        winner = self._winner
        return None if winner is None else winner.wait_session(timeout)

    def _current(self) -> RelayDialer:
        """The dialer the accessors speak for: the winner, else the first try."""
        return self._winner if self._winner is not None else self._dialers[0]

    def open(self, timeout: float = 15.0) -> tuple[str, int] | None:
        """Try each server in order within one deadline; ``None`` when none pairs.

        The budget covers the whole attempt, not each server: N servers must not
        multiply the caller's timeout (this runs in the rendezvous loop, where a
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
                self._refusal = None
                return endpoint
        self._refusal = next(
            (d.refusal for d in self._dialers if getattr(d, "refusal", None)), None
        )
        return None

    @property
    def refusal(self) -> str | None:
        """The refusal code of the last failed attempted server, or ``None``."""
        return self._refusal

    def close(self) -> None:
        """Tear down the winning tunnel and any attempt that is still open."""
        self._winner = None
        self._refusal = None
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
