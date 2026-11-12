# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Slave serving lifecycle: inventory publishing, network tuning and transfer concurrency.

The bookkeeping that starts/stops the read-only inventory publisher, re-derives the
transfer tuning, and tracks master-endpoint reachability, extracted from
``hsync_client.p2p_handler``; the P2P orchestration stays there.
"""

import contextlib
import socket
from typing import Any, cast

from hsync_common.text import safe_json_for_log
from loguru import logger as module_log

from hsync_client.autotune import measure_rtt_tuning
from hsync_client.config import CONFIG, HsyncConfig
from hsync_client.constants import (
    MAX_CONCURRENT_TRANSFERS_RELAY,
    ActionLabel,
)
from hsync_client.p2p_inventory_publisher import (
    P2PInventoryPublisher,
    set_active_inventory_publisher,
)
from hsync_client.p2p_master_connect import (
    MASTER_CANDIDATE_PROBE_TIMEOUT_S,
    _stop_master_relay,
)
from hsync_client.p2p_rendezvous import (
    RendezvousPlan,
)
from hsync_client.p2p_server import P2PServer, P2PServerMode
from hsync_client.p2p_websocket import P2PWebSocketServer

# Bounded wait for live WebSocket clients to finish closing on a role yield,
# before the shared inventory store is detached and closed (ADR-0027). A peer
# that ignores the close frame must not stall the yield indefinitely.
_WS_CLOSE_GRACE_S = 5.0

# The transfer-concurrency value in force before a relay episode forced it down,
# remembered so it can be restored when a direct path is used again. ``None``
# means no relay cap is currently applied.
_relay_saved_max_concurrent_transfers: int | None = None


def _master_endpoint_reachable(host: str, port: int, timeout: float | None = None) -> bool:
    """Return True when a TCP connection to the master's data endpoint succeeds.

    A slave that just lost its WebSocket can still hold a "fresh" peer record
    for a master that has exited (``reconcile_stale_peers`` deliberately keeps
    a fresh record so a local link blip never triggers a needless takeover). The
    automatic dry-run preview would then spend its whole retry budget against
    the dead endpoint and surface a spurious error; one short connect answers
    the question cheaply.
    """
    try:
        with socket.create_connection(
            (host, int(port)),
            timeout=timeout or MASTER_CANDIDATE_PROBE_TIMEOUT_S,
        ):
            return True
    except (OSError, TypeError, ValueError):
        return False


def _start_slave_inventory(
    config: "HsyncConfig",
    p2p_sync: RendezvousPlan,
    log,
    p2p_server: P2PServer | None,
) -> P2PInventoryPublisher | None:
    """Start the read-only inventory publisher; serve blocks only when enabled.

    Every slave publishes its inventory so the master can classify the share
    against what each device holds, regardless of dry-run previews or block
    serving. The seeder role (and its readiness advertisement) is installed only
    when ``P2P_PEER_DOWNLOAD`` is enabled; an inventory-only publisher never
    advertises block capacity.
    """
    if p2p_server is None:
        return None
    publisher = P2PInventoryPublisher(config["LOCAL_BUCKET"])
    if config.get("P2P_PEER_DOWNLOAD", True):
        p2p_server.configure_role(
            P2PServerMode.seeder,
            bucket=config["REMOTE_BUCKET"],
            cid_guard=None,
            source_service=None,
            inventory_store=None,
            peer_read_cap=config.get("P2P_PEER_READ_CAP", 2),
        )
        try:
            p2p_sync.update_endpoint_ready(True)
        except Exception as exc:
            try:
                p2p_sync.update_endpoint_ready(False)
            except Exception as cleanup_exc:
                log.warning(f"Peer seeder readiness cleanup failed: {cleanup_exc}")
            p2p_server.stop_serving()
            log.warning(f"Peer seeder readiness could not be published ({exc}); disabling seeding")
            return None
    set_active_inventory_publisher(publisher)
    return publisher


def _stop_slave_inventory(
    p2p_sync: RendezvousPlan,
    publisher: P2PInventoryPublisher | None = None,
    *,
    serving: bool = False,
) -> None:
    """Withdraw read-only serving readiness and detach the inventory publisher.

    Only runs when a publisher was actually started (``publisher`` is set): the
    master loop calls this every iteration, and an unconditional
    ``update_endpoint_ready(False)`` would re-register mDNS and clear the
    master's own ``server_ready`` flag. Readiness is withdrawn only when the
    stopped publisher was actually serving blocks.
    """
    if publisher is None:
        return
    if serving:
        try:
            p2p_sync.update_endpoint_ready(False)
        except Exception as exc:
            module_log.warning(f"Could not withdraw peer seeder readiness: {exc}")
    set_active_inventory_publisher(None)


def _stop_p2p_serving(ws_server: P2PWebSocketServer | None, http_server: P2PServer | None) -> None:
    """Retire the serving role, keeping the process-wide listener bound (ADR-0027).

    The role is cleared FIRST so new HTTP/WS work is refused, then the live
    WebSocket clients are closed and awaited (bounded) before the shared
    inventory store is detached and closed, so no consumer can still be using it
    (ADR-0027). The listening socket stays up (it is swapped, not rebound, on the
    next role). The surrounding provider/publisher resets are left to the
    callers because they differ between the mastership-loss, role-transition and
    shutdown paths.
    """
    close_future = http_server.stop_serving() if http_server is not None else None
    # The relay listener belongs to the serving role: withdraw it together with
    # the role so a yielded node stops accepting relayed sessions at once.
    _stop_master_relay()
    if close_future is not None:
        # Wait for the live clients to be closed before detaching the store they
        # still use to end their inventory sessions.
        with contextlib.suppress(Exception):
            close_future.result(timeout=_WS_CLOSE_GRACE_S)
    if ws_server is not None:
        ws_server.stop()
        ws_server.set_inventory_store(None)
    if http_server is not None:
        http_server.close_inventory()
        # Drop the retired role's parked delta payloads/tokens: the listener is
        # reused, so they must not survive into the next role (ADR-0027).
        http_server.reset_delta_state()


def _refresh_p2p_network_tuning(
    master,
    config: "HsyncConfig",
    log,
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Re-derive autotune-managed network tuning from the real master RTT.

    In P2P mode with the placeholder URL the startup RTT probe is skipped (no
    server is known yet), so the tuning profile starts at SAFE defaults. Once
    the master is discovered there is a real endpoint to measure. Only keys in
    ``AUTOTUNE_MANAGED_KEYS`` (no CLI/env override) are re-derived, preserving
    the CLI > env > autotune precedence.

    ``host``/``port`` override the peer's primary endpoint with the actually
    selected dial candidate (a rendezvous peer may serve on a different candidate
    than its first advertised one); they default to ``master.address`` /
    ``master.port``.

    Updates are applied to both the global ``CONFIG`` and the local *config*
    dict: they are distinct objects (``set_config`` copies values) so callers
    must invoke this after ``set_config(config)`` to keep them in sync.
    """
    if not config.get("AUTOTUNE_ENABLED"):
        return

    managed = set(config.get("AUTOTUNE_MANAGED_KEYS", []))
    if not managed:
        log.debug("autotune (P2P): no autotune-managed keys (all overridden via CLI/env)")
        return

    measured = measure_rtt_tuning(
        host if host is not None else master.address,
        port if port is not None else master.port,
        managed,
    )
    if measured is None:
        log.debug("autotune (P2P): RTT to master not measurable; keeping startup tuning")
        return
    rtt_ms, net_class, updates = measured

    # The class tunes the slave WSS liveness timeouts even when no tuning key is
    # re-derived below, so record it as soon as the RTT is known (the slave reads
    # ``CONFIG["P2P_NET_CLASS"]`` before connecting its WebSocket).
    cast(dict[str, Any], CONFIG)["P2P_NET_CLASS"] = net_class.value
    cast(dict[str, Any], config)["P2P_NET_CLASS"] = net_class.value

    if not updates:
        return

    for key, value in updates.items():
        cast(dict[str, Any], CONFIG)[key] = value
        cast(dict[str, Any], config)[key] = value
    log.info(
        f"autotune (P2P): rtt={rtt_ms:.1f}ms net={net_class.value} -> {safe_json_for_log(updates)}"
    )


def _master_claim_settled(plan: Any) -> bool:
    """True when the plan permits claiming local incumbency.

    A rendezvous-aware plan holds the claim off until it has answered one query
    round (or its bounded grace elapsed), so a node never advertises itself as
    the serving master before it has had a chance to see an already-serving one.
    Plans without cross-network discovery (mDNS-only) are always settled.
    """
    settled = getattr(plan, "discovery_settled", None)
    return True if not callable(settled) else bool(settled())


def _set_transfer_concurrency(value: int, config: "HsyncConfig") -> None:
    """Write ``MAX_CONCURRENT_TRANSFERS`` to both the global and the local dict."""
    cast(dict[str, Any], CONFIG)["MAX_CONCURRENT_TRANSFERS"] = value
    cast(dict[str, Any], config)["MAX_CONCURRENT_TRANSFERS"] = value


def _apply_relay_transfer_tuning(config: "HsyncConfig", log) -> None:
    """Cap slave transfer concurrency while the data plane rides the relay.

    The whole tunnel multiplexes every loopback connection onto one relay
    session, but each connection still pairs its own stream against the node's
    per-peer budget, so a LAN-sized fan-out can exhaust it and the surplus
    streams are refused mid-transfer. The concurrency is forced down here and
    remembered, and :func:`_restore_relay_transfer_tuning` puts it back once a
    direct path is chosen again — a cap that outlived the relay would throttle
    the fast path (autotune only re-derives the key when it manages it).
    """
    global _relay_saved_max_concurrent_transfers

    current = config.get("MAX_CONCURRENT_TRANSFERS")
    if isinstance(current, int) and current <= MAX_CONCURRENT_TRANSFERS_RELAY:
        return
    if _relay_saved_max_concurrent_transfers is None and isinstance(current, int):
        _relay_saved_max_concurrent_transfers = current
    _set_transfer_concurrency(MAX_CONCURRENT_TRANSFERS_RELAY, config)
    log.info(
        f"{ActionLabel.RELAY} path: capping concurrent transfers at "
        f"{MAX_CONCURRENT_TRANSFERS_RELAY}"
    )


def _restore_relay_transfer_tuning(config: "HsyncConfig", log) -> None:
    """Undo :func:`_apply_relay_transfer_tuning` when the relay is left.

    Restores the concurrency that was in force before the relay forced it down.
    Necessary even though ``MAX_CONCURRENT_TRANSFERS`` is autotune-managed: the
    key is not re-derived when ``AUTOTUNE_ENABLED`` is off or the value was
    overridden via CLI/env, and without this the cap would outlive the relay.
    """
    global _relay_saved_max_concurrent_transfers

    saved = _relay_saved_max_concurrent_transfers
    if saved is None:
        return
    _relay_saved_max_concurrent_transfers = None
    _set_transfer_concurrency(saved, config)
    log.info(f"Direct path restored: concurrent transfers back to {saved}")
