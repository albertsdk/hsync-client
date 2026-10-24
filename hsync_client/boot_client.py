# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Client for the ``boot/1`` bootstrap protocol (QUIC via aioquic).

Wire contract (SSOT: ``hsync-bootnode/protocol-spec/boot-1.md``, mirrored and
validated in ``hsync_common.protocol.bootnode``): one QUIC/TLS 1.3 connection
with ALPN ``hsync-boot/1``; every bidirectional stream is one session; frames
are newline-terminated JSON (NDJSON). A request writes one frame and reads the
single response line (or ``None`` when the node silently drops it).

This module exposes a synchronous API (the client is thread-based) around a
private asyncio loop, and keeps the framing/validation here so the discovery
plan (``p2p_discovery_bootnode``) only deals with peer state. It never treats a
response as authoritative beyond the wire contract.

Consumers: ``hsync-client`` (p2p_discovery_bootnode), tests.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from aioquic.asyncio.client import connect
from aioquic.quic.configuration import QuicConfiguration
from hsync_common.endpoint import format_authority
from hsync_common.protocol.bootnode import (
    BOOT_ALPN,
    PROTOCOL_VERSION_BOOT,
    BootAck,
    BootLeave,
    BootQuery,
    BootRefresh,
    BootRegister,
    BootResponse,
    validate_boot_ack,
    validate_boot_frame,
    validate_boot_response,
)
from hsync_common.protocol.pairing import fingerprint_matches
from hsync_common.protocol.tls_trust import (
    TrustAction,
    TrustPolicy,
    evaluate_tls_trust,
)
from hsync_common.ssl_utils import compute_cert_fingerprint
from hsync_common.text import compact_json_bytes
from loguru import logger as log

from hsync_client.bootnode_trust import BootnodeTrustStore, default_bootnode_trust_store
from hsync_client.events import UiEventName, emit_event
from hsync_client.trust_messages import known_hosts_added_message, mismatch_guidance

__all__ = [
    "DEFAULT_QUIC_IDLE_TIMEOUT",
    "BootnodeClient",
    "BootnodeError",
    "BootnodeFingerprintMismatch",
    "BootnodeNotTrusted",
    "BootnodeProtocolError",
    "BootnodeRefused",
    "BootnodeUnreachable",
    "TrustPrompt",
    "build_leave_frame",
    "build_query_frame",
    "build_refresh_frame",
    "build_register_frame",
]

# QUIC idle timeout: a single request/response exchange is well under a second
# on any usable link; the timeout bounds a stalled handshake, not throughput.
DEFAULT_QUIC_IDLE_TIMEOUT = 30.0

# SSH-style trust prompt: ``(host, port, observed_fingerprint) -> accept?``.
# Supplied by the front-end (terminal for the CLI; the desktop confirms with a
# pre-flight instead). ``None`` means the caller cannot prompt (non-interactive),
# so policy ``ask`` must fail closed.
TrustPrompt = Callable[[str, int, str], bool]


class BootnodeError(Exception):
    """Base class for bootnode client failures."""


class BootnodeUnreachable(BootnodeError):
    """The bootnode could not be reached (DNS, timeout, TLS, connection)."""


class BootnodeProtocolError(BootnodeError):
    """The bootnode answered with something that violates ``boot/1``."""


class BootnodeRefused(BootnodeError):
    """The node answered ``register``/``refresh`` with a uniform refusal frame.

    ``boot/1`` replies to a rate-limited, token-refused or capacity-refused
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


class BootnodeFingerprintMismatch(BootnodeError):
    """The bootnode certificate fingerprint did not match the accepted pin(s).

    Fatal: the connection is closed before any frame is sent, and the discovery
    lease loop must not retry it. ``str`` keeps the concise case-specific reason
    (changed pin, node not pinned under ``strict``, no explicit-pin match), while
    ``guidance`` carries the SSH-style recovery block the front-end logs once:
    the presented fingerprint, the known-hosts file and the exact
    ``hsync trust bootnode`` commands that unblock the situation.
    """

    def __init__(self, message: str, *, guidance: str | None = None) -> None:
        super().__init__(message)
        self.guidance = guidance


class BootnodeNotTrusted(BootnodeError):
    """Policy ``ask`` could not establish trust: the user declined or cannot prompt.

    Fatal, like :class:`BootnodeFingerprintMismatch`: the node is not contacted,
    the lease loop stops and the sync core exits non-zero. ``non_interactive``
    distinguishes "no terminal to ask" (fail closed) from an explicit decline.
    """

    def __init__(self, message: str, *, non_interactive: bool = False) -> None:
        super().__init__(message)
        self.non_interactive = non_interactive


def _unreachable(host: str, port: int, exc: BaseException) -> BootnodeUnreachable:
    """Build the unreachable error; aioquic's ``ConnectionError`` has no text."""
    detail = str(exc) or type(exc).__name__
    return BootnodeUnreachable(f"bootnode {format_authority(host, port)}: {detail}")


_T = TypeVar("_T")


@dataclass(frozen=True)
class BootnodeClient:
    """Synchronous ``boot/1`` client for one bootnode.

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
    # another (BOOT_PORT_SET). ``port`` is always tried first; empty means
    # single-port (unchanged behaviour). Declared last so the field order of the
    # existing parameters is unchanged.
    ports: tuple[int, ...] = ()
    # Mutable holder for the last port on which an exchange succeeded (frozen
    # dataclass, so the element is mutated in place). Tried first so a client
    # behind a UDP-port-blocking egress firewall does not re-pay the QUIC idle
    # timeout on every exchange. Excluded from eq/repr.
    _preferred: list[int] = field(default_factory=list, compare=False, repr=False)

    def _candidate_ports(self) -> tuple[int, ...]:
        """Ordered ports to dial: the last working port first, then the rest."""
        seen: list[int] = []
        for candidate in (*self._preferred, self.port, *self.ports):
            if candidate not in seen:
                seen.append(candidate)
        return tuple(seen)

    def register(self, frame: BootRegister) -> BootAck | None:
        """Send ``register``; return the ``ack`` (STUN-echo) or ``None`` if dropped."""
        return self._ack_exchange(frame)

    def refresh(self, frame: BootRefresh) -> BootAck | None:
        """Renew the TTL; return the ``ack`` or ``None`` if the node dropped it."""
        return self._ack_exchange(frame)

    def leave(self, frame: BootLeave) -> None:
        """Announce departure. ``leave`` has no response frame."""
        self._exchange(frame, expect=None)

    def query(self, frame: BootQuery) -> BootResponse | None:
        """Ask for a random peer sample; return the ``response`` or ``None``."""
        raw = self._exchange(frame, expect="response")
        if raw is None:
            return None
        parsed = validate_boot_response(raw)
        if parsed is None:
            raise BootnodeProtocolError("malformed response frame")
        return parsed

    def fetch_fingerprint(self) -> str:
        """Return the peer certificate fingerprint without trusting or writing.

        Connects, reads the certificate during the handshake and closes without
        sending any frame (like ``ssh-keyscan``). Used by
        ``hsync trust bootnode fingerprint`` so an operator can verify a node out
        of band before pinning it.
        """
        try:
            return asyncio.run(self._fetch_fingerprint_async())
        except BootnodeError:
            raise
        except OSError as exc:
            raise _unreachable(self.host, self.port, exc) from exc

    async def _with_connection(
        self, action: Callable[[Any], Awaitable[_T]], *, what: str = "reply"
    ) -> _T:
        """Dial the first reachable candidate port and run *action* on it.

        Candidates are tried in order (the last working port first, then the
        configured set). Only a failed **connection** (handshake) falls back to
        the next candidate: that is the client-egress-firewall case, where one
        UDP port is blocked but another reaches the SAME node. Once the handshake
        succeeds the port is reachable by definition, so an error or timeout
        raised by *action* is a real exchange failure and is propagated without
        re-dialling (no duplicate frame is sent to an already-reachable node).

        The port on which *action* succeeds is remembered so a later exchange
        skips a blocked one; a trust/protocol failure is never a fallback signal
        either, because :class:`BootnodeError` is not ``OSError``/``TimeoutError``.
        """
        configuration = self._quic_configuration()
        candidates = self._candidate_ports()
        last_exc: BaseException | None = None
        failed_port: int | None = None
        for port in candidates:
            connected = False
            try:
                async with connect(
                    self.host,
                    port,
                    configuration=configuration,
                    wait_connected=True,
                ) as client:
                    connected = True
                    result = await action(client)
            except (OSError, TimeoutError) as exc:
                if connected:
                    # The handshake succeeded: the port is reachable, so this is
                    # a real exchange failure, not a blocked-port signal. Never
                    # fall back; keep the concise timeout message the caller had
                    # before multi-port (a bare TimeoutError would lose ``what``).
                    if isinstance(exc, TimeoutError):
                        raise BootnodeUnreachable(
                            f"bootnode {format_authority(self.host, port)}: "
                            f"timed out waiting for a {what}"
                        ) from exc
                    raise
                last_exc = exc
                failed_port = port
                continue
            self._preferred[:] = [port]
            return result
        # Every candidate failed at the connection phase.
        if last_exc is None:  # defensive: ``candidates`` always holds ``self.port``
            raise BootnodeUnreachable(
                f"bootnode {format_authority(self.host, self.port)}: no reachable port"
            )
        if isinstance(last_exc, TimeoutError):
            port = failed_port if failed_port is not None else self.port
            raise BootnodeUnreachable(
                f"bootnode {format_authority(self.host, port)}: timed out waiting for a {what}"
            ) from last_exc
        raise last_exc

    async def _fetch_fingerprint_async(self) -> str:
        async def _read(client: Any) -> str:
            cert = _peer_certificate(client)
            if cert is None:
                raise BootnodeFingerprintMismatch(
                    f"bootnode {format_authority(self.host, self.port)}: "
                    "could not read the peer certificate"
                )
            return compute_cert_fingerprint(cert)

        return await self._with_connection(_read)

    def _ack_exchange(self, frame: BootRegister | BootRefresh) -> BootAck | None:
        raw = self._exchange(frame, expect="ack", refusal=True)
        if raw is None:
            return None
        if raw["type"] == "response":
            # A uniform refusal (token / rate limit / capacity) is a valid boot/1
            # answer to register/refresh — surface it instead of a contract error.
            raise BootnodeRefused(raw["error"], raw.get("retry_after"))
        parsed = validate_boot_ack(raw)
        if parsed is None:
            raise BootnodeProtocolError("malformed ack frame")
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
        (the caller maps it to :class:`BootnodeRefused`); any other unexpected
        reply type is still a contract violation.
        """
        validated = validate_boot_frame(frame)
        if validated is None:
            raise BootnodeProtocolError(f"refusing to send an invalid {frame.get('type')!r} frame")
        payload = compact_json_bytes(validated)
        try:
            raw = asyncio.run(self._exchange_async(payload, expect=expect))
        except BootnodeError:
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
        *validated* uniform refusal ``response``, which ``boot/1`` also uses to
        answer a rate-limited / token-refused ``register``/``refresh`` (the
        caller maps it to :class:`BootnodeRefused`). A malformed ``response`` is
        still rejected.
        """
        if raw is None:
            return None
        reply_type = raw.get("type")
        if reply_type != expect:
            refusal_frame = (
                validate_boot_response(raw) if refusal and reply_type == "response" else None
            )
            if refusal_frame is None or "error" not in refusal_frame:
                raise BootnodeProtocolError(f"expected a {expect!r} frame, got {reply_type!r}")
            return refusal_frame
        return raw

    async def _exchange_async(self, payload: bytes, *, expect: str | None) -> dict | None:
        async def _run(client: Any) -> dict | None:
            # Verify the peer certificate *before* opening a stream: the
            # bucket_token travels in the frame, so trust is decided pre-write.
            self._verify_peer(client)
            reader, writer = await client.create_stream()
            writer.write(payload + b"\n")
            await writer.drain()
            if expect is None:
                # e.g. ``leave``: the spec defines no response frame, so the
                # server keeps the stream open and never writes. Do not wait
                # for a reply that will not come.
                writer.close()
                return None
            line = await asyncio.wait_for(_read_line(reader), timeout=self.timeout)
            writer.close()
            if line is None:
                return None
            try:
                decoded = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BootnodeProtocolError("reply is not valid JSON") from exc
            if not isinstance(decoded, dict):
                raise BootnodeProtocolError("reply is not a JSON object")
            return decoded

        return await self._with_connection(_run, what=expect or "reply")

    def _quic_configuration(self) -> QuicConfiguration:
        configuration = QuicConfiguration(
            is_client=True,
            alpn_protocols=[BOOT_ALPN],
            idle_timeout=self.idle_timeout,
        )
        # The bootnode is self-signed by contract, so WebPKI can never validate
        # it: trust comes from the pinned/prompted fingerprint (or ``none``
        # disables verification entirely). The handshake therefore never rejects
        # the certificate before we can read it in ``_verify_peer``.
        configuration.verify_mode = ssl.CERT_NONE
        return configuration

    def _verify_peer(self, client: Any) -> None:
        """Apply the trust policy against the peer fingerprint, pre-write.

        Runs after the handshake and before any frame, so a refusal closes the
        connection with zero application bytes sent (the ``bucket_token`` never
        leaves the host). ``accept_new`` records a first sighting; ``ask`` calls
        ``prompt`` and records on acceptance; a changed fingerprint is always
        fatal.
        """
        policy = self.policy
        if policy is TrustPolicy.none:
            return
        cert = _peer_certificate(client)
        if cert is None:
            raise BootnodeFingerprintMismatch(
                f"bootnode {format_authority(self.host, self.port)}: could not read the peer "
                "certificate to verify its fingerprint"
            )
        observed = compute_cert_fingerprint(cert)
        store = default_bootnode_trust_store()

        # Explicit pins (``--bootnode-fingerprint``) are accepted directly and are
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

    def _prompt_and_pin(self, store: BootnodeTrustStore, observed: str) -> None:
        """Run the interactive trust prompt; pin on acceptance, refuse otherwise."""
        if self.prompt is None:
            raise BootnodeNotTrusted(
                f"bootnode {format_authority(self.host, self.port)}: certificate fingerprint {observed} "
                "is not trusted and there is no terminal to confirm it; pass "
                "--bootnode-tofu (accept new) or --bootnode-fingerprint <fp> to trust it",
                non_interactive=True,
            )
        if self.prompt(self.host, self.port, observed):
            store.set(self.host, self.port, observed, source="prompt")
            self._announce_known_host(store)
            return
        raise BootnodeNotTrusted(
            f"bootnode {format_authority(self.host, self.port)}: certificate fingerprint {observed} "
            "was not accepted; nothing was saved. Pass --bootnode-tofu to trust the "
            "first sighting or --bootnode-fingerprint <fp> to pin it"
        )

    def _announce_known_host(self, store: BootnodeTrustStore) -> None:
        """Report a just-pinned fingerprint together with the store that holds it."""
        log.info(known_hosts_added_message(self.host, self.port, store.path))

    def _mismatch(
        self,
        observed: str,
        *,
        stored: str | None,
        store: BootnodeTrustStore,
    ) -> BootnodeFingerprintMismatch:
        """Build the fatal refusal: concise reason plus SSH-style recovery guidance.

        The structured event mirrors the master path: a node that is not pinned
        under ``strict`` emits ``pairing_needed`` (an explicit trust confirmation
        is required), while a changed pin or an explicit-pin mismatch emits
        ``fingerprint_changed``.
        """
        if stored is not None:
            reason = (
                f"bootnode {format_authority(self.host, self.port)}: certificate fingerprint {observed} "
                "does not match the pinned fingerprint (possible man-in-the-middle)"
            )
        elif self.fingerprints:
            reason = (
                f"bootnode {format_authority(self.host, self.port)}: certificate fingerprint {observed} "
                "does not match any fingerprint passed with --bootnode-fingerprint"
            )
        else:
            reason = (
                f"bootnode {format_authority(self.host, self.port)}: certificate fingerprint {observed} "
                "is not pinned and the trust policy is 'strict'"
            )
        if stored is None and not self.fingerprints:
            emit_event(
                UiEventName.pairing_needed,
                scope="bootnode",
                peer_address=f"{format_authority(self.host, self.port)}",
                fingerprint=observed,
                message=reason,
            )
        else:
            emit_event(
                UiEventName.fingerprint_changed,
                scope="bootnode",
                peer_address=f"{format_authority(self.host, self.port)}",
                fingerprint=observed,
                message=reason,
            )
        return BootnodeFingerprintMismatch(
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
    is unavailable and the caller fails closed.
    """
    tls = getattr(protocol._quic, "tls", None)
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
    startup_time: float = 0.0,
    sync_direction: str = "bidirectional",
) -> BootRegister:
    """Build a ``register`` frame (validated against the mirror before send).

    ``server_ready``/``prefer_master``/``never_master``/``startup_time``/
    ``sync_direction`` are the election advertisement the node relays verbatim
    in its ``query`` peers (the same values the mDNS TXT record carries), so a
    bootnode-discovered peer ranks in the master election exactly like a
    LAN-discovered one.
    """
    frame = BootRegister(
        protocol_version=PROTOCOL_VERSION_BOOT,
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
) -> BootRefresh:
    """Build a ``refresh`` frame (TTL + ``connected`` only; no endpoints)."""
    frame = BootRefresh(
        protocol_version=PROTOCOL_VERSION_BOOT,
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
) -> BootLeave:
    """Build a ``leave`` frame."""
    frame = BootLeave(
        protocol_version=PROTOCOL_VERSION_BOOT,
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
) -> BootQuery:
    """Build a ``query`` frame (generic sample request; no file targeting)."""
    frame = BootQuery(
        protocol_version=PROTOCOL_VERSION_BOOT,
        type="query",
        bucket=bucket,
    )
    _apply_optional(frame, group=group, bucket_token=bucket_token, limit=limit)
    return frame
