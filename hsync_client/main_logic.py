#!/usr/bin/python3
# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso <alberto.sdk [at] gmail [dot] com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

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
    LOG = setup_logging(
        debug=debug,
        quiet=quiet,
        logfile=logfile if logfile and is_valid_logfile(logfile) else None,
        no_color=no_color,
    )

    LOG.info(f"HSYNC Client v{HSYNC_VERSION}")

    # Validate local directory exists
    if not exists(local_path):
        LOG.error(f"Directory '{local_path}' does not exist")
        return

    # Validate log file path
    if logfile and not is_valid_logfile(logfile):
        LOG.error(f"Invalid output file '{logfile}'")
        return

    # Determine server URL (CLI argument takes precedence over env var)
    effective_server_url = server_url or get_server_url()

    # Validate server URL is provided (not required in LAN mode)
    if not effective_server_url and not lan_mode:
        LOG.error("Server URL is required (use -s or set HSYNC_SERVER, or use --lan-mode)")
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
    }

    set_config(config)
    set_debug(debug)

    LOG.debug("Local configuration:", extra={'json_data': config})

    # Display legend if requested
    if legend:
        LOG.info("Log legend:")
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
            LOG.info(f"  {label} {description}")

    # LAN Mode: Peer Discovery and Master Election
    if config["LAN_MODE"]:
        _run_lan_mode(config, LOG)
        return

    # Server Connectivity Check with retry
    if not _check_server_connectivity(
        server_url=effective_server_url,
        timeout=min(config["HTTP_TIMEOUT"], 10),
        verify_ssl=not config["NO_VERIFY_SSL"],
        logger=LOG,
    ):
        return

    # Execute Sync
    if config["DRYRUN"] and continuous:
        LOG.warning("Continuous sync is disabled due to dryrun option set")
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


def _run_lan_mode(config: HsyncConfig, LOG) -> None:
    """Handle LAN P2P mode synchronization."""
    import time

    LOG.info("LAN P2P mode enabled")
    persistent = config.get("LAN_PERSISTENT", False)
    if persistent:
        LOG.info("Persistent mode enabled - will wait indefinitely for peers")

    cert_info = None
    if config["LAN_HTTPS"]:
        from hsync_client.ssl_utils import generate_self_signed_cert, save_cert_to_temp_files
        LOG.info("Generating self-signed certificate for HTTPS...")
        cert_info = generate_self_signed_cert()
        cert_info = save_cert_to_temp_files(cert_info)
        LOG.info(f"Certificate fingerprint: {cert_info.fingerprint[:32]}...")

    lan_sync = LANSync(
        cid=config["CID"],
        bucket=config["REMOTE_BUCKET"],
        port=config["LAN_PORT"],
        group=config["LAN_GROUP"],
        use_https=config["LAN_HTTPS"],
        cert_fingerprint=cert_info.fingerprint if cert_info else None,
    )
    peer_manager = lan_sync.start()

    # Track if we ever had peers (for different messages)
    ever_had_peers = False
    lan_server = None

    try:
        while True:
            # Wait for peer discovery
            lan_sync.wait_for_discovery(timeout=config["DISCOVERY_TIMEOUT"])

            peers = peer_manager.get_alive_peers()
            if peers:
                ever_had_peers = True

            if peer_manager.is_master():
                LOG.info("This client is the MASTER - acting as sync coordinator")

                # Start server if not already running
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
                    LOG.info(f"Master server listening on port {lan_sync.port} ({protocol})")
                    if config["LAN_KEY"]:
                        LOG.info("Authentication enabled (HSYNC_LAN_KEY)")
                    else:
                        LOG.info("No authentication (trust LAN mode)")

                if persistent:
                    LOG.info("Press Ctrl+C to stop...")
                    # In persistent mode, keep running as master
                    while True:
                        time.sleep(1)
                        # Check if we're still master (peers may have joined/left)
                        if not peer_manager.is_master():
                            LOG.info("No longer master, switching to client mode...")
                            if lan_server:
                                lan_server.stop()
                                lan_server = None
                            break
                else:
                    # Non-persistent: just wait for Ctrl+C
                    LOG.info("Press Ctrl+C to stop...")
                    while True:
                        time.sleep(1)
            else:
                master = peer_manager.get_master()
                if master:
                    master_url = master.get_server_url()
                    LOG.info(f"Connecting to master: {master.cid} at {master_url}")

                    if master.use_https and master.cert_fingerprint:
                        LOG.info(f"Master using HTTPS, fingerprint: {master.cert_fingerprint[:32]}...")
                        config["MASTER_CERT_FINGERPRINT"] = master.cert_fingerprint

                    config["API"]["buckets"] = f"{master_url}/api/v1/buckets"
                    ws_master_url = sub(r"^http:", "ws:", master_url)
                    if master.use_https:
                        ws_master_url = sub(r"^http:", "wss:", master_url)
                    config["API"]["events"] = f"{ws_master_url}/api/v1/buckets"

                    if config["LAN_KEY"]:
                        config["REQUEST_HEADERS"]["Authorization"] = f"Bearer {config['LAN_KEY']}"

                    set_config(config)
                    run(sync())

                    if not persistent:
                        break
                    # In persistent mode, continue discovery loop after sync
                    LOG.info("Sync complete, continuing peer discovery...")
                else:
                    # No master and no peers
                    if persistent:
                        if ever_had_peers:
                            LOG.info("All peers disconnected, waiting for new peers...")
                        else:
                            LOG.info("Waiting for peers to join...")
                        # Continue the discovery loop
                    else:
                        LOG.warning("No master elected - this client is alone, nothing to sync")
                        break

    except KeyboardInterrupt:
        LOG.info("Shutting down LAN mode...")
    finally:
        if lan_server:
            lan_server.stop()
        lan_sync.stop()

