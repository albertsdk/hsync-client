# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Client for the ``rdv/1`` rendezvous protocol (QUIC via aioquic).

Wire contract (SSOT: ``hsync-rendezvous/protocol-spec/rdv-1.md``, mirrored and
validated in ``hsync_common.protocol.rendezvous``): one QUIC/TLS 1.3 connection
with ALPN ``hsync-rdv/1``; every bidirectional stream is one session; frames
are newline-terminated JSON (NDJSON). A request writes one frame and reads the
single response line (or ``None`` when the node silently drops it).

This module exposes a synchronous API (the client is thread-based) around a
private asyncio loop, and keeps the framing/validation here so the rendezvous
plan (``p2p_rendezvous_rdv``) only deals with peer state. It never treats a
response as authoritative beyond the wire contract.

Consumers: ``hsync-client`` (p2p_rendezvous_rdv), tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from aioquic.asyncio.client import connect
from aioquic.quic.configuration import QuicConfiguration
from cryptography.hazmat.primitives import serialization
from hsync_common.constants import (
    RDV_TCP_FALLBACK_TTL_S,
    RDV_TRANSPORT_DEFAULT,
    RDV_TRANSPORT_STAGGER_S,
)
from hsync_common.endpoint import format_authority
from hsync_common.protocol.pairing import fingerprint_matches
from hsync_common.protocol.rendezvous import (
    PROTOCOL_VERSION_RDV,
    RDV_ALPN,
    RdvAck,
    RdvLeave,
    RdvQuery,
    RdvRefresh,
    RdvRegister,
    RdvRelayAttach,
    RdvRelayConnect,
    RdvRelayListen,
    RdvRelayPing,
    RdvResponse,
    validate_rdv_ack,
    validate_rdv_frame,
    validate_rdv_response,
)
from hsync_common.protocol.tls_trust import (
    TrustAction,
    TrustPolicy,
    evaluate_tls_trust,
)
from hsync_common.ssl_utils import fingerprint_from_der
from hsync_common.text import compact_json_bytes
from hsync_common.tls_context import unverified_ssl_context
from loguru import logger as log

from hsync_client.constants import RELAY_IDLE_TIMEOUT_S
from hsync_client.events import UiEventName, emit_event
from hsync_client.rendezvous_trust import RendezvousTrustStore, default_rendezvous_trust_store
from hsync_client.trust_messages import known_hosts_added_message, mismatch_guidance

__all__ = [
    "DEFAULT_QUIC_IDLE_TIMEOUT",
    "RendezvousClient",
    "RendezvousError",
    "RendezvousFingerprintMismatch",
    "RendezvousNotTrusted",
    "RendezvousProtocolError",
    "RendezvousRefused",
    "RendezvousUnreachable",
    "TrustPrompt",
    "build_leave_frame",
    "build_query_frame",
    "build_refresh_frame",
    "build_register_frame",
    "build_relay_attach_frame",
    "build_relay_connect_frame",
    "build_relay_listen_frame",
    "build_relay_ping_frame",
    "frame_line",
]

# QUIC idle timeout: a single request/response exchange is well under a second
# on any usable link; the timeout bounds a stalled handshake, not throughput.
DEFAULT_QUIC_IDLE_TIMEOUT = 30.0

# SSH-style trust prompt: ``(host, port, observed_fingerprint) -> accept?``.
# Supplied by the front-end (terminal for the CLI; the desktop confirms with a
# pre-flight instead). ``None`` means the caller cannot prompt (non-interactive),
# so policy ``ask`` must fail closed.
TrustPrompt = Callable[[str, int, str], bool]


class RendezvousError(Exception):
    """Base class for rendezvous client failures."""


class RendezvousUnreachable(RendezvousError):
    """The rendezvous could not be reached (DNS, timeout, TLS, connection)."""


class RendezvousProtocolError(RendezvousError):
    """The rendezvous answered with something that violates ``rdv/1``."""


class RendezvousRefused(RendezvousError):
    """The node answered ``register``/``refresh`` with a uniform refusal frame.

    ``rdv/1`` replies to a rate-limited, token-refused or capacity-refused
    ``register``/``refresh`` with the same ``response`` frame it uses for
    ``query`` refusals
    (``{peers: [], count: 0, error: "token_required"|"rate_limited"|"overloaded"}``).
    That is a legitimate protocol outcome, not a contract violation and not a
    transport failure: the lease loop must keep its state and retry at its normal
    cadence instead of re-registering from scratch (which would only deepen the
    rate limit). ``retry_after`` is the node's advisory backoff, in seconds; for
    ``overloaded`` it is node-capacity backpressure, not a client abuse signal.
    """

    def __init__(self, error: str, retry_after: int | None = None) -> None:
        detail = f" (retry after {retry_after}s)" if retry_after else ""
        super().__init__(f"{error}{detail}")
        self.error = error
        self.retry_after = retry_after


class RendezvousFingerprintMismatch(RendezvousError):
    """The rendezvous certificate fingerprint did not match the accepted pin(s).

    Fatal: the connection is closed before any frame is sent, and the rendezvous
    lease loop must not retry it. ``str`` keeps the concise case-specific reason
    (changed pin, node not pinned under ``strict``, no explicit-pin match), while
    ``guidance`` carries the SSH-style recovery block the front-end logs once:
    the presented fingerprint, the known-hosts file and the exact
    ``hsync trust rendezvous-server`` commands that unblock the situation.
    """

    def __init__(self, message: str, *, guidance: str | None = None) -> None:
        super().__init__(message)
        self.guidance = guidance


class RendezvousNotTrusted(RendezvousError):
    """Policy ``ask`` could not establish trust: the user declined or cannot prompt.

    Fatal, like :class:`RendezvousFingerprintMismatch`: the node is not contacted,
    the lease loop stops and the sync core exits non-zero. ``non_interactive``
    distinguishes "no terminal to ask" (fail closed) from an explicit decline.
    """

    def __init__(self, message: str, *, non_interactive: bool = False) -> None:
        super().__init__(message)
        self.non_interactive = non_interactive


def _unreachable(host: str, port: int, exc: BaseException) -> RendezvousUnreachable:
    """Build the unreachable error; aioquic's ``ConnectionError`` has no text."""
    detail = str(exc) or type(exc).__name__
    return RendezvousUnreachable(f"rendezvous {format_authority(host, port)}: {detail}")


_T = TypeVar("_T")


@dataclass(slots=True)
class _Dial:
    """One dialed ``rdv/1`` transport session, owning its connection.

    ``reader``/``writer`` are the single request/response stream of the session
    (the fresh QUIC stream, or the TLS/TCP connection itself); ``peer_cert_der``
    is the node's certificate in DER form for fingerprint verification. The
    transport stays open until :meth:`close` (and only until then), so the
    caller owns the lifetime instead of a context manager the dial no longer
    sees.
    """

    transport: str
    port: int
    reader: Any
    writer: Any
    peer_cert_der: bytes | None
    _closer: Any | None = None

    async def close(self) -> None:
        if self._closer is not None:
            await self._closer()
            self._closer = None


async def _close_unused_dials(
    tasks: Iterable[asyncio.Task[_Dial]], winner: asyncio.Task[_Dial] | None
) -> None:
    """Close every attempt that completed but did not win the race.

    A cancelled attempt is already torn down by its caller; an attempt that
    *completed* before losing holds an open transport that nothing else owns, so
    it must be closed or the connection leaks. Best-effort: a failing teardown
    must not turn a decided race into an error.
    """
    for task in tasks:
        if task is winner or not task.done() or task.cancelled():
            continue
        if task.exception() is not None:
            continue
        with contextlib.suppress(Exception):
            await task.result().close()


@dataclass(frozen=True)
class RendezvousClient:
    """Synchronous ``rdv/1`` client for one rendezvous.

    Trust follows the shared policy in
    :mod:`hsync_common.protocol.tls_trust` (ADR-0035): ``policy`` selects
    ``ask`` (prompt on an unknown node, refuse a changed one; the default),
    ``accept_new`` (silent first-sighting pin), ``strict`` (explicit pin
    required) or ``none`` (no verification). ``fingerprints`` pins one or two
    fingerprints (the second allows a rotation window) and is what ``strict``
    matches against.

    ``prompt`` answers policy ``ask`` on a terminal; ``None`` means the caller
    cannot prompt (non-interactive), where ``ask`` fails closed (SSH ``BatchMode``).
    """

    host: str
    port: int
    fingerprints: tuple[str, ...] = ()
    policy: TrustPolicy = TrustPolicy.ask
    prompt: TrustPrompt | None = None
    timeout: float = 10.0
    idle_timeout: float = DEFAULT_QUIC_IDLE_TIMEOUT
    # Ordered additional UDP ports to try for the SAME node (multi-port dial
    # fallback): a client egress firewall may block one UDP port but allow
    # another (RDV_PORT_SET). ``port`` is always tried first; empty means
    # single-port (unchanged behaviour). Declared last so the field order of the
    # existing parameters is unchanged.
    ports: tuple[int, ...] = ()
    # Transport selector: ``auto`` races QUIC/UDP (preferred) and TLS/TCP with
    # the RFC 8305 stagger, ``quic``/``tcp`` use one transport only. Declared
    # after the existing fields so positional construction keeps working.
    transport: str = RDV_TRANSPORT_DEFAULT
    # Mutable holder for the last port on which an exchange succeeded (frozen
    # dataclass, so the element is mutated in place). Tried first so a client
    # behind a UDP-port-blocking egress firewall does not re-pay the QUIC idle
    # timeout on every exchange. Excluded from eq/repr.
    _preferred: list[int] = field(default_factory=list, compare=False, repr=False)
    # Memoized ``(transport, port, expires_at)`` of the last successful dial
    # (per endpoint). A QUIC winner never expires; a TLS/TCP winner expires
    # after ``RDV_TCP_FALLBACK_TTL_S`` so QUIC regains priority on a network
    # that restored UDP. ``None`` = no memo (run the full race). Excluded from
    # eq/repr.
    _memo: tuple[str, int, float] | None = field(default=None, compare=False, repr=False)

    def _candidate_ports(self) -> tuple[int, ...]:
        """Ordered ports to dial: the last working port first, then the rest."""
        return tuple(dict.fromkeys((*self._preferred, self.port, *self.ports)))

    def reset_transport_memo(self) -> None:
        """Forget the memoized transport/port (network change resets the race).

        Called by the rendezvous plan when the network signature changes: a new
        network is a new reachability landscape, so QUIC gets its fair chance
        again even when the previous network only reached the node over TCP.
        """
        object.__setattr__(self, "_memo", None)

    def _memoized(self, now: float | None = None) -> tuple[str, int] | None:
        """The memoized ``(transport, port)`` when still valid, else ``None``.

        A QUIC winner never expires (``float('inf')``); a TLS/TCP winner expires
        after ``RDV_TCP_FALLBACK_TTL_S`` so QUIC regains priority on a network
        whose UDP path recovered. An expired memo is simply not used — the full
        race reruns and refreshes it.
        """
        memo = self._memo
        if memo is None:
            return None
        transport, port, expires = memo
        if transport == "quic":
            return (transport, port)
        now = time.monotonic() if now is None else now
        return (transport, port) if now < expires else None

    def _remember(self, transport: str, port: int) -> None:
        """Remember the winning ``(transport, port)`` for the next exchange."""
        self._preferred[:] = [port]
        expires = float("inf") if transport == "quic" else time.monotonic() + RDV_TCP_FALLBACK_TTL_S
        object.__setattr__(self, "_memo", (transport, port, expires))

    def _tls_context(self) -> ssl.SSLContext:
        """Client TLS 1.3 context for the TLS/TCP transport (ALPN pinned).

        The rendezvous is self-signed by contract, so WebPKI can never validate
        it: trust comes from the pinned/prompted fingerprint (or ``none``
        disables verification entirely). ``check_hostname=False`` is set
        **before** ``verify_mode = CERT_NONE`` (the reverse order raises), and
        the peer certificate is still read afterwards for fingerprint
        verification. TLS 1.2 and below are refused.
        """
        context = unverified_ssl_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.set_alpn_protocols([RDV_ALPN])
        return context

    def candidate_ports(self) -> tuple[int, ...]:
        """Ordered ports to try for this node (multi-port egress fallback)."""
        return self._candidate_ports()

    def resolved_relay_ip(self, port: int | None = None) -> str:
        """Resolve the node host once (the IP relay attach connections pin to).

        A relay listener opens one control connection plus one connection per
        ``relay_attach``; with several A/AAAA records an attach dialing the
        hostname could reach another node instance (which does not hold the
        pending ``relay_id``) or another address family (failing the node's
        ``peer_ip`` check). Resolving once and pinning every relay connection to
        the same IP makes all of them reach the same node instance. The address
        family is whatever the resolver returns first (IPv4 or IPv6), so an
        IPv6-only node is not forced to a failing IPv4 lookup.
        """
        import socket

        port = self.port if port is None else port
        infos = socket.getaddrinfo(
            self.host,
            port,
            type=socket.SOCK_STREAM,
        )
        # ``getaddrinfo`` returns an IPv6 sockaddr as a 4-tuple; only the address
        # is used here.
        address = infos[0][4][0]
        return str(address)

    async def _open_rdv_tls_stream(
        self, ip: str, port: int, context: ssl.SSLContext
    ) -> tuple[Any, Any, bytes | None]:
        """Open one TLS/TCP stream, enforce ALPN and return the peer certificate DER.

        The node is self-signed by contract, so the fingerprint is verified by
        the caller on the returned DER before the first write. ALPN is enforced
        client-side because Python's client handshake does not abort on no
        overlap; parity with the node's server-side check.
        """
        reader, writer = await asyncio.open_connection(
            ip, port, ssl=context, server_hostname=self.host
        )
        ssl_object = writer.get_extra_info("ssl_object")
        selected = ssl_object.selected_alpn_protocol() if ssl_object is not None else None
        if selected != RDV_ALPN:
            writer.close()
            raise ConnectionError(
                f"rendezvous {format_authority(self.host, port)}: node did not select hsync-rdv/1"
            )
        cert_der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
        return reader, writer, cert_der

    async def dial_relay_tcp(self, port: int, *, ip: str) -> tuple[Any, Any, bytes | None]:
        """Dial one TLS/TCP relay connection (one connection = one session).

        Returns ``(reader, writer, peer_cert_der)`` with the peer certificate in
        DER form; the caller (``rdv_relay``) runs :meth:`verify_peer_der` on it
        before the first write. ALPN is enforced as in ``_dial_tcp``.
        """
        return await self._open_rdv_tls_stream(ip, port, self._tls_context())

    def register(self, frame: RdvRegister) -> RdvAck | None:
        """Send ``register``; return the ``ack`` (STUN-echo) or ``None`` if dropped."""
        return self._ack_exchange(frame)

    def refresh(self, frame: RdvRefresh) -> RdvAck | None:
        """Renew the TTL; return the ``ack`` or ``None`` if the node dropped it."""
        return self._ack_exchange(frame)

    def leave(self, frame: RdvLeave) -> None:
        """Announce departure. ``leave`` has no response frame."""
        self._exchange(frame, expect=None)

    def query(self, frame: RdvQuery) -> RdvResponse | None:
        """Ask for a random peer sample; return the ``response`` or ``None``."""
        raw = self._exchange(frame, expect="response")
        if raw is None:
            return None
        parsed = validate_rdv_response(raw)
        if parsed is None:
            raise RendezvousProtocolError("malformed response frame")
        return parsed

    def fetch_fingerprint(self) -> str:
        """Return the peer certificate fingerprint without trusting or writing.

        Connects, reads the certificate during the handshake and closes without
        sending any frame (like ``ssh-keyscan``). Used by
        ``hsync trust rendezvous-server fingerprint`` so an operator can verify a node out
        of band before pinning it.
        """
        try:
            return asyncio.run(self._fetch_fingerprint_async())
        except RendezvousError:
            raise
        except OSError as exc:
            raise _unreachable(self.host, self.port, exc) from exc

    async def _with_connection(
        self, action: Callable[[_Dial], Awaitable[_T]], *, what: str = "reply"
    ) -> _T:
        """Select a transport, dial it, verify the winner once and run *action*.

        ``transport`` selects the behaviour:

        ``quic``/``tcp``
            dial only that transport, candidate ports in order, no race. Only a
            failed **connection** (handshake, ALPN mismatch, timeout) falls back
            to the next candidate port: that is the client-egress-firewall case,
            where one port is blocked but another reaches the SAME node. Once a
            handshake succeeds the port is reachable by definition, so an error
            or timeout raised by *action* is a real exchange failure and is
            propagated without re-dialling (no duplicate frame is sent).

        ``auto``
            a memoized ``(transport, port)`` is tried alone first (the previous
            winner gets one solo attempt; a connection-phase failure clears the
            memo); otherwise the full race runs: QUIC starts first, TLS/TCP
            after ``RDV_TRANSPORT_STAGGER_S`` (RFC 8305), the first attempt to
            complete its handshake with the right ALPN wins, the loser is
            cancelled and closed before any frame. Both failing advances to the
            next candidate port.

        Trust is verified once, on the winner, before the first write; a
        trust/protocol failure is never a fallback signal either, because
        :class:`RendezvousError` is not ``OSError``/``TimeoutError``.
        """
        selector = self.transport
        if selector in ("quic", "tcp"):
            dialed = await self._dial_transport_ports(selector, what=what)
        else:
            dialed = await self._dial_auto(what=what)
        return await self._run_on(dialed, action, what=what)

    async def _dial_transport_ports(self, transport: str, *, what: str) -> _Dial:
        """Dial one transport across the candidate ports (no race)."""
        last_exc: BaseException | None = None
        failed_port: int | None = None
        for port in self._candidate_ports():
            try:
                return await self._dial(transport, port)
            except (OSError, TimeoutError) as exc:
                last_exc = exc
                failed_port = port
                continue
        raise self._connection_failure(last_exc, failed_port, what=what)

    async def _dial_auto(self, *, what: str) -> _Dial:
        """Dial with the memo-then-race policy of ``auto``."""
        memoized = self._memoized()
        if memoized is not None:
            transport, port = memoized
            try:
                return await self._dial(transport, port)
            except (OSError, TimeoutError):
                # A memoized transport that fails at connection level is cleared
                # and the full race runs within the same exchange.
                object.__setattr__(self, "_memo", None)
        last_exc: BaseException | None = None
        failed_port: int | None = None
        for port in self._candidate_ports():
            try:
                return await self._race(port)
            except (OSError, TimeoutError) as exc:
                last_exc = exc
                failed_port = port
                continue
        raise self._connection_failure(last_exc, failed_port, what=what)

    def _connection_failure(
        self, exc: BaseException | None, port: int | None, *, what: str
    ) -> BaseException:
        """Map the last connection-phase failure to the public error shape."""
        if exc is None:  # defensive: ``candidates`` always holds ``self.port``
            return RendezvousUnreachable(
                f"rendezvous {format_authority(self.host, self.port)}: no reachable endpoint"
            )
        if isinstance(exc, TimeoutError):
            port = port if port is not None else self.port
            return RendezvousUnreachable(
                f"rendezvous {format_authority(self.host, port)}: timed out waiting for a {what}"
            )
        return exc

    async def _race(self, port: int) -> _Dial:
        """RFC 8305 race on one port: QUIC first, TLS/TCP after the stagger.

        The first attempt to complete its handshake (with the right ALPN) wins.
        Every other attempt is stopped before any frame is sent: one still
        running is cancelled, one that completed but lost the race is closed
        (it holds an open transport too, and nothing else owns it). A QUIC that
        completed within the stagger window wins immediately (QUIC is the
        preferred transport); otherwise TCP starts, and the first to finish wins.
        """
        spawned: set[asyncio.Task[_Dial]] = set()
        quic_task = asyncio.create_task(self._dial("quic", port))
        spawned.add(quic_task)
        try:
            done, _ = await asyncio.wait(
                {quic_task}, timeout=RDV_TRANSPORT_STAGGER_S, return_when=asyncio.FIRST_COMPLETED
            )
            if quic_task in done and not quic_task.cancelled() and quic_task.exception() is None:
                return quic_task.result()
            tcp_task = asyncio.create_task(self._dial("tcp", port))
            spawned.add(tcp_task)
            winner: asyncio.Task[_Dial] | None = None
            waiting = {quic_task, tcp_task}
            while waiting and winner is None:
                done, waiting = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if not task.cancelled() and task.exception() is None:
                        winner = task
                        break
            # The race is decided: stop every attempt still running.
            for task in waiting:
                task.cancel()
            if waiting:
                await asyncio.gather(*waiting, return_exceptions=True)
            if winner is not None:
                # A losing attempt that already completed holds an open
                # transport (QUIC manager / TCP writer): close it, or the
                # connection leaks even though its frames were never used.
                await _close_unused_dials(spawned, winner)
                return winner.result()
            # Every attempt on this port failed at the connection phase.
            first: BaseException | None = None
            for task in (quic_task, tcp_task):
                if task.done() and not task.cancelled() and task.exception() is not None:
                    first = task.exception()
                    break
            if first is not None:
                raise first
            raise ConnectionError(f"rendezvous {format_authority(self.host, port)}: dial failed")
        finally:
            # Cancellation or an unexpected error can leave attempts running;
            # stop them here so no dial outlives the race.
            outstanding = [task for task in spawned if not task.done()]
            for task in outstanding:
                task.cancel()
            if outstanding:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*outstanding, return_exceptions=True)

    async def _run_on(
        self, dial: _Dial, action: Callable[[_Dial], Awaitable[_T]], *, what: str
    ) -> _T:
        """Run *action* on the winner, verifying the peer once, pre-write."""
        try:
            # Trust is verified once, on the winner, before the first write (the
            # loser was closed before any frame). A refusal here is fatal and
            # never falls back (RendezvousError is not OSError/TimeoutError).
            self.verify_peer_der(dial.peer_cert_der)
            result = await action(dial)
        except TimeoutError as exc:
            raise RendezvousUnreachable(
                f"rendezvous {format_authority(self.host, dial.port)}: "
                f"timed out waiting for a {what}"
            ) from exc
        finally:
            await dial.close()
        self._remember(dial.transport, dial.port)
        return result

    async def _dial(self, transport: str, port: int) -> _Dial:
        """Dial one transport on one port; connection-phase failures propagate."""
        tls = self._tls_context()
        if transport == "tcp":
            return await self._dial_tcp(port, tls)
        return await self._dial_quic(port)

    async def _dial_quic(self, port: int) -> _Dial:
        """Open one QUIC connection plus a fresh stream (the session's stream)."""
        manager = connect(
            self.host,
            port,
            configuration=self._quic_configuration(),
            wait_connected=True,
        )
        client = await manager.__aenter__()
        try:
            reader, writer = await client.create_stream()
            cert = _peer_certificate(client)
        except BaseException:
            await manager.__aexit__(*sys.exc_info())
            raise
        cert_der = None if cert is None else cert.public_bytes(serialization.Encoding.DER)

        async def _close() -> None:
            try:
                is_closing = getattr(writer, "is_closing", lambda: False)
                if not is_closing():
                    writer.close()
            except Exception:  # noqa: BLE001 - teardown must never raise
                pass
            finally:
                # aioquic's context exit transmits CONNECTION_CLOSE and then
                # blocks on ``wait_closed()`` until the peer terminates the
                # connection (a draining timer / round trip: ~95 ms on a
                # loopback here). The close datagram is already queued, so the
                # wait delays only this caller — and ``rdv/1`` opens a fresh
                # connection per exchange, so it is paid every time. Mark the
                # connection terminated to skip the wait; if a future aioquic
                # renames the attribute, fall back to the normal graceful exit.
                closed = getattr(client, "_closed", None)
                if isinstance(closed, asyncio.Event):
                    closed.set()
                await manager.__aexit__(None, None, None)

        return _Dial(
            transport="quic",
            port=port,
            reader=reader,
            writer=writer,
            peer_cert_der=cert_der,
            _closer=_close,
        )

    async def _dial_tcp(self, port: int, context: ssl.SSLContext) -> _Dial:
        """Open one TLS/TCP connection (one connection = one ``rdv/1`` session)."""
        reader, writer, cert_der = await self._open_rdv_tls_stream(self.host, port, context)

        async def _close() -> None:
            try:
                is_closing = getattr(writer, "is_closing", lambda: False)
                if not is_closing():
                    writer.close()
                # Release the socket now instead of at GC: the client opens one
                # TLS/TCP connection per exchange, so an unawaited close leaks a
                # descriptor per exchange under sustained load.
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - teardown must never raise
                pass

        return _Dial(
            transport="tcp",
            port=port,
            reader=reader,
            writer=writer,
            peer_cert_der=cert_der,
            _closer=_close,
        )

    async def _fetch_fingerprint_async(self) -> str:
        async def _read(dial: _Dial) -> str:
            if dial.peer_cert_der is None:
                raise RendezvousFingerprintMismatch(
                    f"rendezvous {format_authority(self.host, self.port)}: "
                    "could not read the peer certificate"
                )
            return fingerprint_from_der(dial.peer_cert_der)

        return await self._with_connection(_read)

    def _ack_exchange(self, frame: RdvRegister | RdvRefresh) -> RdvAck | None:
        raw = self._exchange(frame, expect="ack", refusal=True)
        if raw is None:
            return None
        if raw["type"] == "response":
            # A uniform refusal (token / rate limit / capacity) is a valid rdv/1
            # answer to register/refresh — surface it instead of a contract error.
            raise RendezvousRefused(raw["error"], raw.get("retry_after"))
        parsed = validate_rdv_ack(raw)
        if parsed is None:
            raise RendezvousProtocolError("malformed ack frame")
        return parsed

    def _exchange(
        self,
        frame: Mapping[str, Any],
        *,
        expect: str | None,
        refusal: bool = False,
    ) -> dict | None:
        """Run one request/response session on a fresh QUIC stream.

        ``refusal`` accepts the uniform ``response`` refusal frame the node uses
        for token-refused / rate-limited / overloaded ``register``/``refresh``
        (the caller maps it to :class:`RendezvousRefused`); any other unexpected
        reply type is still a contract violation.
        """
        validated = validate_rdv_frame(frame)
        if validated is None:
            raise RendezvousProtocolError(
                f"refusing to send an invalid {frame.get('type')!r} frame"
            )
        payload = compact_json_bytes(validated)
        try:
            raw = asyncio.run(self._exchange_async(payload, expect=expect))
        except RendezvousError:
            raise
        except OSError as exc:
            # ConnectionError/TimeoutError/ssl.SSLError are all OSError subclasses;
            # aioquic's ConnectionError often carries no text, hence _unreachable.
            raise _unreachable(self.host, self.port, exc) from exc
        return self._check_reply(raw, expect=expect, refusal=refusal)

    @staticmethod
    def _check_reply(raw: dict | None, *, expect: str | None, refusal: bool) -> dict | None:
        """Enforce the reply contract of one exchange (pure, no network).

        An unexpected reply type is a contract violation, with one exception: a
        *validated* uniform refusal ``response``, which ``rdv/1`` also uses to
        answer a rate-limited / token-refused ``register``/``refresh`` (the
        caller maps it to :class:`RendezvousRefused`). A malformed ``response`` is
        still rejected.
        """
        if raw is None:
            return None
        reply_type = raw.get("type")
        if reply_type != expect:
            refusal_frame = (
                validate_rdv_response(raw) if refusal and reply_type == "response" else None
            )
            if refusal_frame is None or "error" not in refusal_frame:
                raise RendezvousProtocolError(f"expected a {expect!r} frame, got {reply_type!r}")
            return refusal_frame
        return raw

    async def _exchange_async(self, payload: bytes, *, expect: str | None) -> dict | None:
        async def _run(dial: _Dial) -> dict | None:
            writer = dial.writer
            writer.write(payload + b"\n")
            await writer.drain()
            if expect is None:
                # e.g. ``leave``: the spec defines no response frame, so the
                # server keeps the stream open and never writes. Do not wait
                # for a reply that will not come.
                return None
            line = await asyncio.wait_for(_read_line(dial.reader), timeout=self.timeout)
            if line is None:
                return None
            try:
                decoded = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RendezvousProtocolError("reply is not valid JSON") from exc
            if not isinstance(decoded, dict):
                raise RendezvousProtocolError("reply is not a JSON object")
            return decoded

        return await self._with_connection(_run, what=expect or "reply")

    def _quic_configuration(self) -> QuicConfiguration:
        configuration = QuicConfiguration(
            is_client=True,
            alpn_protocols=[RDV_ALPN],
            idle_timeout=self.idle_timeout,
        )
        # The rendezvous is self-signed by contract, so WebPKI can never validate
        # it: trust comes from the pinned/prompted fingerprint (or ``none``
        # disables verification entirely). The handshake therefore never rejects
        # the certificate before we can read it in ``_verify_peer``.
        configuration.verify_mode = ssl.CERT_NONE
        return configuration

    def relay_configuration(self) -> QuicConfiguration:
        """QUIC/TLS configuration for a long-lived **relay** connection.

        Same trust model as rendezvous; only the idle timeout differs, because a
        relay connection carries a whole sync session (the effective value is
        still the minimum of the two peers' advertised timeouts).
        """
        configuration = self._quic_configuration()
        configuration.idle_timeout = RELAY_IDLE_TIMEOUT_S
        return configuration

    def verify_peer(self, client: Any) -> None:
        """Public entry point for a QUIC relay connection (aioquic client)."""
        cert = _peer_certificate(client)
        cert_der = None if cert is None else cert.public_bytes(serialization.Encoding.DER)
        self.verify_peer_der(cert_der)

    def verify_peer_der(self, peer_cert_der: bytes | None) -> None:
        """Apply the trust policy against the peer fingerprint, pre-write.

        Runs after the handshake and before any frame, so a refusal closes the
        connection with zero application bytes sent (the ``bucket_token`` never
        leaves the host). ``accept_new`` records a first sighting; ``ask`` calls
        ``prompt`` and records on acceptance; a changed fingerprint is always
        fatal. ``peer_cert_der`` is the node's certificate in DER form (QUIC
        exposes it through aioquic's TLS state; TCP through
        ``ssl``'s ``getpeercert(binary_form=True)``), so both transports verify
        through one path.
        """
        policy = self.policy
        if policy is TrustPolicy.none:
            return
        if peer_cert_der is None:
            raise RendezvousFingerprintMismatch(
                f"rendezvous {format_authority(self.host, self.port)}: could not read the peer "
                "certificate to verify its fingerprint"
            )
        observed = fingerprint_from_der(peer_cert_der)
        store = default_rendezvous_trust_store()

        # Explicit pins (``--rendezvous-server-fingerprint``) are accepted directly and are
        # the source of truth for a ``strict`` run; a non-match falls through so
        # ``strict`` refuses and ``ask`` prompts.
        if self.fingerprints and fingerprint_matches(observed, list(self.fingerprints)):
            return

        stored = store.get(self.host, self.port)
        action = evaluate_tls_trust(policy, stored, observed)
        if action is TrustAction.proceed:
            return
        if action is TrustAction.pin_and_proceed:
            store.set(self.host, self.port, observed, source="tofu")
            self._announce_known_host(store)
            return
        if action is TrustAction.prompt:
            self._prompt_and_pin(store, observed)
            return
        # refuse: unknown under strict, or a changed fingerprint
        raise self._mismatch(observed, stored=stored, store=store)

    def _prompt_and_pin(self, store: RendezvousTrustStore, observed: str) -> None:
        """Run the interactive trust prompt; pin on acceptance, refuse otherwise."""
        if self.prompt is None:
            raise RendezvousNotTrusted(
                f"rendezvous {format_authority(self.host, self.port)}: certificate fingerprint {observed} "
                "is not trusted and there is no terminal to confirm it; pass "
                "--rendezvous-server-tofu (accept new) or --rendezvous-server-fingerprint <fp> to trust it",
                non_interactive=True,
            )
        if self.prompt(self.host, self.port, observed):
            store.set(self.host, self.port, observed, source="prompt")
            self._announce_known_host(store)
            return
        raise RendezvousNotTrusted(
            f"rendezvous {format_authority(self.host, self.port)}: certificate fingerprint {observed} "
            "was not accepted; nothing was saved. Pass --rendezvous-server-tofu to trust the "
            "first sighting or --rendezvous-server-fingerprint <fp> to pin it"
        )

    def _announce_known_host(self, store: RendezvousTrustStore) -> None:
        """Report a just-pinned fingerprint together with the store that holds it."""
        log.info(known_hosts_added_message(self.host, self.port, store.path))

    def _mismatch(
        self,
        observed: str,
        *,
        stored: str | None,
        store: RendezvousTrustStore,
    ) -> RendezvousFingerprintMismatch:
        """Build the fatal refusal: concise reason plus SSH-style recovery guidance.

        The structured event mirrors the master path: a node that is not pinned
        under ``strict`` emits ``pairing_needed`` (an explicit trust confirmation
        is required), while a changed pin or an explicit-pin mismatch emits
        ``fingerprint_changed``.
        """
        if stored is not None:
            reason = (
                f"rendezvous {format_authority(self.host, self.port)}: certificate fingerprint {observed} "
                "does not match the pinned fingerprint (possible man-in-the-middle)"
            )
        elif self.fingerprints:
            reason = (
                f"rendezvous {format_authority(self.host, self.port)}: certificate fingerprint {observed} "
                "does not match any fingerprint passed with --rendezvous-server-fingerprint"
            )
        else:
            reason = (
                f"rendezvous {format_authority(self.host, self.port)}: certificate fingerprint {observed} "
                "is not pinned and the trust policy is 'strict'"
            )
        if stored is None and not self.fingerprints:
            emit_event(
                UiEventName.pairing_needed,
                scope="rendezvous",
                peer_address=f"{format_authority(self.host, self.port)}",
                fingerprint=observed,
                message=reason,
            )
        else:
            emit_event(
                UiEventName.fingerprint_changed,
                scope="rendezvous",
                peer_address=f"{format_authority(self.host, self.port)}",
                fingerprint=observed,
                message=reason,
            )
        return RendezvousFingerprintMismatch(
            reason,
            guidance=mismatch_guidance(
                self.host,
                self.port,
                observed,
                stored=stored,
                store_path=store.path,
                explicit_pins=bool(self.fingerprints),
            ),
        )


def _peer_certificate(protocol: Any) -> Any:
    """Read the peer certificate from a connected aioquic client.

    aioquic 1.3.0 keeps the verified peer certificate on the internal TLS state
    and exposes no public accessor. Isolated here (R-01) so an aioquic upgrade
    only requires changing this one spot; returns ``None`` when the certificate
    is unavailable and the caller fails closed. Access is defensive
    (``getattr``) so fake/stub clients in tests keep working.
    """
    quic = getattr(protocol, "_quic", None)
    tls = getattr(quic, "tls", None)
    return getattr(tls, "_peer_certificate", None)


async def _read_line(reader: Any) -> bytes | None:
    """Read one newline-terminated frame; ``None`` on a clean EOF."""
    line = await reader.readline()
    if not line:
        return None
    return bytes(line).rstrip(b"\n")


def _apply_optional(
    frame: Any,
    *,
    group: str | None = None,
    bucket_token: str | None = None,
    connected: int | None = None,
    limit: int | None = None,
) -> None:
    """Copy the optional wire fields that are present (non-``None``) onto *frame*.

    Single point for the "omit when None" rule shared by every builder, so a new
    optional field is wired in one place.
    """
    if group is not None:
        frame["group"] = group
    if bucket_token is not None:
        frame["bucket_token"] = bucket_token
    if connected is not None:
        frame["connected"] = connected
    if limit is not None:
        frame["limit"] = limit


def build_register_frame(
    *,
    cid: str,
    bucket: str,
    cert_fp: str,
    version: str,
    group: str | None = None,
    bucket_token: str | None = None,
    capacity: int = 0,
    endpoints: list[dict[str, Any]] | None = None,
    connected: int | None = None,
    server_ready: bool = False,
    prefer_master: bool = False,
    never_master: bool = False,
    inbound: bool = False,
    startup_time: float = 0.0,
    sync_direction: str = "bidirectional",
) -> RdvRegister:
    """Build a ``register`` frame (validated against the mirror before send).

    ``server_ready``/``prefer_master``/``never_master``/``inbound``/
    ``startup_time``/``sync_direction`` are the election advertisement the node
    relays verbatim in its ``query`` peers (the same values the mDNS TXT record
    carries), so a rendezvous-discovered peer ranks in the master election
    exactly like a LAN-discovered one.
    """
    frame = RdvRegister(
        protocol_version=PROTOCOL_VERSION_RDV,
        type="register",
        cid=cid,
        bucket=bucket,
        cert_fp=cert_fp,
        version=version,
        capacity=capacity,
        endpoints=list(endpoints or []),
        server_ready=server_ready,
        prefer_master=prefer_master,
        never_master=never_master,
        inbound=inbound,
        startup_time=startup_time,
        sync_direction=sync_direction,
    )
    _apply_optional(frame, group=group, bucket_token=bucket_token, connected=connected)
    return frame


def build_refresh_frame(
    *,
    cid: str,
    bucket: str,
    group: str | None = None,
    bucket_token: str | None = None,
    connected: int | None = None,
) -> RdvRefresh:
    """Build a ``refresh`` frame (TTL + ``connected`` only; no endpoints)."""
    frame = RdvRefresh(
        protocol_version=PROTOCOL_VERSION_RDV,
        type="refresh",
        cid=cid,
        bucket=bucket,
    )
    _apply_optional(frame, group=group, bucket_token=bucket_token, connected=connected)
    return frame


def build_leave_frame(
    *,
    cid: str,
    bucket: str,
    bucket_token: str | None = None,
) -> RdvLeave:
    """Build a ``leave`` frame."""
    frame = RdvLeave(
        protocol_version=PROTOCOL_VERSION_RDV,
        type="leave",
        cid=cid,
        bucket=bucket,
    )
    _apply_optional(frame, bucket_token=bucket_token)
    return frame


def build_query_frame(
    *,
    bucket: str,
    group: str | None = None,
    bucket_token: str | None = None,
    limit: int | None = None,
) -> RdvQuery:
    """Build a ``query`` frame (generic sample request; no file targeting)."""
    frame = RdvQuery(
        protocol_version=PROTOCOL_VERSION_RDV,
        type="query",
        bucket=bucket,
    )
    _apply_optional(frame, group=group, bucket_token=bucket_token, limit=limit)
    return frame


def build_relay_listen_frame(
    *,
    cid: str,
    bucket: str,
    group: str | None = None,
    bucket_token: str | None = None,
) -> RdvRelayListen:
    """Build a ``relay_listen`` frame (the serving peer's control stream)."""
    frame = RdvRelayListen(
        protocol_version=PROTOCOL_VERSION_RDV,
        type="relay_listen",
        cid=cid,
        bucket=bucket,
    )
    _apply_optional(frame, group=group, bucket_token=bucket_token)
    return frame


def build_relay_ping_frame(*, cid: str, bucket: str) -> RdvRelayPing:
    """Build a ``relay_ping`` keepalive (control stream, no reply)."""
    return RdvRelayPing(
        protocol_version=PROTOCOL_VERSION_RDV,
        type="relay_ping",
        cid=cid,
        bucket=bucket,
    )


def build_relay_connect_frame(
    *,
    cid: str,
    bucket: str,
    target_cid: str,
    group: str | None = None,
    bucket_token: str | None = None,
) -> RdvRelayConnect:
    """Build a ``relay_connect`` frame (requester side of one session)."""
    frame = RdvRelayConnect(
        protocol_version=PROTOCOL_VERSION_RDV,
        type="relay_connect",
        cid=cid,
        bucket=bucket,
        target_cid=target_cid,
    )
    _apply_optional(frame, group=group, bucket_token=bucket_token)
    return frame


def build_relay_attach_frame(
    *,
    cid: str,
    bucket: str,
    relay_id: str,
    bucket_token: str | None = None,
) -> RdvRelayAttach:
    """Build a ``relay_attach`` frame (serving peer's per-session stream)."""
    frame = RdvRelayAttach(
        protocol_version=PROTOCOL_VERSION_RDV,
        type="relay_attach",
        cid=cid,
        bucket=bucket,
        relay_id=relay_id,
    )
    _apply_optional(frame, bucket_token=bucket_token)
    return frame


def frame_line(frame: Mapping[str, Any]) -> bytes:
    """Serialize one validated ``rdv/1`` frame as an NDJSON line.

    Validation runs before serialization, so a builder bug fails here instead of
    making the node drop the frame silently (unknown fields are fatal on the
    wire). Relay data streams reuse this for their single framed line; every
    following byte on those streams is opaque TLS.
    """
    validated = validate_rdv_frame(frame)
    if validated is None:
        raise RendezvousProtocolError(f"refusing to send an invalid {frame.get('type')!r} frame")
    return compact_json_bytes(validated) + b"\n"
