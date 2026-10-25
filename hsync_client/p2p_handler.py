# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
P2P Mode Handler for HSYNC Client.

This module handles P2P synchronization including peer discovery,
master election, and WebSocket communication.
"""

import contextlib
import os
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, cast

from hsync_common.endpoint import format_authority
from hsync_common.hashing import is_sha256_hex
from hsync_common.protocol.pairing import (
    PinDecision,
    evaluate_pin,
    normalize_fingerprint,
    requires_pairing_confirmation,
)
from hsync_common.text import safe_json_for_log
from loguru import logger as module_log

from hsync_client.autotune import classify_network, derive_managed_rtt_updates, measure_rtt
from hsync_client.config import CONFIG, set_config
from hsync_client.constants import (
    BOOTNODE_DEFAULT_PORT,
    HEARTBEAT_INTERVAL,
    WATCHER_START_RETRY_DELAY_S,
    ActionLabel,
)
from hsync_client.events import UiEventName, emit_event
from hsync_client.exceptions import (
    AuthenticationError,
    NetworkError,
    ServerError,
    WatcherStartError,
)
from hsync_client.exitcodes import ExitCode, exit_code_for_result
from hsync_client.master_status import NO_DEVICE_MESSAGE, MasterSyncStatus
from hsync_client.net_utils import get_local_ip
from hsync_client.p2p_discovery import (
    DiscoveryFatalError,
    DiscoveryPlan,
    build_discovery_plan,
)
from hsync_client.p2p_inventory import (
    P2PInventoryPublisher,
    P2PInventoryStore,
    set_active_inventory_publisher,
)
from hsync_client.p2p_peers import dial_candidates, server_url
from hsync_client.p2p_server import P2PServer, P2PServerMode
from hsync_client.p2p_sources import P2PSourceResolver, P2PSourceService
from hsync_client.p2p_websocket import P2PWebSocketServer, SlaveCidRegistry, p2p_websocket_client
from hsync_client.pinning import default_pin_store
from hsync_client.presence import record_presence, slave_connected_count
from hsync_client.preview import _run_preview, _run_stdin_wait, _with_stdin_commands
from hsync_client.ssl_utils import create_fingerprint_ssl_context, verify_peer_cert_fingerprint
from hsync_client.stdin_commands import StdinCommandHandler
from hsync_client.sync import SyncCycleResult, log_initial_sync_start, sync
from hsync_client.trust_messages import (
    known_peer_added_message,
    peer_mismatch_guidance,
    peer_pairing_needed_guidance,
)
from hsync_client.utils import http_to_ws

if TYPE_CHECKING:
    from hsync_client.config import HsyncConfig
    from hsync_client.p2p_peers import PeerManager


# Seconds to wait before warning that no master has been elected (e.g. when every
# peer in the group is configured with --never-master). Emitted as a parsable
# ``[NO MASTER]`` warning so front-ends (desktop UI) can surface a notification.
NO_MASTER_WARNING_TIMEOUT = 60.0

# Message carried by the ``no_peers`` event when a p2p one-shot aborts because no
# peer was discovered (exit code ``P2P_NO_PEER``).
NO_PEERS_MESSAGE = NO_DEVICE_MESSAGE


def _p2p_status(connected_clients: int) -> str:
    """p2p link status: an active peer connection is required to be "connected".

    A serving master with no connected slave is ``waiting`` for a device, not a
    live link (so the status bar stays honest and no "connection lost" fires when
    nobody was ever connected).
    """
    return "connected" if connected_clients > 0 else "waiting"


def _emit_master_presence(count: int, peer_manager: "PeerManager") -> None:
    """Re-emit the local ``peers`` pair while the master serves.

    The discovery loop that normally owns the ``peers`` event is blocked inside
    ``_run_as_master``, so a client joining/leaving must refresh the master's own
    presence too (the mDNS TXT record is updated separately). ``connected``
    includes this node; ``max`` keeps ``2/2`` even when the mDNS peer entry lags
    the WebSocket join.
    """
    total = max(1 + len(peer_manager.get_alive_peers()), 1 + count)
    record_presence(min(1 + count, total), total)


class _MasterChannel:
    """Stdin commands + sync-status classification for the serving role.

    The serving role runs outside any asyncio loop (``_run_as_master`` yields
    with a plain sleep), so the slave-side readers in ``preview.py`` never see
    its commands: without this channel a front-end's ``PREVIEW``/``SYNC`` are
    silently dropped while the node is the master, leaving its file table stuck
    on the pre-connect state (all rows "Local") even though the status bar says
    "connected". ``MasterSyncStatus`` answers with a classification of the
    local share against the inventories the devices published.
    """

    def __init__(
        self,
        p2p_server: P2PServer,
        p2p_ws_server: P2PWebSocketServer,
        peer_manager: "PeerManager",
        log,
    ) -> None:
        self._p2p_server = p2p_server
        self._p2p_ws_server = p2p_ws_server
        self._peer_manager = peer_manager
        self._log = log
        self._status = MasterSyncStatus(lambda: self._p2p_server.inventory_store, log)
        self._reader: StdinCommandHandler | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the command reader and classify once (idempotent).

        Called on every discovery iteration while the node stays master, so a
        running channel must be left alone: the inventory callback — not the
        loop — drives the re-classification.
        """
        if self._thread is not None:
            return
        reader = StdinCommandHandler(self._log)
        # The answers are marker-less ``file_status`` bursts: preview_start/
        # preview_end belong to the front-end's own preview cycles (see
        # MasterSyncStatus.request).
        reader.register("PREVIEW", lambda **_kw: self._status.request())
        # A master is the source of the share: there is nothing to pull, but the
        # command must still acknowledge by refreshing the reported status.
        reader.register("SYNC", lambda **_kw: self._status.request())
        reader.register(
            "PEERS",
            lambda **_kw: _emit_master_presence(
                self._p2p_ws_server.connected_clients, self._peer_manager
            ),
        )
        self._reader = reader
        self._stop.clear()
        self._thread = threading.Thread(
            target=reader.run_in_thread,
            args=(self._stop,),
            name="hsync-stdin-master",
            daemon=True,
        )
        self._thread.start()
        self.refresh()

    def refresh(self) -> None:
        """(Re)wire the inventory hook and ask for a classification."""
        store = self._p2p_server.inventory_store
        if store is not None:
            store.set_change_callback(self._status.request)
        self._status.request()

    def stop(self) -> None:
        """Stop reading commands and detach from the inventory (idempotent)."""
        store = self._p2p_server.inventory_store
        if store is not None:
            store.set_change_callback(None)
        reader, self._reader = self._reader, None
        self._stop.set()
        if reader is not None:
            reader.stop_thread()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def close(self) -> None:
        """Full shutdown: stop the reader and the classification worker."""
        self.stop()
        self._status.stop()

    def invalidate_local(self) -> None:
        """The share changed on disk: do not reuse the cached listing."""
        self._status.invalidate_local()


def _emit_slave_presence(peer_manager: "PeerManager") -> None:
    """Re-emit the local ``peers`` pair when this slave links to the master.

    The discovery loop that normally owns the ``peers`` event is blocked inside
    the WebSocket run for the whole connection, so a slave joining a master must
    refresh its own presence too; otherwise it keeps the ``1/N`` pair it emitted
    before connecting (the master's mDNS TXT count lags the join). ``connected``
    includes this node and a live WSS link proves the master is reachable, so a
    two-node group reads ``2/2`` even before the master's TXT count converges.
    """
    master = peer_manager.get_master()
    if master is None:
        return
    total = 1 + len(peer_manager.get_alive_peers())
    connected = 1 + slave_connected_count(peer_manager, master)
    record_presence(min(connected, total), total)


# Upper bound on how long the bootnode plan may delay the master's [READY]
# while it waits for the endpoint publication (second register). UPnP is
# best-effort, so publication happens even when no mapping could be created;
# the bound only guards a wedged lease loop.
READINESS_ENDPOINT_TIMEOUT_S = 10.0

# Bounded wait for live WebSocket clients to finish closing on a role yield,
# before the shared inventory store is detached and closed (ADR-0027). A peer
# that ignores the close frame must not stall the yield indefinitely.
_WS_CLOSE_GRACE_S = 5.0

# Per-candidate reachability budget when a master advertises several data-plane
# endpoints (bootnode discovery). An unreachable candidate must not stall the
# connection for the full request timeout before the next one is tried; a
# same-subnet candidate that fails fast falls back to the public endpoint.
MASTER_CANDIDATE_PROBE_TIMEOUT_S = 3.0


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


def _start_slave_seeder(
    config: "HsyncConfig",
    p2p_sync: DiscoveryPlan,
    log,
    p2p_server: P2PServer | None,
) -> P2PInventoryPublisher | None:
    """Serve this peer's shareable files read-only, degrading cleanly.

    Reuses the process-wide unified listener: installing the seeder role swaps
    the serving context in place (no rebind, no port conflict).
    """
    if (
        config.get("ONE_SHOT", False)
        or config.get("DRYRUN", False)
        or not config.get("P2P_PEER_DOWNLOAD", True)
        or p2p_server is None
    ):
        return None
    publisher = P2PInventoryPublisher(config["LOCAL_BUCKET"])
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


def _stop_slave_seeder(
    p2p_sync: DiscoveryPlan,
    publisher: P2PInventoryPublisher | None = None,
) -> None:
    """Withdraw read-only serving readiness and detach inventory.

    Only withdraws when a seeder was actually active (``publisher`` is set):
    the master loop calls this every iteration, and an unconditional
    ``update_endpoint_ready(False)`` would re-register mDNS and clear the
    master's own ``server_ready`` flag.
    """
    if publisher is None:
        return
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
    selected dial candidate (a bootnode peer may serve on a different candidate
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

    rtt_ms = measure_rtt(
        host if host is not None else master.address,
        port if port is not None else master.port,
    )
    if rtt_ms is None:
        log.debug("autotune (P2P): RTT to master not measurable; keeping startup tuning")
        return

    updates = derive_managed_rtt_updates(rtt_ms, managed)
    if not updates:
        return

    for key, value in updates.items():
        cast(dict[str, Any], CONFIG)[key] = value
        cast(dict[str, Any], config)[key] = value
    log.info(
        f"autotune (P2P): rtt={rtt_ms:.1f}ms net={classify_network(rtt_ms).value} "
        f"-> {safe_json_for_log(updates)}"
    )


def run_p2p_mode(config: "HsyncConfig", log) -> int:
    """
    Handle P2P mode synchronization.

    This function orchestrates peer discovery, master election,
    and either runs as master or connects to a master peer.

    Args:
        config: The hsync configuration dictionary.
        log: Logger instance for messages.

    Returns:
        The process exit code for the run: a completed one-shot slave
        sync maps its verdict through the documented code table, no
        discovered peer yields P2P_NO_PEER, and an interrupted run
        yields OK.
    """
    log.info("P2P mode enabled")

    # Clock offset (ADR-0023): in P2P mode no server is known at startup, so
    # the public SNTP hosts (HSYNC_NTP_HOSTS) are the time authority. The probe
    # runs on a background thread: discovery and the first sync never wait on
    # an external time source, and failures degrade to offset 0 (uncorrected
    # local time) without blocking anything.
    from hsync_client.timesync import start_ntp_clock_offset_probe

    start_ntp_clock_offset_probe(logger=log)

    one_shot = config.get("ONE_SHOT", False)
    if not one_shot:
        log.info("Continuous mode enabled - will wait indefinitely for peers")

    discovery_timeout = config["DISCOVERY_TIMEOUT"]
    wait_indefinitely_for_peer = discovery_timeout == 0
    # The effective heartbeat (env-overridable) sets the discovery poll cadence,
    # so staleness is re-evaluated at least once per announce cycle.
    heartbeat = config.get("HEARTBEAT_INTERVAL") or HEARTBEAT_INTERVAL
    discovery_wait = heartbeat if wait_indefinitely_for_peer else discovery_timeout
    if wait_indefinitely_for_peer and one_shot:
        log.info("Discovery timeout disabled - waiting indefinitely for the first P2P peer/master")

    from hsync_client.identity import load_or_create_device_identity

    # Reuses the device identity resolved at startup (cached): the keypair
    # presented to peers always belongs to this installation's device.
    cert_info = load_or_create_device_identity().cert_info
    log.info(f"Certificate fingerprint: {cert_info.fingerprint[:32]}...")

    # One shared duplicate-identity registry for both serving channels (HTTP
    # write endpoints and WSS admissions): a clone refused on one is refused on
    # the other too. It persists for the process lifetime and across mastership
    # changes (the WSS stop clears the bindings).
    cid_registry = SlaveCidRegistry(master_cid=config["CID"])

    # The serving stack is bound ONCE for the process lifetime: the unified
    # HTTPS/WSS listener reserves the port before discovery starts, so the
    # advertised port is the real bound one (no probe/release race). The role
    # (master / read-only seeder / off) is a context swapped in place.
    p2p_ws_server = P2PWebSocketServer(
        port=config["P2P_LISTEN_PORT"] or 0,
        master_cid=config["CID"],
        cid_registry=cid_registry,
    )
    p2p_server = P2PServer(
        port=config["P2P_LISTEN_PORT"],
        use_https=True,
        cert_file=cert_info.cert_file,
        key_file=cert_info.key_file,
        cid_guard=cid_registry,
        websocket_server=p2p_ws_server,
    )
    p2p_server.start()
    p2p_server.stop_serving()

    # The discovery plan (mDNS or bootnode) is selected in one place; the core
    # only ever sees the DiscoveryPlan surface and never branches on the plan.
    # Discovery advertises the already-bound serving port.
    p2p_sync = build_discovery_plan(
        config, cert_fingerprint=cert_info.fingerprint, port=p2p_server.port
    )
    bootnode = config.get("BOOTNODE")
    if bootnode:
        log.info(
            "Discovery: mDNS on the local network + bootnode "
            f"{bootnode}:{config.get('BOOTNODE_PORT', BOOTNODE_DEFAULT_PORT)} "
            "(cross-network; UPnP "
            f"{'on' if config.get('UPNP_ENABLED', True) else 'off'})"
        )
        emit_event(UiEventName.discovery, plan="mdns+bootnode", bootnode=bootnode)
    else:
        log.info("Discovery: mDNS on the local network (no bootnode configured)")
        emit_event(UiEventName.discovery, plan="mdns")
    if config["P2P_NEVER_MASTER"]:
        log.info("Never-master mode enabled - this client will always remain a client/slave")
    peer_manager = p2p_sync.start(register_service=True)

    seeder_attempted = False
    master_serving = False
    inventory_publisher: P2PInventoryPublisher | None = None
    master_channel: _MasterChannel | None = None

    last_exit = int(ExitCode.OK)
    discovery_deadline = (
        time.monotonic() + discovery_timeout
        if one_shot and not wait_indefinitely_for_peer
        else None
    )

    # Track how long the group has been without an elected master so we can warn
    # once after NO_MASTER_WARNING_TIMEOUT (e.g. when every peer is never-master).
    no_master_since: float | None = None
    no_master_warned = False

    # Per-(kind, cid) dedupe of the trust refusal blocks/events: the discovery
    # loop retries continuously, so a repeating refusal must not spam the log or
    # the front-end.
    notified: dict[tuple[str, str], str] = {}

    try:
        while True:
            # Wait for peer discovery
            current_wait = float(discovery_wait)
            if discovery_deadline is not None:
                current_wait = min(
                    current_wait,
                    max(0.0, discovery_deadline - time.monotonic()),
                )
            p2p_sync.wait_for_discovery(timeout=current_wait)

            # Expire peers whose heartbeats stopped and re-elect if the master
            # vanished. Discovery is event-driven (mDNS announcements), which a
            # hard-crashed peer stops sending entirely — so this periodic sweep
            # is what bounds failover detection to ``peer_timeout`` even when no
            # further event arrives (e.g. a two-node group whose master died).
            peer_manager.reconcile_stale_peers()

            # A one-shot run must terminate: its discovery window is its whole
            # lifetime budget. Once that window expired without a single live
            # peer there is nothing to sync and nobody to serve, yet the local
            # role may be MASTER — a lone eligible node claims mastership at
            # discovery start so a joining slave finds a serving master. Without
            # this gate that self-conferred role would keep the continuous
            # master loop alive forever, so `--once` would never honour the
            # documented no-peer exit code. A master that does have live peers
            # (or any continuous run, which has no deadline) stays continuous.
            # A single clock read feeds both gates below so mocked and real
            # clocks advance one step per iteration.
            now_after_discovery = time.monotonic()
            if (
                discovery_deadline is not None
                and now_after_discovery >= discovery_deadline
                and not peer_manager.get_alive_peers()
            ):
                log.warning("No master elected - this client is alone, nothing to sync")
                emit_event(UiEventName.no_peers, message=NO_PEERS_MESSAGE)
                return int(ExitCode.P2P_NO_PEER)

            # A finite one-shot window must not end merely because an
            # ineligible seeder wakes discovery before the serving master is
            # announced. Keep consuming mDNS changes until a master is elected
            # or the original deadline expires.
            if (
                discovery_deadline is not None
                and peer_manager.get_master_cid() is None
                and now_after_discovery < discovery_deadline
            ):
                continue

            if peer_manager.is_master():
                no_master_since = None
                no_master_warned = False
                _stop_slave_seeder(p2p_sync, inventory_publisher)
                inventory_publisher = None
                seeder_attempted = False

                # Wire the master's live WebSocket client count into discovery
                # presence BEFORE serving: the provider feeds wait_for_discovery
                # (the single owner of the peers event) and the callback refreshes
                # this node's mDNS ``connected`` TXT value on every join/leave.
                # Wiring it only after ``_run_as_master`` would let the master
                # advertise ``connected=0`` and miss clients that connect while it
                # is serving, so peers would keep reading a stale 0.
                ws_server = p2p_ws_server

                def ws_connected_count(server: P2PWebSocketServer = ws_server) -> int:
                    return server.connected_clients

                def on_clients_changed(count: int) -> None:
                    p2p_sync.set_connected_clients(count)
                    manager = p2p_sync.peer_manager
                    if manager is not None:
                        # The discovery loop is blocked while serving; refresh the
                        # master's own presence on join/leave.
                        _emit_master_presence(count, manager)
                    # "connected" means an active peer link: a lone serving
                    # master is "waiting" for a device to connect.
                    emit_event(UiEventName.status, status=_p2p_status(count))

                p2p_sync.set_connected_clients_provider(ws_connected_count)
                p2p_ws_server.set_clients_changed_callback(on_clients_changed)

                p2p_server, p2p_ws_server = _run_as_master(
                    config=config,
                    log=log,
                    p2p_sync=p2p_sync,
                    p2p_server=p2p_server,
                    p2p_ws_server=p2p_ws_server,
                    cid_registry=cid_registry,
                )
                # A watcher-failure return leaves the control plane not running:
                # the flag must reflect whether the master is actually serving.
                master_serving = p2p_ws_server.running
                # The serving role answers the front-end's PREVIEW/SYNC through
                # its own channel (no asyncio loop runs here) and re-classifies
                # whenever a device publishes a new inventory.
                if master_channel is None:
                    master_channel = _MasterChannel(p2p_server, p2p_ws_server, peer_manager, log)
                master_channel.start()
                # Check if we're still master after _run_as_master returns
                if not peer_manager.is_master():
                    # We lost mastership: retire the serving role, then revoke our
                    # incumbency (local flag + mDNS server_ready) so peers no
                    # longer rank us as an incumbent — a yielded node that left
                    # server_ready=true would be a "zombie incumbent" and could
                    # prolong a split-brain (ADR-0027 leadership lease).
                    _stop_p2p_serving(p2p_ws_server, p2p_server)
                    master_serving = False
                    inventory_publisher = None
                    if master_channel is not None:
                        master_channel.stop()
                    p2p_sync.set_connected_clients_provider(None)
                    p2p_ws_server.set_clients_changed_callback(None)
                    p2p_sync.update_server_not_ready()
                    log.warning(
                        f"{ActionLabel.MASTER_YIELD} this client yielded mastership; "
                        f"the deterministic election picked "
                        f"{peer_manager.get_master_cid() or 'no eligible peer'}."
                    )
                    # Back to the discovery loop: the desktop status drops to
                    # "connecting" until the new master link is established.
                    emit_event(UiEventName.status, status="connecting")
                # NOTE: the master role is inherently continuous: it must
                # stay available to serve peers regardless of the one-shot
                # flag (which only applies to slaves). The loop only exits
                # via KeyboardInterrupt or by losing the master role and
                # being elected slave on a later iteration.
            else:
                # Retire any serving role before acting as a requester. The
                # process-wide listener stays bound and is reconfigured.
                if master_serving:
                    _stop_p2p_serving(p2p_ws_server, p2p_server)
                    master_serving = False
                    if master_channel is not None:
                        master_channel.stop()
                    p2p_sync.set_connected_clients_provider(None)
                    p2p_sync.update_server_not_ready()
                # Warn (once) if no master has been elected for a while: this
                # happens when every reachable peer is in never-master mode.
                if peer_manager.get_master_cid() is None:
                    now = time.time()
                    if no_master_since is None:
                        no_master_since = now
                    elif not no_master_warned and (
                        now - no_master_since >= NO_MASTER_WARNING_TIMEOUT
                    ):
                        log.warning(
                            f"{ActionLabel.NO_MASTER} No master elected after "
                            f"{int(now - no_master_since)}s. All discovered peers "
                            f"may be configured with --never-master; no node will "
                            f"serve files and synchronization cannot proceed."
                        )
                        emit_event(
                            UiEventName.no_master,
                            elapsed=int(now - no_master_since),
                        )
                        no_master_warned = True
                else:
                    no_master_since = None
                    no_master_warned = False

                if not seeder_attempted and not one_shot:
                    inventory_publisher = _start_slave_seeder(config, p2p_sync, log, p2p_server)
                    seeder_attempted = True

                should_continue, exit_code = _run_as_slave(
                    config=config,
                    log=log,
                    one_shot=one_shot,
                    peer_manager=peer_manager,
                    wait_indefinitely_for_peer=wait_indefinitely_for_peer,
                    inventory_publisher=inventory_publisher,
                    notified=notified,
                    plan=p2p_sync,
                )
                last_exit = exit_code
                if not should_continue:
                    break

    except DiscoveryFatalError as exc:
        # The plan stopped for good (e.g. bootnode certificate pin mismatch):
        # no peer can ever be discovered, so exit fatally instead of waiting.
        log.error(f"P2P discovery stopped: {exc}")
        last_exit = int(ExitCode.FATAL)
    except KeyboardInterrupt:
        log.info("Shutting down P2P mode...")
    finally:
        if master_channel is not None:
            master_channel.close()
        _stop_p2p_serving(p2p_ws_server, p2p_server)
        _stop_slave_seeder(p2p_sync, inventory_publisher)
        p2p_server.stop()
        p2p_sync.stop()

    return last_exit


def _run_as_master(
    config: "HsyncConfig",
    log,
    p2p_sync: DiscoveryPlan,
    p2p_server: P2PServer,
    p2p_ws_server: P2PWebSocketServer,
    cid_registry: SlaveCidRegistry | None = None,
) -> tuple[P2PServer, P2PWebSocketServer]:
    """Run as master (sync coordinator).

    The master role is inherently continuous: the master must stay available to
    serve any peer that joins. The one-shot flag only affects slave behaviour
    (one-shot vs. continuous sync); the master loop always yields back to the
    outer discovery loop after a brief idle window so master/peer election can
    be re-evaluated.

    The process-wide listener is reused: installing the master role swaps the
    serving context in place (no rebind). Returns the (p2p_server,
    p2p_ws_server) tuple so the caller keeps the same instances across discovery
    iterations.
    """
    # On every master activation the role context is (re)installed; the watcher
    # is only started when it is not already running.
    first_activation = not p2p_ws_server.running

    if first_activation:
        log.info("This client is the MASTER - acting as sync coordinator")

    peer_manager = p2p_sync.peer_manager
    if peer_manager is None:
        raise RuntimeError("P2P peer manager is unavailable")

    # The inventory store and source service live for the whole master stint:
    # create them only on the first activation and reuse them on every re-entry
    # (the discovery loop re-enters this function roughly once a second while the
    # node stays master). Recreating them would leak a SQLite handle and wipe the
    # live inventory sessions/holders with ``activate()``.
    inventory_store: P2PInventoryStore | None
    source_service: P2PSourceService | None
    if first_activation:
        inventory_store = None
        if config.get("P2P_PEER_DOWNLOAD", True):
            inventory_store = P2PInventoryStore.for_bucket(config["REMOTE_BUCKET"])
            inventory_store.activate()
        source_service = (
            P2PSourceService(
                bucket=config["REMOTE_BUCKET"],
                master_cid=config["CID"],
                inventory_store=inventory_store,
                peer_snapshot_provider=peer_manager.get_alive_peer_snapshots,
            )
            if inventory_store is not None
            else None
        )
    else:
        inventory_store = p2p_server.inventory_store
        source_service = p2p_server.source_service

    # Wire the WebSocket-authoritative liveness signal on every master start: a
    # re-election may install a fresh PeerManager, so the callback must never
    # stay bound to a stale one.
    p2p_ws_server.set_peer_connection_callback(
        lambda cid, connected: (
            peer_manager.mark_peer_connected(cid)
            if connected
            else peer_manager.mark_peer_disconnected(cid)
        )
    )
    p2p_ws_server.set_inventory_store(inventory_store)

    # Install the master role on the process-wide listener.
    p2p_server.configure_role(
        P2PServerMode.master,
        bucket=config["REMOTE_BUCKET"],
        cid_guard=cid_registry if cid_registry is not None else p2p_server.registry,
        source_service=source_service,
        inventory_store=inventory_store,
        peer_read_cap=config.get("P2P_PEER_READ_CAP", 2),
    )

    # Start the master watcher. A retry after a kernel-limit failure re-enters
    # here on the next discovery iteration.
    if not p2p_ws_server.running:
        try:
            p2p_ws_server.start(watch_path=config["LOCAL_BUCKET"])
        except WatcherStartError:
            # The master watcher could not start (inotify kernel limits). The
            # parsable [WATCHER ERROR] line was already logged at the watcher
            # start site. Back off: without the watcher the master cannot serve
            # realtime events, but the retry must be slow because a kernel limit
            # never clears at the discovery cadence. server_ready is NOT
            # advertised, so slaves keep waiting instead of connecting to a
            # master that cannot watch.
            #
            # Retire the role exactly like every other yield path (ADR-0027):
            # clear the serving role, close live WebSocket clients, drop the
            # duplicate-identity registry and any parked delta state — the
            # listener is reused, so none of it may survive into the retry.
            _stop_p2p_serving(p2p_ws_server, p2p_server)
            time.sleep(WATCHER_START_RETRY_DELAY_S)
            return p2p_server, p2p_ws_server
        log.info(f"Master realtime channel on port {p2p_sync.port} (WSS)")

        # Serving with no connected slave is "waiting for devices", not a live
        # link: "connected" means at least one active peer connection.
        emit_event(
            UiEventName.status,
            status=_p2p_status(p2p_ws_server.connected_clients),
        )

        if config["P2P_KEY"]:
            log.info("Authentication enabled (HSYNC_P2P_KEY)")
        else:
            log.info("No authentication (trust P2P mode)")

        # Update mDNS service to indicate server is ready.
        #
        # Test seam (black-box tests only): HSYNC_BB_MASTER_READY_DELAY widens
        # the "elected but not yet server_ready" window so a prefer_master peer
        # can deterministically take over and exercise the yield path
        # (ADR-0027). It is read directly from the environment — not via
        # EnvSettings — so it never affects packaged deployments or the
        # documented HSYNC_* configuration surface.
        ready_delay = float(os.environ.get("HSYNC_BB_MASTER_READY_DELAY", "0") or "0")
        if ready_delay > 0:
            time.sleep(ready_delay)
        # A peer may have won the election while this node was starting up: a
        # --prefer-master peer that joined after we self-elected but before we
        # advertised server_ready (or an earlier-started peer that was only just
        # announced). Advertising the incumbency now would out-rank that winner —
        # server_ready beats prefer_master — and re-elect this node, so two nodes
        # would serve the same bucket and a later crash-failover could never
        # converge on a single successor. Yield instead of claiming readiness:
        # returning here lets the caller's yield path retire the serving role and
        # revoke the incumbency (ADR-0027). An already-serving incumbent never
        # reaches this point (the block runs on the first activation only), so
        # the "a serving incumbent keeps the role" rule is unchanged.
        if not peer_manager.is_master():
            log.info(
                "Mastership changed while starting up (master: "
                f"{peer_manager.get_master_cid() or 'no eligible peer'}); not "
                "advertising server_ready so the winner keeps the role."
            )
            return p2p_server, p2p_ws_server
        p2p_sync.update_server_ready()
        # Bootnode plan only: the master is reachable across networks only once
        # its endpoint has been published to the bootnode (UPnP ok or public IP
        # known). mDNS reports published immediately, so its readiness timing is
        # unchanged. The wait is bounded so a wedged lease loop degrades to the
        # configured readiness timing instead of hanging.
        if not p2p_sync.endpoint_published():
            deadline = time.monotonic() + READINESS_ENDPOINT_TIMEOUT_S
            while not p2p_sync.endpoint_published() and time.monotonic() < deadline:
                time.sleep(0.1)
        from hsync_client.utils import _write_ready_file as _signal_ready

        _signal_ready()

        # Readiness threshold: HTTP + WebSocket servers are up and the master
        # watcher is running (started inside P2PWebSocketServer.start) — the
        # master serves peers in real time.
        log.info(
            f"{ActionLabel.READY} Master ready — serving '{config['REMOTE_BUCKET']}' "
            f"on port {p2p_sync.port} (HTTPS), watching '{config['LOCAL_BUCKET']}'"
        )

    if first_activation:
        log.info("Press Ctrl+C to stop...")
    # Yield back to the outer discovery loop after a short idle window
    # so master/peer election can be re-evaluated.  If we are still
    # master on the next iteration, the outer loop will call us again
    # and reuse the existing server instances.
    time.sleep(1)

    return p2p_server, p2p_ws_server


def _mark_trust_notice(
    notified: dict[tuple[str, str], str] | None,
    kind: str,
    cid: str,
    fingerprint: str,
) -> bool:
    """Dedupe a trust refusal; return True when it was already notified.

    ``notified`` maps ``(kind, cid)`` to the last fingerprint notified. The first
    time a given fingerprint is seen for a ``(kind, cid)`` pair it is recorded and
    the call returns ``False`` (the caller logs/emits); a repeat of the same
    fingerprint returns ``True`` (suppress). With ``notified`` None every call
    returns ``False`` and nothing is recorded (direct/test callers).
    """
    if notified is None:
        return False
    key = (kind, cid)
    if notified.get(key) == fingerprint:
        return True
    notified[key] = fingerprint
    return False


def _clear_trust_notice(
    notified: dict[tuple[str, str], str] | None,
    cid: str,
) -> None:
    """Forget the recorded refusals for *cid* once its connection proceeds."""
    if notified is None:
        return
    notified.pop(("pairing_needed", cid), None)
    notified.pop(("fingerprint_changed", cid), None)


def _evaluate_master_pin(
    config: "HsyncConfig",
    master,
    log,
    notified: dict[tuple[str, str], str] | None = None,
) -> bool:
    """TOFU-pin the master's certificate; return whether the connection may proceed.

    The trust decisions are the shared ones in
    :mod:`hsync_common.protocol.pairing`; here we only persist the pin and surface
    structured security events so a front-end can prompt the user instead of
    silently trusting a certificate.

    Two cases refuse the connection (return ``False``):

    * **Strict pairing** (``P2P_REQUIRE_PAIRING``): a first-seen certificate is
      not auto-pinned. We emit ``pairing_needed`` and refuse until the operator
      confirms the peer out of band (by pinning it or disabling strict pairing).
    * **Fingerprint changed**: the certificate no longer matches the pin. We log
      a parsable ``[FINGERPRINT CHANGED]`` block, emit ``fingerprint_changed`` and
      refuse (possible man-in-the-middle).

    ``notified`` (see :func:`_mark_trust_notice`) dedupes the refusal blocks and
    their events: the discovery loop retries continuously, so without it the
    guidance would repeat every iteration. The entry is cleared once the
    connection proceeds (:func:`_clear_trust_notice`), so a later flip re-notifies.
    """
    from hsync_client.pinning import PinStoreError

    store = default_pin_store()
    bucket = config["REMOTE_BUCKET"]
    raw_fingerprint = master.cert_fingerprint
    if not isinstance(raw_fingerprint, str):
        log.error(
            f"{ActionLabel.SECURITY} P2P master {master.cid} advertised an invalid "
            "certificate fingerprint. Refusing to connect."
        )
        return False
    fingerprint = normalize_fingerprint(raw_fingerprint)
    if not is_sha256_hex(fingerprint):
        log.error(
            f"{ActionLabel.SECURITY} P2P master {master.cid} advertised a non-SHA-256 "
            "certificate fingerprint. Refusing to connect."
        )
        return False

    try:
        if config.get("P2P_REQUIRE_PAIRING", False):
            first_seen = evaluate_pin(store.get(master.cid, bucket), fingerprint)
            if requires_pairing_confirmation(first_seen, require_pairing=True):
                if not _mark_trust_notice(notified, "pairing_needed", master.cid, fingerprint):
                    log.error(
                        peer_pairing_needed_guidance(
                            master.cid, bucket, master.address, fingerprint
                        )
                    )
                    emit_event(
                        UiEventName.pairing_needed,
                        scope="master",
                        peer_cid=master.cid,
                        peer_address=master.address,
                        fingerprint=fingerprint,
                        message="P2P master requires explicit pairing confirmation",
                    )
                return False

        decision = store.check_and_record(master.cid, bucket, fingerprint)
    except PinStoreError as exc:
        log.error(
            f"{ActionLabel.SECURITY} Cannot establish durable trust for P2P master "
            f"{master.cid}: {exc}. Refusing to connect."
        )
        return False
    if decision is PinDecision.changed:
        if not _mark_trust_notice(notified, "fingerprint_changed", master.cid, fingerprint):
            log.error(
                peer_mismatch_guidance(master.cid, bucket, master.address, fingerprint, store.path)
            )
            emit_event(
                UiEventName.fingerprint_changed,
                scope="master",
                peer_cid=master.cid,
                peer_address=master.address,
                fingerprint=fingerprint,
                message="P2P master certificate fingerprint changed",
            )
        return False
    _clear_trust_notice(notified, master.cid)
    if decision is PinDecision.trusted:
        log.info(known_peer_added_message(master.cid, bucket, store.path))
    return True


def _probe_master_endpoint(host: str, port: int, fingerprint: str, timeout: float) -> bool:
    """Whether *host:port* completes a fingerprint-pinned TLS handshake.

    The check is the handshake itself: any completed handshake whose peer
    certificate matches the pin proves the endpoint is dialable, independent of
    the HTTP status a keyed master would answer an unauthenticated request with
    (``401``). The certificate is verified on the live connection, never on a
    separate probe, so there is no TOCTOU window.
    """
    try:
        context = create_fingerprint_ssl_context(fingerprint)
        with socket.create_connection((host, port), timeout=timeout) as connection:
            with context.wrap_socket(connection, server_hostname=None) as tls:
                return verify_peer_cert_fingerprint(tls, fingerprint)
    except (OSError, ValueError):
        return False


def _select_master_endpoint(master, log) -> tuple[str, int] | None:
    """Pick the first reachable master candidate, host/LAN candidates first.

    A single-candidate peer (mDNS, or a bootnode peer that advertised one
    address) is returned as-is: the data-plane connection is its own
    reachability check, so no extra probe is spent. A peer that advertised
    several candidates is probed in host/LAN-first order with a bounded
    per-candidate timeout; the first candidate whose pinned handshake succeeds
    wins. This is what lets two peers behind the same NAT use the local path
    while a remote peer still falls back to its public endpoint. Returns ``None``
    when no candidate is reachable now.
    """
    candidates = dial_candidates(
        getattr(master, "endpoints", ()),
        master.address,
        master.port,
        get_local_ip(),
    )
    if len(candidates) == 1:
        return candidates[0]
    for host, port in candidates:
        if _probe_master_endpoint(
            host, port, master.cert_fingerprint, MASTER_CANDIDATE_PROBE_TIMEOUT_S
        ):
            if (host, port) != (master.address, master.port):
                log.info(
                    f"Selected master endpoint {format_authority(host, port)} of {len(candidates)}"
                )
            return host, port
    return None


def _run_as_slave(
    config: "HsyncConfig",
    log,
    one_shot: bool,
    peer_manager,
    wait_indefinitely_for_peer: bool = False,
    inventory_publisher: P2PInventoryPublisher | None = None,
    notified: dict[tuple[str, str], str] | None = None,
    plan: DiscoveryPlan | None = None,
) -> tuple[bool, int]:
    """Run as client (connect to master).

    Returns a ``(continue_loop, exit_code)`` tuple: ``continue_loop`` tells
    the discovery loop whether to keep running, ``exit_code`` is the process
    exit status to report when the loop terminates.
    """
    from asyncio import run

    master = peer_manager.get_master()
    if master:
        # Wait for master to be ready (server_ready=true in mDNS)
        if not master.server_ready:
            log.info(f"Waiting for master {master.cid} to start server...")
            return True, int(ExitCode.OK)  # Continue discovery loop

        # Secure-only P2P: a master must serve over HTTPS and advertise a
        # certificate fingerprint we can pin. Anything else is refused; there is
        # no plain-HTTP downgrade path. We do NOT self-promote on refusal (that
        # would create a second master and split-brain); we stay in the
        # discovery loop and warn.
        if not (master.use_https and master.cert_fingerprint):
            log.warning(
                f"{ActionLabel.INSECURE_MASTER} master {master.cid} does not advertise an HTTPS "
                f"endpoint with a certificate fingerprint. Refusing to connect "
                f"(P2P transport is HTTPS-only)."
            )
            return (not one_shot) or wait_indefinitely_for_peer, int(ExitCode.OK)

        if not _evaluate_master_pin(config, master, log, notified=notified):
            # Stay in the discovery loop (continuous) rather than connecting to
            # a master whose identity can no longer be verified or which has not
            # yet been paired out of band.
            return (not one_shot) or wait_indefinitely_for_peer, int(ExitCode.OK)
        log.info(f"Master using HTTPS, fingerprint: {master.cert_fingerprint[:32]}...")
        config["MASTER_CERT_FINGERPRINT"] = master.cert_fingerprint

        # Pick the reachable data-plane endpoint: a bootnode peer advertises
        # several candidates, so the local one is preferred over the NATed
        # public one. A single-candidate peer keeps its endpoint unchanged.
        endpoint = _select_master_endpoint(master, log)
        if endpoint is None:
            log.warning(f"No reachable endpoint for master {master.cid}; retrying discovery.")
            return (not one_shot) or wait_indefinitely_for_peer, int(ExitCode.OK)
        master_host, master_port = endpoint

        master_url = server_url(master_host, master_port, use_https=master.use_https)
        log.info(f"Connecting to master: {master.cid} at {master_url}")
        emit_event(UiEventName.server, url=master_url)

        config["API"]["buckets"] = f"{master_url}/api/v1/buckets"
        ws_master_url = http_to_ws(master_url)
        config["API"]["events"] = f"{ws_master_url}/api/v1/buckets"

        if config["P2P_KEY"]:
            config["REQUEST_HEADERS"]["Authorization"] = f"Bearer {config['P2P_KEY']}"

        # Set compression capability based on master peer
        config["SERVER_COMPRESSION_ENABLED"] = master.compression_enabled
        config["SERVER_DELTA_ENABLED"] = master.delta_enabled

        set_config(config)

        source_resolver = P2PSourceResolver(
            master_url=master_url,
            master_cert_fingerprint=master.cert_fingerprint,
            requester_cid=config["CID"],
            bucket=config["REMOTE_BUCKET"],
            p2p_key=config["P2P_KEY"],
            local_bucket=config["LOCAL_BUCKET"],
            enabled=config.get("P2P_PEER_DOWNLOAD", True),
            master_capable=bool(master.peer_download_enabled),
            timeout=float(config.get("HTTP_TIMEOUT", 30)),
        )

        # Re-derive autotune-managed tuning from the real master RTT (the P2P
        # startup probe is skipped when only the placeholder URL is known)
        _refresh_p2p_network_tuning(master, config, log, host=master_host, port=master_port)

        exit_code = int(ExitCode.OK)
        if config["DRYRUN"]:
            # The master may have exited just before this iteration: its peer
            # record can still look fresh, and the automatic preview would run
            # against a dead endpoint, burning the full retry budget and
            # surfacing a spurious error notification. Probe the data endpoint
            # first; when it fails, demote the master and go back to discovery
            # so the election can pick a reachable node.
            if not _master_endpoint_reachable(master_host, master_port):
                log.warning(
                    f"Master {master.cid} at {master_host}:{master_port} is "
                    "unreachable; skipping the preview and re-entering discovery."
                )
                peer_manager.remove_peer(master.cid)
                return (not one_shot) or wait_indefinitely_for_peer, int(ExitCode.OK)
            # In dry-run mode, only show preview without syncing
            _run_preview(log)
            if one_shot:
                return False, exit_code
            # In continuous mode with dry-run, wait for SYNC/PREVIEW commands.
            # This node is parked here and cannot serve, so it must not be
            # elected master: it withdraws its candidacy both locally and on the
            # wire (a peer that elected it would wait forever for its
            # ``server_ready``). It is restored when the dry run ends (SYNC or
            # stdin EOF) and the node returns to the discovery loop.
            log.info("Dry-run mode: waiting for commands (stdin) or Ctrl+C to exit...")
            if plan is not None:
                plan.set_master_eligibility(False)
            try:

                def _credit_master() -> None:
                    # The preview-confirm flow's first real sync uses the HTTPS
                    # data plane: credit the master for presence now (the
                    # WebSocket opens only after this sync), exactly like the
                    # direct initial-sync branch below.
                    peer_manager.mark_peer_connected(master.cid)
                    _emit_slave_presence(peer_manager)

                def _clear_master_credit() -> None:
                    peer_manager.mark_peer_disconnected(master.cid)

                run(
                    _run_stdin_wait(
                        log,
                        inventory_observer=inventory_publisher,
                        source_resolver=source_resolver,
                        on_sync_start=_credit_master,
                        on_sync_error=_clear_master_credit,
                    )
                )
            finally:
                if plan is not None:
                    plan.set_master_eligibility(True)
        else:
            # Perform initial sync (the master is the transport coordinator).
            # The HTTPS data plane itself proves the master is reachable: credit
            # it for presence now, otherwise the UI sits on "1/N" for the whole
            # initial sync (the WebSocket — and the master's advertised client
            # count — only come up afterwards). Cleared again if the sync fails.
            peer_manager.mark_peer_connected(master.cid)
            _emit_slave_presence(peer_manager)
            try:
                log_initial_sync_start()
                if inventory_publisher is None:
                    result = run(
                        sync(
                            p2p_mode=True,
                            source_resolver=source_resolver,
                            reconcile_dirs=True,
                        )
                    )
                else:
                    result = run(
                        sync(
                            p2p_mode=True,
                            inventory_observer=inventory_publisher,
                            source_resolver=source_resolver,
                            reconcile_dirs=True,
                        )
                    )
            except (AuthenticationError, NetworkError, ServerError):
                # sync() already logged the failure and emitted the structured
                # event. A one-shot run maps a transport failure to a fatal
                # exit; a continuous run must survive a transient failure (e.g.
                # the master going offline mid-sync) and re-enter the discovery
                # loop so the master is re-detected or a new one is elected.
                peer_manager.mark_peer_disconnected(master.cid)
                if (not one_shot) or wait_indefinitely_for_peer:
                    return True, int(ExitCode.OK)
                return False, int(ExitCode.FATAL)
            except Exception:
                # An unexpected error (a genuine bug) must never be masked as a
                # retryable condition, even in continuous mode: surface it as a
                # fatal exit instead of looping forever.
                peer_manager.mark_peer_disconnected(master.cid)
                return False, int(ExitCode.FATAL)
            if isinstance(result, SyncCycleResult):
                exit_code = exit_code_for_result(
                    failed_transfers=result.failed,
                    guard_tripped=result.guard_tripped,
                    conflicts=result.conflicts,
                )
            else:
                # A caller that stubbed `run()` without a cycle result (test
                # doubles, defensive paths) reports a clean exit.
                exit_code = int(ExitCode.OK)

        if one_shot:
            return False, exit_code

        # In continuous mode, connect via WebSocket for real-time events. The
        # control plane shares the master's single HTTPS/WSS port.
        log.info("Initial sync complete, connecting to WebSocket for real-time events...")
        # The master's mDNS record may be quiescent (its TXT only changes on
        # presence/server_ready flips), which would otherwise let this slave
        # falsely demote it while the WebSocket link is healthy. Mark the master
        # live via the WebSocket channel for the whole connection lifetime.
        peer_manager.mark_peer_connected(master.cid)
        # Refresh our own presence now the link is live: the discovery loop that
        # owns the ``peers`` event is blocked for the whole WebSocket run, so
        # without this a connected slave keeps the pre-connect ``1/N`` pair.
        _emit_slave_presence(peer_manager)
        try:
            run(
                _with_stdin_commands(
                    log,
                    p2p_websocket_client(
                        host=master_host,
                        port=master_port,
                        use_https=True,
                        inventory_publisher=inventory_publisher,
                        source_resolver=source_resolver,
                    ),
                    inventory_observer=inventory_publisher,
                    source_resolver=source_resolver,
                )
            )
        except WatcherStartError:
            # The slave's local watcher could not start (inotify kernel
            # limits). The parsable [WATCHER ERROR] line and remediation hint
            # were already logged at the watcher start site. Re-entering the
            # discovery loop at its normal cadence would immediately retry
            # the full initial sync + watcher against the same saturated
            # limit, so back off once before returning to the loop: another
            # process may free inotify resources in the meantime.
            log.info(
                f"Watcher start failed; backing off "
                f"{WATCHER_START_RETRY_DELAY_S:.0f}s before retrying."
            )
            peer_manager.mark_peer_disconnected(master.cid)
            time.sleep(WATCHER_START_RETRY_DELAY_S)
            return True, int(ExitCode.OK)
        except Exception as e:
            log.warning(f"WebSocket connection ended: {type(e).__name__}: {e}")
        # The WebSocket is down: clear the liveness mark so a master that died
        # mid-session is demoted by the sweep below, then re-enter discovery.
        peer_manager.mark_peer_disconnected(master.cid)
        # If WebSocket disconnects, re-enter discovery loop. Expire any peer
        # whose heartbeat already stopped so a master that died mid-session is
        # demoted immediately instead of waiting for the next discovery wake:
        # the surviving peers then elect the pre-agreed successor. A master
        # whose record is still fresh (a local link blip) is left untouched,
        # so a transient drop never triggers a needless takeover/split-brain.
        peer_manager.reconcile_stale_peers()
        log.info("WebSocket disconnected, re-entering discovery loop...")
        return True, int(ExitCode.OK)
    # No master and no peers
    if (not one_shot) or wait_indefinitely_for_peer:
        log.info("Waiting for peers to join...")
        return True, int(ExitCode.OK)  # Continue the discovery loop
    log.warning("No master elected - this client is alone, nothing to sync")
    emit_event(UiEventName.no_peers, message=NO_PEERS_MESSAGE)
    return False, int(ExitCode.P2P_NO_PEER)
