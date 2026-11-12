# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
P2P Mode Handler for HSYNC Client.

This module handles P2P synchronization including peer rendezvous,
master election, and WebSocket communication.
"""

import os
import time
from typing import TYPE_CHECKING

from hsync_common.endpoint import format_authority

from hsync_client.asyncio_utils import is_event_loop_teardown_error
from hsync_client.config import CONFIG, set_config
from hsync_client.constants import (
    HEARTBEAT_INTERVAL,
    MASTER_CLAIM_SETTLE_S,
    RENDEZVOUS_DEFAULT_PORT,
    WATCHER_START_RETRY_DELAY_S,
    ActionLabel,
)
from hsync_client.events import (
    UiEventName,
    emit_event,
    emit_warning_cleared,
    note_condition_standing,
    set_link_status,
)
from hsync_client.exceptions import (
    AuthenticationError,
    NetworkError,
    ServerError,
    WatcherStartError,
)
from hsync_client.exitcodes import ExitCode, exit_code_for_result
from hsync_client.master_status import NO_DEVICE_MESSAGE
from hsync_client.p2p_inventory import P2PInventoryStore
from hsync_client.p2p_inventory_publisher import (
    P2PInventoryPublisher,
)
from hsync_client.p2p_master_channel import (
    _emit_master_presence,
    _emit_slave_presence,
    _MasterChannel,
    _p2p_status,
)
from hsync_client.p2p_master_connect import (
    _close_relay_dialer,
    _evaluate_master_pin,
    _MasterEndpointGuard,
    _relay_master_endpoint,
    _select_master_endpoint,
    _start_master_relay,
)
from hsync_client.p2p_peers import server_url
from hsync_client.p2p_rendezvous import (
    RendezvousPlan,
    build_rendezvous_plan,
)
from hsync_client.p2p_server import P2PServer, P2PServerMode
from hsync_client.p2p_serving import (
    _apply_relay_transfer_tuning,
    _master_claim_settled,
    _master_endpoint_reachable,
    _refresh_p2p_network_tuning,
    _restore_relay_transfer_tuning,
    _start_slave_inventory,
    _stop_p2p_serving,
    _stop_slave_inventory,
)
from hsync_client.p2p_sources import P2PSourceResolver, P2PSourceService
from hsync_client.p2p_websocket import P2PWebSocketServer, SlaveCidRegistry, p2p_websocket_client
from hsync_client.preview import _run_preview, _run_stdin_wait, _with_stdin_commands
from hsync_client.sync import SyncCycleResult, log_initial_sync_start, sync
from hsync_client.utils import http_to_ws

if TYPE_CHECKING:
    from hsync_client.config import HsyncConfig


# Seconds to wait before warning that no master has been elected (e.g. when every
# peer in the group is configured with --never-master). Emitted as a parsable
# ``[NO MASTER]`` warning so front-ends (desktop UI) can surface a notification.
NO_MASTER_WARNING_TIMEOUT = 60.0

# Message carried by the ``no_peers`` event when a p2p one-shot aborts because no
# peer was discovered (exit code ``P2P_NO_PEER``).
NO_PEERS_MESSAGE = NO_DEVICE_MESSAGE

# Data relay over the rendezvous: the serving role owns one acceptor (a listener
# control stream plus one bridged stream per requester) and the requesting role
# owns at most one dialer (a tunnel to the elected master). Both are process-wide
# because the rendezvous loop re-enters the role functions every second and the
# tunnel must outlive that call.
# The relay state and the per-candidate probe budget now live in
# ``p2p_master_connect`` (the slave connection policy).


# Upper bound on how long the rendezvous plan may delay the master's [READY]
# while it waits for the endpoint publication (second register). UPnP is
# best-effort, so publication happens even when no mapping could be created;
# the bound only guards a wedged lease loop.
READINESS_ENDPOINT_TIMEOUT_S = 10.0


def run_p2p_mode(config: "HsyncConfig", log) -> int:
    """
    Handle P2P mode synchronization.

    This function orchestrates peer rendezvous, master election,
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
    # runs on a background thread: rendezvous and the first sync never wait on
    # an external time source, and failures degrade to offset 0 (uncorrected
    # local time) without blocking anything.
    from hsync_client.timesync import start_ntp_clock_offset_probe

    start_ntp_clock_offset_probe(logger=log)

    one_shot = config.get("ONE_SHOT", False)
    if not one_shot:
        log.info("Continuous mode enabled - will wait indefinitely for peers")

    rendezvous_timeout = config["PEER_RENDEZVOUS_TIMEOUT"]
    wait_indefinitely_for_peer = rendezvous_timeout == 0
    # The effective heartbeat (env-overridable) sets the rendezvous poll cadence,
    # so staleness is re-evaluated at least once per announce cycle.
    heartbeat = config.get("HEARTBEAT_INTERVAL") or HEARTBEAT_INTERVAL
    rendezvous_wait = heartbeat if wait_indefinitely_for_peer else rendezvous_timeout
    if wait_indefinitely_for_peer and one_shot:
        log.info("Rendezvous timeout disabled - waiting indefinitely for the first P2P peer/master")

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
    # HTTPS/WSS listener reserves the port before rendezvous starts, so the
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

    # The rendezvous plan (mDNS or mDNS + discovery servers) is selected in one
    # place; the core only ever sees the RendezvousPlan surface and never branches
    # on the plan. Rendezvous advertises the already-bound serving port.
    p2p_sync = build_rendezvous_plan(
        config, cert_fingerprint=cert_info.fingerprint, port=p2p_server.port
    )
    endpoints = tuple(config.get("RENDEZVOUS_SERVER_ENDPOINTS") or ())
    mdns_enabled = bool(config.get("MDNS_ENABLED", True))
    if endpoints:
        listed = ", ".join(
            format_authority(endpoint.host, endpoint.port or RENDEZVOUS_DEFAULT_PORT)
            for endpoint in endpoints
        )
        upnp = "on" if config.get("MAP_DATA_PORT", True) else "off"
        if mdns_enabled:
            log.info(
                "Discovery: mDNS on the local network + discovery server(s) "
                f"{listed} (cross-network; UPnP {upnp})"
            )
            # The UI event keeps its documented shape: ``plan`` names the combined
            # plan and ``rendezvous`` carries the configured endpoints.
            emit_event(UiEventName.rendezvous, plan="mdns+rendezvous", rendezvous=listed)
        else:
            log.info(
                f"Discovery: discovery server(s) {listed} only "
                f"(local network excluded; UPnP {upnp})"
            )
            emit_event(UiEventName.rendezvous, plan="rendezvous", rendezvous=listed)
    else:
        log.info("Discovery: mDNS on the local network (no discovery server configured)")
        emit_event(UiEventName.rendezvous, plan="mdns")
    if config["P2P_NEVER_MASTER"]:
        log.info("Never-master mode enabled - this client will always remain a client/slave")
    peer_manager = p2p_sync.start(register_service=True)

    seeder_attempted = False
    master_serving = False
    inventory_publisher: P2PInventoryPublisher | None = None
    master_channel: _MasterChannel | None = None

    last_exit = int(ExitCode.OK)
    rendezvous_deadline = (
        time.monotonic() + rendezvous_timeout
        if one_shot and not wait_indefinitely_for_peer
        else None
    )

    # Track how long the group has been without an elected master so we can warn
    # once after NO_MASTER_WARNING_TIMEOUT (e.g. when every peer is never-master).
    no_master_since: float | None = None
    no_master_warned = False

    # Per-(kind, cid) dedupe of the trust refusal blocks/events: the rendezvous
    # loop retries continuously, so a repeating refusal must not spam the log or
    # the front-end.
    notified: dict[tuple[str, str], str] = {}

    try:
        while True:
            # Wait for peer rendezvous
            current_wait = float(rendezvous_wait)
            if rendezvous_deadline is not None:
                current_wait = min(
                    current_wait,
                    max(0.0, rendezvous_deadline - time.monotonic()),
                )
            p2p_sync.wait_for_rendezvous(timeout=current_wait)

            # Expire peers whose heartbeats stopped and re-elect if the master
            # vanished. Rendezvous is event-driven (mDNS announcements), which a
            # hard-crashed peer stops sending entirely — so this periodic sweep
            # is what bounds failover detection to ``peer_timeout`` even when no
            # further event arrives (e.g. a two-node group whose master died).
            peer_manager.reconcile_stale_peers()

            # Never advertise incumbency before the rendezvous has answered once:
            # a node that has not yet seen an already-serving peer would otherwise
            # claim ``server_ready`` first and out-rank it, splitting the bucket
            # until the next election. The plan bounds its own wait, so a lone
            # node still serves after a short grace (mDNS-only plans are always
            # settled and never reach here).
            if (
                rendezvous_deadline is None
                and peer_manager.is_master()
                and not _master_claim_settled(p2p_sync)
            ):
                time.sleep(MASTER_CLAIM_SETTLE_S / 10)
                continue

            # A one-shot run must terminate: its rendezvous window is its whole
            # lifetime budget. Once that window expired without a single live
            # peer there is nothing to sync and nobody to serve, yet the local
            # role may be MASTER — a lone eligible node claims mastership at
            # rendezvous start so a joining slave finds a serving master. Without
            # this gate that self-conferred role would keep the continuous
            # master loop alive forever, so `--once` would never honour the
            # documented no-peer exit code. A master that does have live peers
            # (or any continuous run, which has no deadline) stays continuous.
            # A single clock read feeds both gates below so mocked and real
            # clocks advance one step per iteration.
            now_after_rendezvous = time.monotonic()
            if (
                rendezvous_deadline is not None
                and now_after_rendezvous >= rendezvous_deadline
                and not peer_manager.get_alive_peers()
            ):
                log.warning("No master elected - this client is alone, nothing to sync")
                emit_event(UiEventName.no_peers, message=NO_PEERS_MESSAGE)
                return int(ExitCode.P2P_NO_PEER)

            # A finite one-shot window must not end merely because an
            # ineligible seeder wakes rendezvous before the serving master is
            # announced. Keep consuming mDNS changes until a master is elected
            # or the original deadline expires.
            if (
                rendezvous_deadline is not None
                and peer_manager.get_master_cid() is None
                and now_after_rendezvous < rendezvous_deadline
            ):
                continue

            if peer_manager.is_master():
                no_master_since = None
                no_master_warned = False
                _stop_slave_inventory(
                    p2p_sync,
                    inventory_publisher,
                    serving=bool(config.get("P2P_PEER_DOWNLOAD", True)),
                )
                inventory_publisher = None
                seeder_attempted = False

                # Wire the master's live WebSocket client count into rendezvous
                # presence BEFORE serving: the provider feeds wait_for_rendezvous
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
                        # The rendezvous loop is blocked while serving; refresh the
                        # master's own presence on join/leave.
                        _emit_master_presence(count, manager)
                    # "connected" means an active peer link: a lone serving
                    # master is "waiting" for a device to connect.
                    set_link_status(_p2p_status(count))

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
                    # Back to the rendezvous loop: the link status drops to
                    # "connecting" until the new master link is established.
                    set_link_status("connecting")
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
                        # The condition is standing: its retraction (when an
                        # election gives the group a primary again) must be
                        # publishable.
                        note_condition_standing("master")
                        no_master_warned = True
                else:
                    no_master_since = None
                    if no_master_warned:
                        # The group has a primary again: retract the warning so a
                        # front-end never reports a condition that is gone.
                        emit_warning_cleared("master")
                    no_master_warned = False

                if not seeder_attempted:
                    inventory_publisher = _start_slave_inventory(config, p2p_sync, log, p2p_server)
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

    except KeyboardInterrupt:
        log.info("Shutting down P2P mode...")
    except RuntimeError as exc:
        # An interrupted shutdown: the stop signal (SIGTERM -> KeyboardInterrupt)
        # lands while asyncio is tearing the loop down, which asyncio reports as
        # "This event loop is already running" / "Cannot close a running event
        # loop". That is the tail of a clean stop, not a failure: log the stop
        # and let the ``finally`` release the servers. Any other RuntimeError is
        # a real defect and keeps propagating.
        if not is_event_loop_teardown_error(exc):
            raise
        log.info("Shutting down P2P mode...")
    finally:
        if master_channel is not None:
            master_channel.close()
        _stop_p2p_serving(p2p_ws_server, p2p_server)
        _close_relay_dialer()
        _stop_slave_inventory(
            p2p_sync,
            inventory_publisher,
            serving=bool(config.get("P2P_PEER_DOWNLOAD", True)),
        )
        p2p_server.stop()
        p2p_sync.stop()

    return last_exit


def _run_as_master(
    config: "HsyncConfig",
    log,
    p2p_sync: RendezvousPlan,
    p2p_server: P2PServer,
    p2p_ws_server: P2PWebSocketServer,
    cid_registry: SlaveCidRegistry | None = None,
) -> tuple[P2PServer, P2PWebSocketServer]:
    """Run as master (sync coordinator).

    The master role is inherently continuous: the master must stay available to
    serve any peer that joins. The one-shot flag only affects slave behaviour
    (one-shot vs. continuous sync); the master loop always yields back to the
    outer rendezvous loop after a brief idle window so master/peer election can
    be re-evaluated.

    The process-wide listener is reused: installing the master role swaps the
    serving context in place (no rebind). Returns the (p2p_server,
    p2p_ws_server) tuple so the caller keeps the same instances across rendezvous
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
    # (the rendezvous loop re-enters this function roughly once a second while the
    # node stays master). Recreating them would leak a SQLite handle and wipe the
    # live inventory sessions/holders with ``activate()``.
    inventory_store: P2PInventoryStore | None
    source_service: P2PSourceService | None
    if first_activation:
        # The inventory store is the master's classification input, not only its
        # serving source: every master keeps it so it can report the sync status
        # of connected devices even when it does not serve blocks.
        inventory_store = P2PInventoryStore.for_bucket(config["REMOTE_BUCKET"])
        inventory_store.activate()
        source_service = (
            P2PSourceService(
                bucket=config["REMOTE_BUCKET"],
                master_cid=config["CID"],
                inventory_store=inventory_store,
                peer_snapshot_provider=peer_manager.get_alive_peer_snapshots,
            )
            if config.get("P2P_PEER_DOWNLOAD", True)
            else None
        )
    else:
        inventory_store = p2p_server.inventory_store
        source_service = p2p_server.source_service

    # Wire the WebSocket-authoritative liveness signal on every master start: a
    # re-election may install a fresh PeerManager, so the callback must never
    # stay bound to a stale one.
    def _on_peer_connection(cid: str, connected: bool) -> None:
        """Feed the shared liveness signal; pull a sample on admission.

        The admission is proof the peer exists, but the shared peer state is fed
        by rendezvous *samples*: without the pull the new peer stays invisible
        (absent from the group view, the election and the peer list) until the
        lease cadence's next query, i.e. up to the refresh interval.
        """
        if not connected:
            peer_manager.mark_peer_disconnected(cid)
            return
        peer_manager.mark_peer_connected(cid)
        p2p_sync.request_sample()

    p2p_ws_server.set_peer_connection_callback(_on_peer_connection)
    # The receiver owns the truth about an upload: publish the bytes this master
    # stored on the uploader's own session, so the sender's row shows the
    # delivery instead of its own transport counter.
    p2p_server.set_progress_echo(p2p_ws_server.send_to_sync)
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
    # here on the next rendezvous iteration.
    if not p2p_ws_server.running:
        try:
            p2p_ws_server.start(watch_path=config["LOCAL_BUCKET"])
        except WatcherStartError:
            # The master watcher could not start (inotify kernel limits). The
            # parsable [WATCHER ERROR] line was already logged at the watcher
            # start site. Back off: without the watcher the master cannot serve
            # realtime events, but the retry must be slow because a kernel limit
            # never clears at the rendezvous cadence. server_ready is NOT
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
        set_link_status(_p2p_status(p2p_ws_server.connected_clients))

        if config["P2P_KEY"]:
            log.info("Authentication enabled (shared secret)")
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
        # Rendezvous plan only: the master is reachable across networks only once
        # its endpoint has been published to the rendezvous (UPnP ok or public IP
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

    # Re-assert the relay listener on every master re-entry, not only on the
    # first activation: the acceptor holds its own QUIC control connection to the
    # rendezvous, so a dropped connection (a mobile-network blip, a node restart,
    # an idle-timeout sweep) leaves the master serving with no relay path. Without
    # this re-assertion every relay-only slave — the two peers behind
    # carrier-grade NAT this feature exists for — gets ``relay_unavailable`` and
    # stays stuck on "waiting" forever while a directly reachable peer still
    # works. The call is idempotent: it returns while the acceptor is alive and
    # backs off ``RELAY_RETRY_BACKOFF_S`` after a failed attempt, so the
    # one-second re-entry cannot flood the node (mDNS-only plans no-op).
    if peer_manager.is_master():
        _start_master_relay(p2p_sync, log)

    if first_activation:
        log.info("Press Ctrl+C to stop...")
    # Yield back to the outer rendezvous loop after a short idle window
    # so master/peer election can be re-evaluated.  If we are still
    # master on the next iteration, the outer loop will call us again
    # and reuse the existing server instances.
    time.sleep(1)

    return p2p_server, p2p_ws_server


def _run_as_slave(
    config: "HsyncConfig",
    log,
    one_shot: bool,
    peer_manager,
    wait_indefinitely_for_peer: bool = False,
    inventory_publisher: P2PInventoryPublisher | None = None,
    notified: dict[tuple[str, str], str] | None = None,
    plan: RendezvousPlan | None = None,
) -> tuple[bool, int]:
    """Run as client (connect to master).

    Returns a ``(continue_loop, exit_code)`` tuple: ``continue_loop`` tells
    the rendezvous loop whether to keep running, ``exit_code`` is the process
    exit status to report when the loop terminates.
    """
    from asyncio import run

    # Every attempt starts with a clean transport choice: a tunnel opened for the
    # previous session (or a previous master) must not outlive it.
    _close_relay_dialer()
    master = peer_manager.get_master()
    if master:
        # Wait for master to be ready (server_ready=true in mDNS)
        if not master.server_ready:
            log.info(f"Waiting for master {master.cid} to start server...")
            return True, int(ExitCode.OK)  # Continue rendezvous loop

        # Secure-only P2P: a master must serve over HTTPS and advertise a
        # certificate fingerprint we can pin. Anything else is refused; there is
        # no plain-HTTP downgrade path. We do NOT self-promote on refusal (that
        # would create a second master and split-brain); we stay in the
        # rendezvous loop and warn.
        if not (master.use_https and master.cert_fingerprint):
            log.warning(
                f"{ActionLabel.INSECURE_MASTER} master {master.cid} does not advertise an HTTPS "
                f"endpoint with a certificate fingerprint. Refusing to connect "
                f"(P2P transport is HTTPS-only)."
            )
            # A refusal, not a failure: without this the user only sees "no
            # device found" and cannot tell a missing peer from one the client
            # deliberately refused for security reasons.
            emit_event(
                UiEventName.security_rejected,
                op="connect",
                path=master.cid,
                message=(
                    "A device was refused because it does not serve HTTPS with a "
                    "certificate fingerprint"
                ),
            )
            return (not one_shot) or wait_indefinitely_for_peer, int(ExitCode.OK)

        if not _evaluate_master_pin(config, master, log, notified=notified):
            # Stay in the rendezvous loop (continuous) rather than connecting to
            # a master whose identity can no longer be verified or which has not
            # yet been paired out of band.
            return (not one_shot) or wait_indefinitely_for_peer, int(ExitCode.OK)
        log.info(f"Master using HTTPS, fingerprint: {master.cert_fingerprint[:32]}...")
        config["MASTER_CERT_FINGERPRINT"] = master.cert_fingerprint

        # Pick the reachable data-plane endpoint: a rendezvous peer advertises
        # several candidates, so the local one is preferred over the NATed
        # public one. A single-candidate peer keeps its endpoint unchanged.
        endpoint = _select_master_endpoint(master, log)
        relayed = False
        if endpoint is None:
            endpoint = _relay_master_endpoint(plan, master, log, peer_manager)
            relayed = endpoint is not None
        if endpoint is None:
            log.warning(f"No reachable endpoint for master {master.cid}; retrying rendezvous.")
            return (not one_shot) or wait_indefinitely_for_peer, int(ExitCode.OK)
        master_host, master_port = endpoint
        if not relayed:
            # The master answered on a direct candidate again: the "no data relay"
            # problem (its premise was "unreachable directly") no longer holds.
            emit_warning_cleared("relay")

        # `master_url` is the transport address (loopback when relayed) and is
        # what the HTTP/WS client, the source resolver and the transfer core use.
        # What the operator and the UI see stays the master's own address: a
        # ``127.0.0.1`` row in the status bar would be meaningless.
        master_url = server_url(master_host, master_port, use_https=master.use_https)
        display_url = (
            master_url
            if not relayed
            else server_url(master.address, master.port, use_https=master.use_https)
        )
        log.info(f"Connecting to master: {master.cid} at {display_url}")
        emit_event(UiEventName.server, url=display_url)

        config["API"]["buckets"] = f"{master_url}/api/v1/buckets"
        ws_master_url = http_to_ws(master_url)
        config["API"]["events"] = f"{ws_master_url}/api/v1/buckets"

        if config["P2P_KEY"]:
            config["REQUEST_HEADERS"]["Authorization"] = f"Bearer {config['P2P_KEY']}"

        # Set compression capability based on master peer
        config["SERVER_COMPRESSION_ENABLED"] = master.compression_enabled
        config["SERVER_DELTA_ENABLED"] = master.delta_enabled

        set_config(config)

        if relayed:
            _apply_relay_transfer_tuning(config, log)

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
        # startup probe is skipped when only the placeholder URL is known). A
        # relayed master is reached over loopback, so its measured RTT says
        # nothing about the real two-leg path: keep the current tuning instead of
        # tightening timeouts around a localhost round trip.
        if not relayed:
            # A previously relayed peer reached directly again: lift the relay's
            # concurrency cap before the RTT refresh (which re-derives it only
            # when it manages the key).
            _restore_relay_transfer_tuning(config, log)
            _refresh_p2p_network_tuning(master, config, log, host=master_host, port=master_port)

        exit_code = int(ExitCode.OK)
        if config["DRYRUN"]:
            # The master may have exited just before this iteration: its peer
            # record can still look fresh, and the automatic preview would run
            # against a dead endpoint, burning the full retry budget and
            # surfacing a spurious error notification. Probe the data endpoint
            # first; when it fails, demote the master and go back to rendezvous
            # so the election can pick a reachable node.
            if not _master_endpoint_reachable(master_host, master_port):
                log.warning(
                    f"Master {master.cid} at {master_host}:{master_port} is "
                    "unreachable; skipping the preview and re-entering rendezvous."
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
            # stdin EOF) and the node returns to the rendezvous loop.
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

                applied = run(
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
            if not applied:
                # The command channel ended without applying the plan (stdin
                # closed, or it was never a pipe/terminal). Falling through
                # would open the live session with DRYRUN still set, and the
                # event path is not gated on it, so a "preview only" run could
                # write files. Stop instead: nothing can command this process.
                log.info("Dry-run finished without applying a plan; stopping.")
                peer_manager.mark_peer_disconnected(master.cid)
                return False, int(ExitCode.OK)
            # The plan was applied: this process is a live engine now. The SYNC
            # handler cleared DRYRUN on the global CONFIG only, while the
            # rendezvous loop re-reads this local ``config`` on every iteration
            # (it is a copy, not the same object), so after a network drop the
            # reconnected run fell back into the preview branch and parked
            # without reopening the WebSocket or the watcher.
            config["DRYRUN"] = False
        else:
            # Perform initial sync (the master is the transport coordinator).
            # The HTTPS data plane itself proves the master is reachable: credit
            # it for presence now, otherwise the UI sits on "1/N" for the whole
            # initial sync (the WebSocket — and the master's advertised client
            # count — only come up afterwards). Cleared again if the sync fails.
            peer_manager.mark_peer_connected(master.cid)
            _emit_slave_presence(peer_manager)

            def _peer_directions() -> list[tuple[str, str]]:
                """Every other device in the group and the direction it advertises.

                Feeds the upload-only report on ``sync_complete``: the master is
                the destination and always stores an upload, so it is left out
                (its acceptance is counted by the caller), while a slave that is
                itself upload-only will ignore the file.
                """
                return [
                    (peer.cid, peer.sync_direction)
                    for peer in peer_manager.get_alive_peers()
                    if peer.cid not in (master.cid, CONFIG.get("CID"))
                ]

            try:
                log_initial_sync_start()
                if inventory_publisher is None:
                    result = run(
                        sync(
                            p2p_mode=True,
                            source_resolver=source_resolver,
                            reconcile_dirs=True,
                            peer_direction_provider=_peer_directions,
                        )
                    )
                else:
                    result = run(
                        sync(
                            p2p_mode=True,
                            inventory_observer=inventory_publisher,
                            source_resolver=source_resolver,
                            reconcile_dirs=True,
                            peer_direction_provider=_peer_directions,
                        )
                    )
            except (AuthenticationError, NetworkError, ServerError):
                # sync() already logged the failure and emitted the structured
                # event. A one-shot run maps a transport failure to a fatal
                # exit; a continuous run must survive a transient failure (e.g.
                # the master going offline mid-sync) and re-enter the rendezvous
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
        # Refresh our own presence now the link is live: the rendezvous loop that
        # owns the ``peers`` event is blocked for the whole WebSocket run, so
        # without this a connected slave keeps the pre-connect ``1/N`` pair.
        _emit_slave_presence(peer_manager)
        # Watch the master's advertised endpoint for the whole session: a master
        # that moves networks must not keep being dialed at its stale address. A
        # relayed session is reached through a stable loopback tunnel, so its
        # advertised endpoint changing is not a reason to reconnect.
        endpoint_guard: _MasterEndpointGuard | None = None
        if not relayed:
            endpoint_guard = _MasterEndpointGuard(
                peer_manager,
                master.cid,
                address=master.address,
                port=master.port,
                endpoints=master.endpoints,
            )
            endpoint_guard.start()
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
                        endpoint_guard=endpoint_guard,
                    ),
                    inventory_observer=inventory_publisher,
                    source_resolver=source_resolver,
                )
            )
        except WatcherStartError:
            # The slave's local watcher could not start (inotify kernel
            # limits). The parsable [WATCHER ERROR] line and remediation hint
            # were already logged at the watcher start site. Re-entering the
            # rendezvous loop at its normal cadence would immediately retry
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
        finally:
            if endpoint_guard is not None:
                endpoint_guard.close()
        # The WebSocket is down: clear the liveness mark so a master that died
        # mid-session is demoted by the sweep below, then re-enter rendezvous.
        peer_manager.mark_peer_disconnected(master.cid)
        # If WebSocket disconnects, re-enter rendezvous loop. Expire any peer
        # whose heartbeat already stopped so a master that died mid-session is
        # demoted immediately instead of waiting for the next rendezvous wake:
        # the surviving peers then elect the pre-agreed successor. A master
        # whose record is still fresh (a local link blip) is left untouched,
        # so a transient drop never triggers a needless takeover/split-brain.
        peer_manager.reconcile_stale_peers()
        log.info("WebSocket disconnected, re-entering rendezvous loop...")
        return True, int(ExitCode.OK)
    # No master and no peers
    if (not one_shot) or wait_indefinitely_for_peer:
        log.info("Waiting for peers to join...")
        return True, int(ExitCode.OK)  # Continue the rendezvous loop
    log.warning("No master elected - this client is alone, nothing to sync")
    emit_event(UiEventName.no_peers, message=NO_PEERS_MESSAGE)
    return False, int(ExitCode.P2P_NO_PEER)
