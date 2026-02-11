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
import time
from asyncio import run
from os.path import basename, exists
from re import sub
from typing import Optional



from hsync_client.config import (
    CONFIG,
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
from hsync_client.constants import (
    ACTION_LABEL,
    FILE_STABILITY_INTERVAL_DEFAULT,
    HSYNCKEEP_FILENAME,
    HSYNC_VERSION,
)
from hsync_client.lan import CLOCK_SKEW_THRESHOLD, LANSync
from hsync_client.lan_server import LANServer
from hsync_client.lan_websocket import LANWebSocketServer, lan_websocket_client
from hsync_client.logger import setup_logging, set_client_id
from hsync_client.models.sync_action import SyncActionName
from hsync_client.retry import retry_on_connection_error
from hsync_client.sync import (
    ensure_hsynckeep_files,
    get_files_local,
    get_files_remote,
    get_sync_actions,
    sync,
)
from hsync_client.stdin_commands import StdinCommandHandler
from hsync_client.utils import bytes_to_human_readable, get_cid, is_valid_logfile
from hsync_client.websocket_handler import remote_connect


def _run_preview(log) -> None:
    """
    Run a preview (dry-run) showing pending sync actions.

    This is triggered by a ``PREVIEW`` command on stdin and shows what
    would be synced without actually performing any transfers.

    Emits structured log messages for each file with prefixes:
    - [SYNCED      ] - file is in sync (no action needed)
    - [TO_DOWNLOAD ] - file needs to be downloaded
    - [TO_UPLOAD   ] - file needs to be uploaded
    - [TO_DELETE   ] - file will be deleted
    """
    log.info("--- Preview (dry-run) ---")
    try:
        # Ensure empty directories have sentinel files
        ensure_hsynckeep_files()

        local_files = get_files_local()
        remote_files = get_files_remote()
        lan_mode = CONFIG.get("LAN_MODE", False)
        sync_actions = get_sync_actions(remote_files, local_files, lan_mode=lan_mode)

        def _is_sentinel(action) -> bool:
            """Return True if the action concerns a .hsynckeep sentinel."""
            fp = action.file.filepath or ""
            return fp.endswith(HSYNCKEEP_FILENAME)

        downloads = [a for a in sync_actions if a.name == SyncActionName.download and not _is_sentinel(a)]
        uploads = [a for a in sync_actions if a.name == SyncActionName.upload and not _is_sentinel(a)]
        deletes = [a for a in sync_actions if a.name == SyncActionName.delete and not _is_sentinel(a)]
        synced = [a for a in sync_actions if a.name == SyncActionName.nothing and not _is_sentinel(a)]

        # Detect renames: DELETE + DOWNLOAD pairs with same md5sum and size.
        # When a file is renamed on the master, the client sees a delete of
        # the old path and a download of the new path.  If both share the
        # same md5 checksum and size the operation is really just a rename.
        renames: list[tuple] = []  # (delete_action, download_action)
        if deletes and downloads:
            delete_by_hash: dict[tuple, list] = {}
            for action in deletes:
                key = (action.file.md5sum, action.file.size or 0)
                if key[0]:  # md5sum must be available
                    delete_by_hash.setdefault(key, []).append(action)

            matched_download_idx: set[int] = set()
            matched_delete_ids: set[int] = set()
            for i, dl_action in enumerate(downloads):
                key = (dl_action.file.md5sum, dl_action.file.size or 0)
                if key[0] and key in delete_by_hash:
                    for del_action in delete_by_hash[key]:
                        if id(del_action) not in matched_delete_ids:
                            renames.append((del_action, dl_action))
                            matched_download_idx.add(i)
                            matched_delete_ids.add(id(del_action))
                            break

            # Remove matched items so they are not emitted as separate events
            downloads = [a for i, a in enumerate(downloads) if i not in matched_download_idx]
            deletes = [a for a in deletes if id(a) not in matched_delete_ids]

        # Emit structured messages for each file status
        for action in synced:
            size_str = bytes_to_human_readable(action.file.size or 0)
            log.info(f'[SYNCED      ] "{action.file.filepath}" ({size_str})')
        for del_action, dl_action in renames:
            size_str = bytes_to_human_readable(dl_action.file.size or 0)
            log.info(f'[TO_RENAME   ] "{del_action.file.filepath}" -> "{dl_action.file.filepath}" ({size_str})')
        for action in downloads:
            size_str = bytes_to_human_readable(action.file.size or 0)
            log.info(f'[TO_DOWNLOAD ] "{action.file.filepath}" ({size_str})')
        for action in uploads:
            size_str = bytes_to_human_readable(action.file.size or 0)
            log.info(f'[TO_UPLOAD   ] "{action.file.filepath}" ({size_str})')
        for action in deletes:
            size_str = bytes_to_human_readable(action.file.size or 0)
            log.info(f'[TO_DELETE   ] "{action.file.filepath}" ({size_str})')

        log.info(
            f"Preview: {len(synced)} synced, {len(downloads)} downloads, "
            f"{len(uploads)} uploads, {len(deletes)} deletes, "
            f"{len(renames)} renames"
        )
    except Exception as e:
        log.error(f"Preview failed: {e}")
    log.info("--- End Preview ---")


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
    force_master: bool,
    debug: bool,
    quiet: bool,
    logfile: Optional[str],
    no_color: bool,
    legend: bool,
    api_key_fd: Optional[int] = None,
    lan_key_fd: Optional[int] = None,
) -> None:
    """
    Main synchronization logic.

    This function orchestrates the entire sync process including:
    - Loading environment files
    - Setting up logging
    - Validating configuration
    - Running sync (single or continuous)
    """
    # Read API key from file descriptor if provided (secure passing from parent)
    api_key_from_fd: Optional[str] = None
    if api_key_fd is not None:
        import os
        try:
            with os.fdopen(api_key_fd, "r") as f:
                api_key_from_fd = f.read().strip()
            if not api_key_from_fd:
                api_key_from_fd = None
        except Exception:
            pass  # Fall back to environment variable

    # Read LAN key from file descriptor if provided (secure passing from parent)
    lan_key_from_fd: Optional[str] = None
    if lan_key_fd is not None:
        import os
        try:
            with os.fdopen(lan_key_fd, "r") as f:
                lan_key_from_fd = f.read().strip()
            if not lan_key_from_fd:
                lan_key_from_fd = None
        except Exception:
            pass  # Fall back to environment variable

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

    # ── PyInstaller workaround: prevent corrupt netrc module crash ──────
    # The ``requests`` library calls ``get_netrc_auth()`` on every HTTP
    # request, which does ``from netrc import …``.  In some PyInstaller
    # bundles the ``netrc`` module is corrupted (zlib decompression error)
    # and the resulting ``zlib.error`` is *not* caught by requests'
    # ``except (ImportError, AttributeError)`` handler.
    # Pre-registering a lightweight stub in ``sys.modules`` avoids the
    # decompression entirely while keeping the ``requests`` code-path
    # happy (it will simply find no credentials and return ``None``).
    try:
        import netrc as _netrc_test  # noqa: F401 – probe only
    except Exception:
        from types import ModuleType

        _netrc_stub = ModuleType("netrc")
        _netrc_stub.netrc = lambda *a, **kw: None  # type: ignore[attr-defined]
        _netrc_stub.NetrcParseError = Exception  # type: ignore[attr-defined]
        sys.modules["netrc"] = _netrc_stub
        log.debug("Registered netrc stub module (PyInstaller workaround)")

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
    log.info(f"Session CID: {cid}")

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
            "Authorization": f"Bearer {api_key_from_fd or get_api_key()}",
            "X-Client-Name": basename(__file__),
            "X-Client-Version": HSYNC_VERSION,
        },
        "RECONNECT_DELAY": reconnect_delay,
        "RESYNC": resync,
        "FILTER_PATH": filter_path,
        "STABILITY_DELAY": _get_stability_delay_value(stability_delay),
        "HTTP_TIMEOUT": http_timeout,
        "MAX_WAIT_TIMEOUT": max_wait_timeout,
        "LAN_MODE": lan_mode,
        "LAN_PORT": lan_port or get_lan_port() or 0,
        "DISCOVERY_TIMEOUT": discovery_timeout or get_discovery_timeout() or 5,
        "HEARTBEAT_INTERVAL": get_heartbeat_interval() or 5,
        "PEER_TIMEOUT": get_peer_timeout() or 15,
        "LAN_KEY": lan_key_from_fd or get_lan_key(),
        "LAN_GROUP": lan_group or get_lan_group(),
        "LAN_HTTPS": lan_https or get_lan_https(),
        "LAN_PERSISTENT": lan_persistent or get_lan_persistent(),
        "LAN_FORCE_MASTER": force_master,
        "MASTER_CERT_FINGERPRINT": None,
        "IGNORE_DELETE": ignore_delete or get_ignore_delete(),
        "COMPRESSION_ENABLED": compression_enabled and get_compression_enabled(),
        "SERVER_COMPRESSION_ENABLED": False,  # Set during WebSocket handshake
    }

    set_config(config)
    set_debug(debug)

    # Mask sensitive values before logging
    _safe_config = {**config}
    _safe_headers = {**config["REQUEST_HEADERS"]}
    auth = _safe_headers.get("Authorization", "")
    _safe_headers["Authorization"] = "Bearer ****" if auth else ""
    _safe_config["REQUEST_HEADERS"] = _safe_headers
    if _safe_config.get("LAN_KEY"):
        _safe_config["LAN_KEY"] = "****"
    log.debug("Local configuration:", extra={'json_data': _safe_config})

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
        # Dry-run + continuous: show preview, wait for SYNC command,
        # then switch to continuous sync.
        _run_preview(log)
        run(_run_stdin_wait(log))
        # After SYNC command: DRYRUN is now False, run continuous sync
        run(_run_with_stdin_commands(log))
    elif not config["DRYRUN"] and continuous:
        run(_run_with_stdin_commands(log))
    else:
        run(sync())


async def _with_stdin_commands(log, coro) -> None:
    """Run *coro* with a background stdin command reader.

    The reader listens for JSON commands on stdin (one per line) and
    dispatches them to the appropriate handler.  ``PREVIEW`` triggers
    a dry-run preview and ``SYNC`` runs a full synchronisation.

    When *coro* finishes (or raises), the reader is cancelled.
    """
    import asyncio

    handler = StdinCommandHandler(log)
    handler.register("PREVIEW", lambda **_kw: _run_preview(log))

    async def _handle_sync(**_kw: object) -> None:
        lan = CONFIG.get("LAN_MODE", False)
        await sync(lan_mode=lan)

    handler.register("SYNC", _handle_sync)

    stdin_task = asyncio.create_task(handler.run())

    try:
        await coro
    finally:
        stdin_task.cancel()
        try:
            await stdin_task
        except asyncio.CancelledError:
            pass


async def _run_with_stdin_commands(log) -> None:
    """Run remote_connect with a background stdin command reader."""
    await _with_stdin_commands(log, remote_connect())


async def _run_stdin_wait(log) -> None:
    """Block reading stdin commands until EOF or SYNC.

    Used in persistent dry-run mode (LAN or server) where there is no
    WebSocket connection yet.  When a ``SYNC`` command arrives the
    handler performs the full sync, then stops the reader so the caller
    can proceed to the next phase (e.g. WebSocket connection).
    """
    handler = StdinCommandHandler(log)
    handler.register("PREVIEW", lambda **_kw: _run_preview(log))

    async def _handle_sync(**_kw: object) -> None:
        CONFIG["DRYRUN"] = False
        lan = CONFIG.get("LAN_MODE", False)
        await sync(lan_mode=lan)
        handler.stop()

    handler.register("SYNC", _handle_sync)
    await handler.run()


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


def _measure_clock_offset(master, config: HsyncConfig, log) -> None:
    """
    Measure clock offset between this client and the master using round-trip HTTP.

    Uses an NTP-like round-trip measurement:
        T1 = local time before request
        T2 = master time (from /api/v1/time response)
        T3 = local time after response
        offset = T2 - (T1 + RTT/2), where RTT = T3 - T1

    Updates master.clock_offset with the measured value and logs a warning
    if the offset exceeds CLOCK_SKEW_THRESHOLD.

    Args:
        master: The master Peer object.
        config: Current hsync configuration.
    """
    from requests import get as http_get, RequestException
    from hsync_client.utils import get_ssl_verify

    master_url = master.get_server_url()
    time_url = f"{master_url}/api/v1/time"

    try:
        t1 = time.time()
        response = http_get(
            time_url,
            headers={k: str(v) for k, v in config["REQUEST_HEADERS"].items()},
            verify=get_ssl_verify(),
            timeout=config.get("HTTP_TIMEOUT", 30),
        )
        t3 = time.time()

        if response.status_code != 200:
            log.debug(f"Clock sync: /api/v1/time returned {response.status_code}, skipping")
            return

        t2 = response.json().get("timestamp")
        if t2 is None:
            log.debug("Clock sync: no timestamp in response, skipping")
            return

        rtt = t3 - t1
        offset = t2 - (t1 + rtt / 2)
        master.clock_offset = offset

        if abs(offset) > CLOCK_SKEW_THRESHOLD:
            log.warning(
                f"Clock skew detected with master {master.cid}: "
                f"offset={offset:.1f}s, RTT={rtt * 1000:.1f}ms. "
                f"File comparison will use MD5 fallback for "
                f"mtime differences within tolerance."
            )
        else:
            log.debug(
                f"Clock sync with master {master.cid}: "
                f"offset={offset:.3f}s, RTT={rtt * 1000:.1f}ms"
            )

    except RequestException as e:
        log.debug(f"Clock sync failed: {e}")
    except (ValueError, KeyError) as e:
        log.debug(f"Clock sync: invalid response: {e}")


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
        force_master=config["LAN_FORCE_MASTER"],
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

                    # Measure clock offset vs master using round-trip HTTP
                    _measure_clock_offset(master, config, log)

                    # Check if dry-run mode is enabled
                    if config["DRYRUN"]:
                        # In dry-run mode, only show preview without syncing
                        _run_preview(log)
                        if not persistent:
                            break
                        # In persistent mode with dry-run, wait for SYNC/PREVIEW commands
                        log.info("Dry-run mode: waiting for commands (stdin) or Ctrl+C to exit...")
                        run(_run_stdin_wait(log))
                    else:
                        # Perform initial sync (master is source of truth in LAN mode)
                        run(sync(lan_mode=True))

                    if not persistent:
                        break

                    # In persistent mode, connect via WebSocket for real-time events
                    # with a stdin command reader running alongside so the
                    # desktop GUI can send PREVIEW (and future) commands.
                    log.info("Initial sync complete, connecting to WebSocket for real-time events...")
                    ws_port = master.port + 1  # WebSocket is on next port
                    try:
                        run(_with_stdin_commands(
                            log,
                            lan_websocket_client(
                                host=master.address,
                                port=ws_port,
                                use_https=master.use_https,
                            ),
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

