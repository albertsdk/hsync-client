# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aioquic.asyncio.client import connect
from aioquic.quic.configuration import QuicConfiguration
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

__all__ = [
    "BootnodeClient",
    "BootnodeError",
    "BootnodeProtocolError",
    "BootnodeUnreachable",
    "DEFAULT_QUIC_IDLE_TIMEOUT",
    "build_leave_frame",
    "build_query_frame",
    "build_refresh_frame",
    "build_register_frame",
]

# QUIC idle timeout: a single request/response exchange is well under a second
# on any usable link; the timeout bounds a stalled handshake, not throughput.
DEFAULT_QUIC_IDLE_TIMEOUT = 30.0


class BootnodeError(Exception):
    """Base class for bootnode client failures."""


class BootnodeUnreachable(BootnodeError):
    """The bootnode could not be reached (DNS, timeout, TLS, connection)."""


class BootnodeProtocolError(BootnodeError):
    """The bootnode answered with something that violates ``boot/1``."""


@dataclass(frozen=True)
class BootnodeClient:
    """Synchronous ``boot/1`` client for one bootnode.

    ``ca_cert`` pins the bootnode's TLS certificate (recommended); ``insecure``
    disables verification entirely and is meant for tests/local loopback only.
    """

    host: str
    port: int
    ca_cert: str | None = None
    insecure: bool = False
    timeout: float = 10.0
    idle_timeout: float = DEFAULT_QUIC_IDLE_TIMEOUT

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

    def _ack_exchange(self, frame: BootRegister | BootRefresh) -> BootAck | None:
        raw = self._exchange(frame, expect="ack")
        if raw is None:
            return None
        parsed = validate_boot_ack(raw)
        if parsed is None:
            raise BootnodeProtocolError("malformed ack frame")
        return parsed

    def _exchange(self, frame: Mapping[str, Any], *, expect: str | None) -> dict | None:
        """Run one request/response session on a fresh QUIC stream."""
        validated = validate_boot_frame(frame)
        if validated is None:
            raise BootnodeProtocolError(f"refusing to send an invalid {frame.get('type')!r} frame")
        payload = json.dumps(validated, separators=(",", ":")).encode("utf-8")
        try:
            return asyncio.run(self._exchange_async(payload, expect=expect))
        except BootnodeError:
            raise
        except (TimeoutError, OSError, ssl.SSLError) as exc:
            raise BootnodeUnreachable(f"bootnode {self.host}:{self.port}: {exc}") from exc
        except ConnectionError as exc:
            raise BootnodeUnreachable(f"bootnode {self.host}:{self.port}: {exc}") from exc

    async def _exchange_async(self, payload: bytes, *, expect: str | None) -> dict | None:
        configuration = self._quic_configuration()
        try:
            async with connect(
                self.host,
                self.port,
                configuration=configuration,
                wait_connected=True,
            ) as client:
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
        except TimeoutError as exc:
            raise BootnodeUnreachable(
                f"bootnode {self.host}:{self.port}: timed out waiting for a {expect or 'reply'}"
            ) from exc
        if line is None:
            return None
        try:
            decoded = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BootnodeProtocolError("reply is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise BootnodeProtocolError("reply is not a JSON object")
        reply_type = decoded.get("type")
        if reply_type != expect:
            raise BootnodeProtocolError(f"expected a {expect!r} frame, got {reply_type!r}")
        return decoded

    def _quic_configuration(self) -> QuicConfiguration:
        configuration = QuicConfiguration(
            is_client=True,
            alpn_protocols=[BOOT_ALPN],
            idle_timeout=self.idle_timeout,
        )
        if self.insecure:
            configuration.verify_mode = ssl.CERT_NONE
        elif self.ca_cert:
            configuration.verify_mode = ssl.CERT_REQUIRED
            configuration.load_verify_locations(self.ca_cert)
        else:
            configuration.verify_mode = ssl.CERT_REQUIRED
        return configuration


async def _read_line(reader: Any) -> bytes | None:
    """Read one newline-terminated frame; ``None`` on a clean EOF."""
    line = await reader.readline()
    if not line:
        return None
    return bytes(line).rstrip(b"\n")


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
) -> BootRegister:
    """Build a ``register`` frame (validated against the mirror before send)."""
    frame = BootRegister(
        protocol_version=PROTOCOL_VERSION_BOOT,
        type="register",
        cid=cid,
        bucket=bucket,
        cert_fp=cert_fp,
        version=version,
        capacity=capacity,
        endpoints=list(endpoints or []),
    )
    if group is not None:
        frame["group"] = group
    if bucket_token is not None:
        frame["bucket_token"] = bucket_token
    if connected is not None:
        frame["connected"] = connected
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
    if group is not None:
        frame["group"] = group
    if bucket_token is not None:
        frame["bucket_token"] = bucket_token
    if connected is not None:
        frame["connected"] = connected
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
    if bucket_token is not None:
        frame["bucket_token"] = bucket_token
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
    if group is not None:
        frame["group"] = group
    if bucket_token is not None:
        frame["bucket_token"] = bucket_token
    if limit is not None:
        frame["limit"] = limit
    return frame
