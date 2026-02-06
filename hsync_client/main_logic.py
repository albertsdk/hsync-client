# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso


"""
HSYNC Client - Main synchronization logic.

This module contains the core synchronization logic extracted from main.py.
"""

import sys
from asyncio import run
from os.path import basename, exists
from re import sub
from typing import Optional



from hsync_client.config import (
    get_api_key,
    get_compression_enabled,
    get_discovery_timeout,
    get_heartbeat_interval,
    get_ignore_delete,
    get_lan_group,
    get_lan_https,
    get_lan_key,
    get_lan_persistent,
    get_lan_port,
    get_peer_timeout,
    get_server_url,
    get_stability_delay,
    load_env_file,
    HsyncConfig,
    set_config,
    set_debug,
)
from hsync_client.constants import ACTION_LABEL, FILE_STABILITY_INTERVAL_DEFAULT, HSYNC_VERSION
from hsync_client.lan import LANSync
from hsync_client.lan_server import LANServer
from hsync_client.lan_websocket import LANWebSocketServer, lan_websocket_client
from hsync_client.logger import setup_logging, set_client_id
from hsync_client.retry import retry_on_connection_error
from hsync_client.sync import sync
from hsync_client.utils import get_cid, is_valid_logfile
from hsync_client.websocket_handler import remote_connect


def _get_stability_delay_value(cli_value: Optional[int]) -> int:
    """
    Determine the stability delay value.

    Priority: CLI argument > environment variable > default constant.
    """
    if cli_value is not None:
        return cli_value
    env_value = get_stability_delay()
    if env_value is not None:
        try:
            return int(env_value)
        except ValueError:
            pass
    return FILE_STABILITY_INTERVAL_DEFAULT


def run_sync(
    bucket: str,
    env_file: Optional[str],
    server_url: Optional[str],
    verify_ssl: bool,
    ca_cert: Optional[str],
    http_timeout: int,
    local_path: str,
    continuous: bool,
    sync_order: str,
    resync: Optional[int],
    reconnect_delay: int,
    stability_delay: Optional[int],
    max_wait_timeout: int,
    filter_path: Optional[str],
    dryrun: bool,
    ignore_delete: bool,
    compression_enabled: bool,
    lan_mode: bool,
    lan_port: int,
    discovery_timeout: int,
    lan_group: Optional[str],
    lan_https: bool,
    lan_persistent: bool,
    debug: bool,
    quiet: bool,
    logfile: Optional[str],
    no_color: bool,
    legend: bool,
) -> None:
    """
    Main synchronization logic.

    This function orchestrates the entire sync process including:
    - Loading environment files
    - Setting up logging
    - Validating configuration
    - Running sync (single or continuous)
    """
    # Load environment file if specified
    if env_file:
        try:
            load_env_file(env_file)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # Initialize logging
    log = setup_logging(
        debug=debug,
        quiet=quiet,
        logfile=logfile if logfile and is_valid_logfile(logfile) else None,
        no_color=no_color,
    )

    log.info(f"HSYNC Client v{HSYNC_VERSION}")

    # Validate local directory exists
    if not exists(local_path):
        log.error(f"Directory '{local_path}' does not exist")
        return

    # Validate log file path
    if logfile and not is_valid_logfile(logfile):
        log.error(f"Invalid output file '{logfile}'")
        return

    # Determine server URL (CLI argument takes precedence over env var)
    effective_server_url = server_url or get_server_url()

    # Validate server URL is provided (not required in LAN mode)
    if not effective_server_url and not lan_mode:
        log.error("Server URL is required (use -s or set HSYNC_SERVER, or use --lan-mode)")
        return

    # In LAN mode without server URL, use placeholder
    if lan_mode and not effective_server_url:
        effective_server_url = "http://localhost:8765"

    # At this point, effective_server_url is guaranteed to be set
    # (either from args, env, or LAN mode placeholder)
    assert effective_server_url is not None

    # Convert HTTP URL to WebSocket URL for events endpoint
    ws_url = sub(r"^http:", "ws:", effective_server_url)
    ws_url = sub(r"^https:", "wss:", ws_url)

    # Generate unique client ID for this session
    cid = get_cid()
    set_client_id(cid)

    # Populate global configuration dictionary
    config: HsyncConfig = {
        "API": {
            "buckets": f"{effective_server_url}/api/v1/buckets",
            "events": f"{ws_url}/api/v1/buckets",
        },
        "CID": cid,
        "DRYRUN": dryrun,
        "LOCAL_BUCKET": local_path,
        "LOGFILE": logfile,
        "NO_VERIFY_SSL": not verify_ssl,
        "CA_CERT": ca_cert,
        "QUIET": quiet,
        "REMOTE_BUCKET": bucket,
        "REQUEST_HEADERS": {
            "accept": "application/json",
            "Authorization": f"Bearer {get_api_key()}",
            "X-Client-Name": basename(__file__),
            "X-Client-Version": HSYNC_VERSION,
        },
        "RECONNECT_DELAY": reconnect_delay,
        "RESYNC": resync,
        "SYNC_ORDER": sync_order,
        "FILTER_PATH": filter_path,
        "STABILITY_DELAY": _get_stability_delay_value(stability_delay),
        "HTTP_TIMEOUT": http_timeout,
        "MAX_WAIT_TIMEOUT": max_wait_timeout,
        "LAN_MODE": lan_mode,
        "LAN_PORT": lan_port or get_lan_port() or 0,
        "DISCOVERY_TIMEOUT": discovery_timeout or get_discovery_timeout() or 5,
        "HEARTBEAT_INTERVAL": get_heartbeat_interval() or 5,
        "PEER_TIMEOUT": get_peer_timeout() or 15,
        "LAN_KEY": get_lan_key(),
        "LAN_GROUP": lan_group or get_lan_group(),
        "LAN_HTTPS": lan_https or get_lan_https(),
        "LAN_PERSISTENT": lan_persistent or get_lan_persistent(),
        "MASTER_CERT_FINGERPRINT": None,
        "IGNORE_DELETE": ignore_delete or get_ignore_delete(),
        "COMPRESSION_ENABLED": compression_enabled and get_compression_enabled(),
        "SERVER_COMPRESSION_ENABLED": False,  # Set during WebSocket handshake
    }

    set_config(config)
    set_debug(debug)

    log.debug("Local configuration:", extra={'json_data': config})

    # Display legend if requested
    if legend:
        log.info("Log legend:")
        log_labels = {
            ACTION_LABEL['nothing']: "File already in sync (no action required)",
            ACTION_LABEL['skip']: "File skipped",
            ACTION_LABEL['download']: "Download file from server",
            ACTION_LABEL['upload']: "Upload file to server",
            ACTION_LABEL['move']: "Move/Rename file or directory",
            ACTION_LABEL['remove']: "Remove file or directory",
            ACTION_LABEL['create']: "Create directory",
            ACTION_LABEL['permission']: "Set file/directory permission",
            ACTION_LABEL['symlink']: "Create symbolic link",
        }
        for label, description in log_labels.items():
            log.info(f"  {label} {description}")

    # LAN Mode: Peer Discovery and Master Election
    if config["LAN_MODE"]:
        _run_lan_mode(config, log)
        return

    # Server Connectivity Check with retry
    if not _check_server_connectivity(
        server_url=effective_server_url,
        timeout=min(config["HTTP_TIMEOUT"], 10),
        verify_ssl=not config["NO_VERIFY_SSL"],
        logger=log,
    ):
        return

    # Execute Sync
    if config["DRYRUN"] and continuous:
        log.warning("Continuous sync is disabled due to dryrun option set")
    elif not config["DRYRUN"] and continuous:
        run(remote_connect())
    else:
        run(sync())


def _check_server_connectivity(
    server_url: str,
    timeout: float,
    verify_ssl: bool,
    logger,
) -> bool:
    """
    Check server connectivity with automatic retry on transient failures.

    Uses tenacity for exponential backoff retry (3 attempts, 1-10s wait).

    Args:
        server_url: Base URL of the server.
        timeout: Request timeout in seconds.
        verify_ssl: Whether to verify SSL certificates.
        logger: Logger instance for messages.

    Returns:
        True if server is reachable, False otherwise.
    """
    from requests import get as http_get, RequestException

    @retry_on_connection_error(max_attempts=3, min_wait=1, max_wait=10)
    def _do_health_check() -> int:
        """Perform health check request (decorated with retry)."""
        response = http_get(
            f"{server_url}/api/v1/health",
            timeout=timeout,
            verify=verify_ssl,
        )
        return response.status_code

    logger.debug(f"Checking server connectivity: {server_url}")
    try:
        status_code = _do_health_check()
        if status_code >= 500:
            logger.error(f"Server error: {server_url} returned status {status_code}")
            return False
        logger.debug(f"Server is reachable (status: {status_code})")
        return True
    except RequestException as e:
        logger.error(f"Cannot connect to server after retries: {server_url} ({e})")
        return False


def _run_lan_mode(config: HsyncConfig, log) -> None:
    """Handle LAN P2P mode synchronization."""
    import time

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
    )
    # Start discovery and register service for peer discovery
    peer_manager = lan_sync.start(register_service=True)

    # Track if we ever had peers (for different messages)
    ever_had_peers = False
    lan_server = None
    lan_ws_server = None

    try:
        while True:
            # Wait for peer discovery
            lan_sync.wait_for_discovery(timeout=config["DISCOVERY_TIMEOUT"])

            peers = peer_manager.get_alive_peers()
            if peers:
                ever_had_peers = True

            if peer_manager.is_master():
                log.info("This client is the MASTER - acting as sync coordinator")

                # Start HTTP server if not already running
                if lan_server is None:
                    if config["LAN_HTTPS"] and cert_info:
                        lan_server = LANServer(
                            port=lan_sync.port,
                            use_https=True,
                            cert_file=cert_info.cert_file,
                            key_file=cert_info.key_file,
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

                if persistent:
                    log.info("Press Ctrl+C to stop...")
                    # In persistent mode, keep running as master
                    while True:
                        time.sleep(1)
                        # Check if we're still master (peers may have joined/left)
                        if not peer_manager.is_master():
                            log.info("No longer master, switching to client mode...")
                            if lan_server:
                                lan_server.stop()
                                lan_server = None
                            if lan_ws_server:
                                lan_ws_server.stop()
                                lan_ws_server = None
                            break
                else:
                    # Non-persistent: just wait for Ctrl+C
                    log.info("Press Ctrl+C to stop...")
                    while True:
                        time.sleep(1)
            else:
                master = peer_manager.get_master()
                if master:
                    # Wait for master to be ready (server_ready=true in mDNS)
                    if not master.server_ready:
                        log.info(f"Waiting for master {master.cid} to start server...")
                        continue  # Re-enter discovery loop

                    master_url = master.get_server_url()
                    log.info(f"Connecting to master: {master.cid} at {master_url}")

                    if master.use_https and master.cert_fingerprint:
                        log.info(f"Master using HTTPS, fingerprint: {master.cert_fingerprint[:32]}...")
                        config["MASTER_CERT_FINGERPRINT"] = master.cert_fingerprint

                    config["API"]["buckets"] = f"{master_url}/api/v1/buckets"
                    ws_master_url = sub(r"^http:", "ws:", master_url)
                    if master.use_https:
                        ws_master_url = sub(r"^http:", "wss:", master_url)
                    config["API"]["events"] = f"{ws_master_url}/api/v1/buckets"

                    if config["LAN_KEY"]:
                        config["REQUEST_HEADERS"]["Authorization"] = f"Bearer {config['LAN_KEY']}"

                    # Set compression capability based on master peer
                    config["SERVER_COMPRESSION_ENABLED"] = master.compression_enabled

                    set_config(config)

                    # Perform initial sync (master is source of truth in LAN mode)
                    run(sync(lan_mode=True))

                    if not persistent:
                        break

                    # In persistent mode, connect via WebSocket for real-time events
                    log.info("Initial sync complete, connecting to WebSocket for real-time events...")
                    ws_port = master.port + 1  # WebSocket is on next port
                    try:
                        run(lan_websocket_client(
                            host=master.address,
                            port=ws_port,
                            use_https=master.use_https,
                        ))
                    except Exception as e:
                        log.warning(f"WebSocket connection ended: {e}")
                    # If WebSocket disconnects, re-enter discovery loop
                    log.info("WebSocket disconnected, re-entering discovery loop...")
                else:
                    # No master and no peers
                    if persistent:
                        if ever_had_peers:
                            log.info("All peers disconnected, waiting for new peers...")
                        else:
                            log.info("Waiting for peers to join...")
                        # Continue the discovery loop
                    else:
                        log.warning("No master elected - this client is alone, nothing to sync")
                        break

    except KeyboardInterrupt:
        log.info("Shutting down LAN mode...")
    finally:
        if lan_server:
            lan_server.stop()
        if lan_ws_server:
            lan_ws_server.stop()
        lan_sync.stop()

