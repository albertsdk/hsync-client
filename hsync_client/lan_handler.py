# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
LAN Mode Handler for HSYNC Client.

This module handles LAN P2P synchronization including peer discovery,
master election, and WebSocket communication.
"""

import time
from re import sub
from typing import TYPE_CHECKING, Any, cast

from hsync_common.protocol.pairing import (
    PinDecision,
    evaluate_pin,
    requires_pairing_confirmation,
)
from hsync_common.text import safe_json_for_log

from hsync_client.autotune import classify_network, derive_retry, derive_timeout, measure_rtt
from hsync_client.config import CONFIG, set_config
from hsync_client.constants import (
    DISCOVERY_POLL_INTERVAL,
    HTTP_TIMEOUT_MAX,
    HTTP_TIMEOUT_MIN,
    RECONNECT_DELAY_MAX,
    RECONNECT_DELAY_MIN,
    ActionLabel,
)
from hsync_client.events import UiEventName, emit_event
from hsync_client.exitcodes import ExitCode, exit_code_for_result
from hsync_client.lan import LANSync
from hsync_client.lan_server import LANServer
from hsync_client.lan_websocket import LANWebSocketServer, lan_websocket_client
from hsync_client.pinning import default_pin_store
from hsync_client.preview import _run_preview, _run_stdin_wait, _with_stdin_commands
from hsync_client.sync import SyncCycleResult, sync

if TYPE_CHECKING:
    from hsync_client.config import HsyncConfig


# Seconds to wait before warning that no master has been elected (e.g. when every
# peer in the group is configured with --never-master). Emitted as a parsable
# ``[NO MASTER]`` warning so front-ends (desktop UI) can surface a notification.
NO_MASTER_WARNING_TIMEOUT = 60.0


def _refresh_lan_network_tuning(master, config: "HsyncConfig", log) -> None:
    """Re-derive autotune-managed network tuning from the real master RTT.

    In LAN mode with the placeholder URL the startup RTT probe is skipped (no
    server is known yet), so the tuning profile starts at SAFE defaults. Once
    the master is discovered there is a real endpoint to measure. Only keys in
    ``AUTOTUNE_MANAGED_KEYS`` (no CLI/env override) are re-derived, preserving
    the CLI > env > autotune precedence.

    Updates are applied to both the global ``CONFIG`` and the local *config*
    dict: they are distinct objects (``set_config`` copies values) so callers
    must invoke this after ``set_config(config)`` to keep them in sync.
    """
    if not config.get("AUTOTUNE_ENABLED"):
        return

    managed = set(config.get("AUTOTUNE_MANAGED_KEYS", []))
    if not managed:
        log.debug("autotune (LAN): no autotune-managed keys (all overridden via CLI/env)")
        return

    rtt_ms = measure_rtt(master.address, master.port)
    if rtt_ms is None:
        log.debug("autotune (LAN): RTT to master not measurable; keeping startup tuning")
        return

    net_class = classify_network(rtt_ms)
    updates: dict[str, int | float] = {}

    if "HTTP_TIMEOUT" in managed:
        updates["HTTP_TIMEOUT"] = derive_timeout(
            rtt_ms, net_class, HTTP_TIMEOUT_MIN, HTTP_TIMEOUT_MAX
        )
    if managed & {"TRANSFER_RETRY_MIN_WAIT", "TRANSFER_RETRY_MAX_WAIT", "RECONNECT_DELAY"}:
        min_wait, max_wait, reconnect = derive_retry(
            rtt_ms, net_class, RECONNECT_DELAY_MIN, RECONNECT_DELAY_MAX
        )
        if "TRANSFER_RETRY_MIN_WAIT" in managed:
            updates["TRANSFER_RETRY_MIN_WAIT"] = float(min_wait)
        if "TRANSFER_RETRY_MAX_WAIT" in managed:
            updates["TRANSFER_RETRY_MAX_WAIT"] = float(max_wait)
        if "RECONNECT_DELAY" in managed:
            updates["RECONNECT_DELAY"] = reconnect

    if not updates:
        return

    for key, value in updates.items():
        cast(dict[str, Any], CONFIG)[key] = value
        cast(dict[str, Any], config)[key] = value
    log.info(
        f"autotune (LAN): rtt={rtt_ms:.1f}ms net={net_class.value} -> {safe_json_for_log(updates)}"
    )


def run_lan_mode(config: "HsyncConfig", log) -> int:  # noqa: C901
    """
    Handle LAN P2P mode synchronization.

    This function orchestrates peer discovery, master election,
    and either runs as master or connects to a master peer.

    Args:
        config: The hsync configuration dictionary.
        log: Logger instance for messages.

    Returns:
        The process exit code for the run: a completed one-shot slave
        sync maps its verdict through the documented code table, no
        discovered peer yields LAN_NO_PEER, and an interrupted run
        yields OK.
    """
    log.info("LAN P2P mode enabled")

    # Clock offset (ADR-0023): in LAN mode no server is known at startup, so
    # the public SNTP hosts (HSYNC_NTP_HOSTS) are the time authority. Failures
    # degrade to offset 0 and never block discovery or synchronisation.
    from hsync_client.timesync import probe_ntp_clock_offset

    probe_ntp_clock_offset(logger=log)

    persistent = config.get("LAN_PERSISTENT", False)
    if persistent:
        log.info("Persistent mode enabled - will wait indefinitely for peers")

    discovery_timeout = config["DISCOVERY_TIMEOUT"]
    wait_indefinitely_for_peer = discovery_timeout == 0
    discovery_wait = DISCOVERY_POLL_INTERVAL if wait_indefinitely_for_peer else discovery_timeout
    if wait_indefinitely_for_peer and not persistent:
        log.info("Discovery timeout disabled - waiting indefinitely for the first LAN peer/master")

    from hsync_client.identity import load_or_create_device_identity

    # Reuses the device identity resolved at startup (cached): the keypair
    # presented to peers always belongs to this installation's device.
    cert_info = load_or_create_device_identity().cert_info
    log.info(f"Certificate fingerprint: {cert_info.fingerprint[:32]}...")

    lan_sync = LANSync(
        cid=config["CID"],
        bucket=config["REMOTE_BUCKET"],
        port=config["LAN_PORT"],
        group=config["LAN_GROUP"],
        use_https=True,
        cert_fingerprint=cert_info.fingerprint,
        force_master=config["LAN_FORCE_MASTER"],
        never_master=config["LAN_NEVER_MASTER"],
        sync_direction=config["SYNC_DIRECTION"],
    )
    if config["LAN_NEVER_MASTER"]:
        log.info("Never-master mode enabled - this client will always remain a client/slave")
    # Start discovery and register service for peer discovery
    peer_manager = lan_sync.start(register_service=True)

    lan_server = None
    lan_ws_server = None

    last_exit = int(ExitCode.OK)

    # Track how long the group has been without an elected master so we can warn
    # once after NO_MASTER_WARNING_TIMEOUT (e.g. when every peer is never-master).
    no_master_since: float | None = None
    no_master_warned = False

    try:
        while True:
            # Wait for peer discovery
            lan_sync.wait_for_discovery(timeout=discovery_wait)

            if peer_manager.is_master():
                no_master_since = None
                no_master_warned = False
                lan_server, lan_ws_server = _run_as_master(
                    config=config,
                    log=log,
                    lan_sync=lan_sync,
                    cert_info=cert_info,
                    persistent=persistent,
                    lan_server=lan_server,
                    lan_ws_server=lan_ws_server,
                )
                # Wire the master's live WebSocket client count into discovery
                # presence: the provider feeds wait_for_discovery (the single
                # owner of the peers event), and the callback refreshes this
                # node's mDNS ``connected`` TXT value on every join/leave.
                if lan_ws_server is not None:
                    ws_server = lan_ws_server

                    def ws_connected_count(server: LANWebSocketServer = ws_server) -> int:
                        return server.connected_clients

                    lan_sync.set_connected_clients_provider(ws_connected_count)
                    lan_ws_server.set_clients_changed_callback(lan_sync.set_connected_clients)
                else:
                    lan_sync.set_connected_clients_provider(None)
                # Check if we're still master after _run_as_master returns
                if not peer_manager.is_master():
                    if lan_server:
                        lan_server.stop()
                        lan_server = None
                    if lan_ws_server:
                        lan_ws_server.stop()
                        lan_ws_server = None
                    lan_sync.set_connected_clients_provider(None)
                # NOTE: the master role is inherently continuous: it must
                # stay available to serve peers regardless of the
                # ``persistent`` flag (which only applies to slaves).  The
                # loop only exits via KeyboardInterrupt or by losing the
                # master role and being elected slave on a later iteration.
            else:
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

                should_continue, exit_code = _run_as_slave(
                    config=config,
                    log=log,
                    lan_sync=lan_sync,
                    cert_info=cert_info,
                    persistent=persistent,
                    peer_manager=peer_manager,
                    wait_indefinitely_for_peer=wait_indefinitely_for_peer,
                )
                last_exit = exit_code
                if not should_continue:
                    break

    except KeyboardInterrupt:
        log.info("Shutting down LAN mode...")
    finally:
        if lan_server:
            lan_server.stop()
        if lan_ws_server:
            lan_ws_server.stop()
        lan_sync.stop()

    return last_exit


def _run_as_master(
    config: "HsyncConfig",
    log,
    lan_sync: LANSync,
    cert_info,
    persistent: bool,
    lan_server,
    lan_ws_server,
) -> tuple[LANServer | None, LANWebSocketServer | None]:
    """Run as master (sync coordinator).

    The master role is inherently continuous: the master must stay
    available to serve any peer that joins.  The ``persistent`` flag
    only affects slave behaviour (one-shot vs. continuous sync) and is
    accepted here for API symmetry with :func:`_run_as_slave` but does
    not change the master loop, which always yields back to the outer
    discovery loop after a brief idle window so master/peer election
    can be re-evaluated.

    Returns the (lan_server, lan_ws_server) tuple so the caller can persist
    the running server instances across discovery loop iterations and avoid
    re-binding the same port on subsequent iterations.
    """
    del persistent  # master role is always continuous; flag kept for API symmetry

    # The discovery loop re-enters this function on every iteration while we
    # remain master. ``lan_server is None`` is true only on the first call
    # (or after we lost and re-won the role, which stops the servers), so it
    # is the natural gate for the one-shot startup messages and avoids
    # spamming the role/Ctrl+C notices once per second.
    first_activation = lan_server is None

    if first_activation:
        log.info("This client is the MASTER - acting as sync coordinator")

    # Start HTTPS server if not already running (LAN transport is HTTPS-only)
    if lan_server is None:
        lan_server = LANServer(
            port=lan_sync.port,
            use_https=True,
            cert_file=cert_info.cert_file,
            key_file=cert_info.key_file,
        )
        lan_server.start()
        log.info(f"Master HTTP server on port {lan_sync.port} (HTTPS)")

    # Start WebSocket server if not already running
    if lan_ws_server is None:
        ws_port = lan_sync.port + 1  # Use next port for WebSocket
        lan_ws_server = LANWebSocketServer(
            port=ws_port,
            use_https=True,
            cert_file=cert_info.cert_file,
            key_file=cert_info.key_file,
        )
        lan_ws_server.start(watch_path=config["LOCAL_BUCKET"])
        log.info(f"Master WebSocket server on port {ws_port} (WSS)")

        if config["LAN_KEY"]:
            log.info("Authentication enabled (HSYNC_LAN_KEY)")
        else:
            log.info("No authentication (trust LAN mode)")

        # Update mDNS service to indicate server is ready
        lan_sync.update_server_ready()
        from hsync_client.utils import _write_ready_file as _signal_ready

        _signal_ready()

    if first_activation:
        log.info("Press Ctrl+C to stop...")
    # Yield back to the outer discovery loop after a short idle window
    # so master/peer election can be re-evaluated.  If we are still
    # master on the next iteration, the outer loop will call us again
    # and reuse the existing server instances.
    time.sleep(1)

    return lan_server, lan_ws_server


def _evaluate_master_pin(config: "HsyncConfig", master, log) -> bool:
    """TOFU-pin the master's certificate; return whether the connection may proceed.

    The trust decisions are the shared ones in
    :mod:`hsync_common.protocol.pairing`; here we only persist the pin and surface
    structured security events so a front-end can prompt the user instead of
    silently trusting a certificate.

    Two cases refuse the connection (return ``False``):

    * **Strict pairing** (``LAN_REQUIRE_PAIRING``): a first-seen certificate is
      not auto-pinned. We emit ``pairing_needed`` and refuse until the operator
      confirms the peer out of band (by pinning it or disabling strict pairing).
    * **Fingerprint changed**: the certificate no longer matches the pin. We log
      a parsable ``[FINGERPRINT CHANGED]`` line, emit ``fingerprint_changed`` and
      refuse (possible man-in-the-middle).
    """
    store = default_pin_store()
    bucket = config["REMOTE_BUCKET"]

    if config.get("LAN_REQUIRE_PAIRING", False):
        first_seen = evaluate_pin(store.get(master.cid, bucket), master.cert_fingerprint)
        if requires_pairing_confirmation(first_seen, require_pairing=True):
            log.error(
                f"{ActionLabel.PAIRING_NEEDED} master {master.cid} at {master.address} presented an "
                f"unpinned certificate and strict pairing is enabled. Refusing to connect; "
                f"confirm this peer out of band (pin it) to trust it."
            )
            emit_event(
                UiEventName.pairing_needed,
                peer_cid=master.cid,
                peer_address=master.address,
                fingerprint=master.cert_fingerprint,
                message="LAN master requires explicit pairing confirmation",
            )
            return False

    decision = store.check_and_record(master.cid, bucket, master.cert_fingerprint)
    if decision is PinDecision.changed:
        log.error(
            f"{ActionLabel.FINGERPRINT_CHANGED} master {master.cid} at {master.address} presented a "
            f"certificate that does not match the pinned one. Refusing to connect "
            f"(possible man-in-the-middle). Remove the stale pin to re-trust this peer."
        )
        emit_event(
            UiEventName.fingerprint_changed,
            peer_cid=master.cid,
            peer_address=master.address,
            fingerprint=master.cert_fingerprint,
            message="LAN master certificate fingerprint changed",
        )
        return False
    if decision is PinDecision.trusted:
        log.info(f"Pinned LAN master certificate for {master.cid} (trust on first use)")
    return True


def _run_as_slave(
    config: "HsyncConfig",
    log,
    lan_sync: LANSync,
    cert_info,
    persistent: bool,
    peer_manager,
    wait_indefinitely_for_peer: bool = False,
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

        # Secure-only LAN: a master must serve over HTTPS and advertise a
        # certificate fingerprint we can pin. Anything else is refused; there is
        # no plain-HTTP downgrade path. We do NOT self-promote on refusal (that
        # would create a second master and split-brain); we stay in the
        # discovery loop and warn.
        if not (master.use_https and master.cert_fingerprint):
            log.warning(
                f"{ActionLabel.INSECURE_MASTER} master {master.cid} does not advertise an HTTPS "
                f"endpoint with a certificate fingerprint. Refusing to connect "
                f"(LAN transport is HTTPS-only)."
            )
            return persistent or wait_indefinitely_for_peer, int(ExitCode.OK)

        master_url = master.get_server_url()
        log.info(f"Connecting to master: {master.cid} at {master_url}")
        emit_event(UiEventName.server, url=master_url)

        if not _evaluate_master_pin(config, master, log):
            # Stay in the discovery loop (persistent) rather than connecting to
            # a master whose identity can no longer be verified or which has not
            # yet been paired out of band.
            return persistent or wait_indefinitely_for_peer, int(ExitCode.OK)
        log.info(f"Master using HTTPS, fingerprint: {master.cert_fingerprint[:32]}...")
        config["MASTER_CERT_FINGERPRINT"] = master.cert_fingerprint

        config["API"]["buckets"] = f"{master_url}/api/v1/buckets"
        ws_master_url = sub(r"^https:", "wss:", master_url)
        config["API"]["events"] = f"{ws_master_url}/api/v1/buckets"

        if config["LAN_KEY"]:
            config["REQUEST_HEADERS"]["Authorization"] = f"Bearer {config['LAN_KEY']}"

        # Set compression capability based on master peer
        config["SERVER_COMPRESSION_ENABLED"] = master.compression_enabled
        config["SERVER_DELTA_ENABLED"] = master.delta_enabled

        set_config(config)

        # Re-derive autotune-managed tuning from the real master RTT (the LAN
        # startup probe is skipped when only the placeholder URL is known)
        _refresh_lan_network_tuning(master, config, log)

        # Check if dry-run mode is enabled
        exit_code = int(ExitCode.OK)
        if config["DRYRUN"]:
            # In dry-run mode, only show preview without syncing
            _run_preview(log)
            if not persistent:
                return False, exit_code
            # In persistent mode with dry-run, wait for SYNC/PREVIEW commands
            log.info("Dry-run mode: waiting for commands (stdin) or Ctrl+C to exit...")
            run(_run_stdin_wait(log))
        else:
            # Perform initial sync (the master is the transport coordinator)
            try:
                result = run(sync(lan_mode=True))
            except Exception:
                # sync() already logged the fatal error and emitted the
                # structured event; fail the run without a raw traceback.
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

        if not persistent:
            return False, exit_code

        # In persistent mode, connect via WebSocket for real-time events
        log.info("Initial sync complete, connecting to WebSocket for real-time events...")
        ws_port = master.port + 1  # WebSocket is on next port
        try:
            run(
                _with_stdin_commands(
                    log,
                    lan_websocket_client(
                        host=master.address,
                        port=ws_port,
                        use_https=True,
                    ),
                )
            )
        except Exception as e:
            log.warning(f"WebSocket connection ended: {type(e).__name__}: {e}")
        # If WebSocket disconnects, re-enter discovery loop
        log.info("WebSocket disconnected, re-entering discovery loop...")
        return True, int(ExitCode.OK)
    else:
        # No master and no peers
        if persistent or wait_indefinitely_for_peer:
            log.info("Waiting for peers to join...")
            return True, int(ExitCode.OK)  # Continue the discovery loop
        else:
            log.warning("No master elected - this client is alone, nothing to sync")
            return False, int(ExitCode.LAN_NO_PEER)
