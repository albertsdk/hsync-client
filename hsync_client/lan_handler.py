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
from typing import TYPE_CHECKING

from hsync_client.config import set_config
from hsync_client.lan import CLOCK_SKEW_THRESHOLD, LANSync
from hsync_client.lan_server import LANServer
from hsync_client.lan_websocket import LANWebSocketServer, lan_websocket_client
from hsync_client.sync import sync

if TYPE_CHECKING:
    from hsync_client.config import HsyncConfig


# Seconds to wait before warning that no master has been elected (e.g. when every
# peer in the group is configured with --never-master). Emitted as a parsable
# ``[NO_MASTER]`` warning so front-ends (desktop UI) can surface a notification.
NO_MASTER_WARNING_TIMEOUT = 60.0


def _measure_clock_offset(master, config: "HsyncConfig", log) -> None:
    """
    Measure clock offset between this client and the master using round-trip HTTP.

    Uses an NTP-like round-trip measurement:
        T1 = local time before request
        T2 = master time (from /api/v1/time response)
        T3 = local time after response
        offset = T2 - (T1 + RTT/2), where RTT = T3 - T1

    Args:
        master: The master Peer object.
        config: Current hsync configuration.
    """
    from hsync_common.clock import measure_clock_offset
    from requests import RequestException
    from requests import get as http_get

    from hsync_client.utils import get_ssl_verify

    time_url = f"{master.get_server_url()}/api/v1/time"

    try:
        result = measure_clock_offset(
            time_url,
            http_get=http_get,
            headers={k: str(v) for k, v in config["REQUEST_HEADERS"].items()},
            verify=get_ssl_verify(),
            timeout=config.get("HTTP_TIMEOUT", 30),
        )
        if result is None:
            log.debug("Clock sync: /api/v1/time unavailable or missing timestamp, skipping")
            return

        offset, rtt = result
        master.clock_offset = offset

        if abs(offset) > CLOCK_SKEW_THRESHOLD:
            log.warning(
                f"Clock skew detected with master {master.cid}: "
                f"offset={offset:.1f}s, RTT={rtt * 1000:.1f}ms. "
                f"File comparison will use SHA-256 fallback for "
                f"mtime differences within tolerance."
            )
        else:
            log.debug(
                f"Clock sync with master {master.cid}: offset={offset:.3f}s, RTT={rtt * 1000:.1f}ms"
            )

    except RequestException as e:
        log.debug(f"Clock sync failed: {e}")
    except (ValueError, KeyError) as e:
        log.debug(f"Clock sync: invalid response: {e}")


def _run_preview(log) -> None:
    """Run a preview (dry-run) showing pending sync actions."""
    from hsync_client.main_logic import _run_preview as run_preview_impl

    run_preview_impl(log)


if TYPE_CHECKING:
    from hsync_client.config import HsyncConfig


def run_lan_mode(config: "HsyncConfig", log) -> None:  # noqa: C901
    """
    Handle LAN P2P mode synchronization.

    This function orchestrates peer discovery, master election,
    and either runs as master or connects to a master peer.

    Args:
        config: The hsync configuration dictionary.
        log: Logger instance for messages.
    """
    log.info("LAN P2P mode enabled")
    persistent = config.get("LAN_PERSISTENT", False)
    if persistent:
        log.info("Persistent mode enabled - will wait indefinitely for peers")

    cert_info = None
    if config["LAN_HTTPS"]:
        from hsync_client.ssl_utils import generate_self_signed_cert, save_cert_to_temp_files

        log.info("Generating self-signed certificate for HTTPS...")
        cert_info = generate_self_signed_cert()
        cert_info = save_cert_to_temp_files(cert_info)
        log.info(f"Certificate fingerprint: {cert_info.fingerprint[:32]}...")

    lan_sync = LANSync(
        cid=config["CID"],
        bucket=config["REMOTE_BUCKET"],
        port=config["LAN_PORT"],
        group=config["LAN_GROUP"],
        use_https=config["LAN_HTTPS"],
        cert_fingerprint=cert_info.fingerprint if cert_info else None,
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

    # Track how long the group has been without an elected master so we can warn
    # once after NO_MASTER_WARNING_TIMEOUT (e.g. when every peer is never-master).
    no_master_since: float | None = None
    no_master_warned = False

    try:
        while True:
            # Wait for peer discovery
            lan_sync.wait_for_discovery(timeout=config["DISCOVERY_TIMEOUT"])

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
                # Check if we're still master after _run_as_master returns
                if not peer_manager.is_master():
                    if lan_server:
                        lan_server.stop()
                        lan_server = None
                    if lan_ws_server:
                        lan_ws_server.stop()
                        lan_ws_server = None
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
                            f"[NO_MASTER] No master elected after "
                            f"{int(now - no_master_since)}s. All discovered peers "
                            f"may be configured with --never-master; no node will "
                            f"serve files and synchronization cannot proceed."
                        )
                        no_master_warned = True
                else:
                    no_master_since = None
                    no_master_warned = False

                should_continue = _run_as_slave(
                    config=config,
                    log=log,
                    lan_sync=lan_sync,
                    cert_info=cert_info,
                    persistent=persistent,
                    peer_manager=peer_manager,
                )
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

    log.info("This client is the MASTER - acting as sync coordinator")

    # Start HTTP server if not already running
    if lan_server is None:
        if config["LAN_HTTPS"] and cert_info:
            lan_server = LANServer(
                port=lan_sync.port,
                use_https=True,
                cert_file=cert_info.cert_file,
                key_file=cert_info.key_file,
                key_password=cert_info.key_password,
            )
        else:
            lan_server = LANServer(port=lan_sync.port)
        lan_server.start()

        protocol = "HTTPS" if config["LAN_HTTPS"] else "HTTP"
        log.info(f"Master HTTP server on port {lan_sync.port} ({protocol})")

    # Start WebSocket server if not already running
    if lan_ws_server is None:
        ws_port = lan_sync.port + 1  # Use next port for WebSocket
        if config["LAN_HTTPS"] and cert_info:
            lan_ws_server = LANWebSocketServer(
                port=ws_port,
                use_https=True,
                cert_file=cert_info.cert_file,
                key_file=cert_info.key_file,
                key_password=cert_info.key_password,
            )
        else:
            lan_ws_server = LANWebSocketServer(port=ws_port)
        lan_ws_server.start(watch_path=config["LOCAL_BUCKET"])
        ws_protocol = "WSS" if config["LAN_HTTPS"] else "WS"
        log.info(f"Master WebSocket server on port {ws_port} ({ws_protocol})")

        if config["LAN_KEY"]:
            log.info("Authentication enabled (HSYNC_LAN_KEY)")
        else:
            log.info("No authentication (trust LAN mode)")

        # Update mDNS service to indicate server is ready
        lan_sync.update_server_ready()

    log.info("Press Ctrl+C to stop...")
    # Yield back to the outer discovery loop after a short idle window
    # so master/peer election can be re-evaluated.  If we are still
    # master on the next iteration, the outer loop will call us again
    # and reuse the existing server instances.
    time.sleep(1)

    return lan_server, lan_ws_server


def _run_as_slave(
    config: "HsyncConfig",
    log,
    lan_sync: LANSync,
    cert_info,
    persistent: bool,
    peer_manager,
) -> bool:
    """Run as client (connect to master). Returns True to continue loop, False to break."""
    from asyncio import run

    master = peer_manager.get_master()
    if master:
        # Wait for master to be ready (server_ready=true in mDNS)
        if not master.server_ready:
            log.info(f"Waiting for master {master.cid} to start server...")
            return True  # Continue discovery loop

        # Protocol gate (symmetric, no-downgrade): only communicate with a
        # master whose transport matches our own LAN_HTTPS preference. We do
        # NOT self-promote on mismatch (that would create a second master and
        # split-brain); we simply stay in the discovery loop and warn.
        if master.use_https != config["LAN_HTTPS"]:
            local_proto = "HTTPS" if config["LAN_HTTPS"] else "HTTP"
            master_proto = "HTTPS" if master.use_https else "HTTP"
            log.warning(
                f"[PROTOCOL_MISMATCH] master {master.cid} uses {master_proto} "
                f"but this client is configured for {local_proto}. "
                f"Refusing to start communication (no protocol downgrade/upgrade). "
                f"Set HSYNC_LAN_HTTPS to match the master ({master_proto}) to sync."
            )
            # Do not self-promote (that would create a second master and
            # split-brain). Keep waiting only in persistent mode; otherwise
            # exit since this node cannot sync with the elected master.
            return persistent

        master_url = master.get_server_url()
        log.info(f"Connecting to master: {master.cid} at {master_url}")

        if master.use_https and master.cert_fingerprint:
            log.info(f"Master using HTTPS, fingerprint: {master.cert_fingerprint[:32]}...")
            config["MASTER_CERT_FINGERPRINT"] = master.cert_fingerprint

        config["API"]["buckets"] = f"{master_url}/api/v1/buckets"
        ws_master_url = sub(r"^http:", "ws:", master_url)
        if master.use_https:
            ws_master_url = sub(r"^http:", "wss:", ws_master_url)
        config["API"]["events"] = f"{ws_master_url}/api/v1/buckets"

        if config["LAN_KEY"]:
            if master.use_https:
                config["REQUEST_HEADERS"]["Authorization"] = f"Bearer {config['LAN_KEY']}"
            else:
                log.warning(
                    "LAN_KEY is set but the master uses plain HTTP; "
                    "the key will NOT be sent over an unencrypted channel."
                )

        # Set compression capability based on master peer
        config["SERVER_COMPRESSION_ENABLED"] = master.compression_enabled

        set_config(config)

        # Measure clock offset vs master using round-trip HTTP
        _measure_clock_offset(master, config, log)

        # Check if dry-run mode is enabled
        if config["DRYRUN"]:
            # In dry-run mode, only show preview without syncing
            _run_preview(log)
            if not persistent:
                return False
            # In persistent mode with dry-run, wait for SYNC/PREVIEW commands
            log.info("Dry-run mode: waiting for commands (stdin) or Ctrl+C to exit...")
            run(_run_stdin_wait(log))
        else:
            # Perform initial sync (master is source of truth in LAN mode)
            run(sync(lan_mode=True))

        if not persistent:
            return False

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
                        use_https=master.use_https,
                    ),
                )
            )
        except Exception as e:
            log.warning(f"WebSocket connection ended: {type(e).__name__}: {e}")
        # If WebSocket disconnects, re-enter discovery loop
        log.info("WebSocket disconnected, re-entering discovery loop...")
        return True
    else:
        # No master and no peers
        if persistent:
            log.info("Waiting for peers to join...")
            return True  # Continue the discovery loop
        else:
            log.warning("No master elected - this client is alone, nothing to sync")
            return False


async def _with_stdin_commands(log, coro) -> None:
    """Run coro with a background stdin command reader."""
    import asyncio
    from asyncio import create_task

    from hsync_client.config import CONFIG
    from hsync_client.stdin_commands import StdinCommandHandler

    handler = StdinCommandHandler(log)
    handler.register("PREVIEW", lambda **_kw: _run_preview(log))

    async def _handle_sync(**_kw: object) -> None:
        lan = CONFIG.get("LAN_MODE", False)
        await sync(lan_mode=lan)

    handler.register("SYNC", _handle_sync)

    async def _handle_set_sync_direction(**kw: object) -> None:
        direction = str(kw.get("direction", "bidirectional"))
        valid = ("bidirectional", "upload-only", "download-only")
        if direction not in valid:
            log.warning(f"Invalid sync direction: {direction!r}. Must be one of {valid}.")
            return
        old = CONFIG.get("SYNC_DIRECTION", "bidirectional")
        if direction == old:
            log.debug(f"Sync direction already set to {direction!r}, no change.")
            return
        CONFIG["SYNC_DIRECTION"] = direction
        log.info(f"Sync direction changed: {old!r} → {direction!r}")
        # Trigger an immediate resync with the new direction
        lan = CONFIG.get("LAN_MODE", False)
        await sync(lan_mode=lan)

    handler.register("SET_SYNC_DIRECTION", _handle_set_sync_direction)

    stdin_task = create_task(handler.run())

    try:
        await coro
    finally:
        stdin_task.cancel()
        try:
            await stdin_task
        except asyncio.CancelledError:
            pass


async def _run_stdin_wait(log) -> None:
    """Block reading stdin commands until EOF or SYNC."""

    from hsync_client.config import CONFIG
    from hsync_client.stdin_commands import StdinCommandHandler

    handler = StdinCommandHandler(log)
    handler.register("PREVIEW", lambda **_kw: _run_preview(log))

    async def _handle_sync(**_kw: object) -> None:
        CONFIG["DRYRUN"] = False
        lan = CONFIG.get("LAN_MODE", False)
        await sync(lan_mode=lan)
        handler.stop()

    handler.register("SYNC", _handle_sync)
    await handler.run()
