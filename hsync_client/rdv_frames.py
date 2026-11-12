# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Builders for the ``rdv/1`` rendezvous wire frames.

Pure frame construction kept separate from the ``RendezvousClient`` transport:
the builders only assemble validated ``hsync_common.protocol.rendezvous``
models, so a new optional field is wired once in ``_apply_optional`` instead of
per call site.
"""

from __future__ import annotations

from typing import Any

from hsync_common.protocol.rendezvous import (
    PROTOCOL_VERSION_RDV,
    RdvLeave,
    RdvQuery,
    RdvRefresh,
    RdvRegister,
    RdvRelayAttach,
    RdvRelayConnect,
    RdvRelayListen,
    RdvRelayPing,
)


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
