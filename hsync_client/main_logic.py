# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso


"""
HSYNC Client - Main synchronization logic.

This module contains the core synchronization logic extracted from main.py.
"""

import contextlib
import sys
from asyncio import run
from pathlib import Path
from re import sub
from typing import Any

from hsync_common.ignore import IgnoreMatcher, set_active_matcher

from hsync_client.config import (
    CONFIG,
    HsyncConfig,
    get_api_key,
    get_compression_enabled,
    get_discovery_timeout,
    get_heartbeat_interval,
    get_ignore_delete,
    get_ignore_patterns,
    get_lan_group,
    get_lan_https,
    get_lan_key,
    get_lan_persistent,
    get_lan_port,
    get_peer_timeout,
    get_server_url,
    get_stability_delay,
    get_sync_hidden,
    load_env_file,
    set_config,
    set_debug,
)
from hsync_client.constants import (
    ACTION_LABEL,
    FILE_STABILITY_INTERVAL_DEFAULT,
    HSYNC_VERSION,
    HSYNCKEEP_FILENAME,
)
from hsync_client.lan_handler import run_lan_mode

# Backward compatibility alias
_run_lan_mode = run_lan_mode
from hsync_client.logger import set_client_id, setup_logging
from hsync_client.models.file import clear_sha256_cache
from hsync_client.models.sync_action import SyncActionName
from hsync_client.remote_websocket import remote_connect
from hsync_client.retry import retry_on_connection_error
from hsync_client.stdin_commands import StdinCommandHandler
from hsync_client.sync import (
    ensure_hsynckeep_files,
    get_files_local,
    get_files_remote,
    get_sync_actions,
    sync,
)
from hsync_client.utils import (
    bytes_to_human_readable,
    get_cid,
    is_valid_logfile,
    reset_bandwidth_throttle,
)


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
        # Clear SHA-256 cache so every file is re-hashed with fresh content
        clear_sha256_cache()

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

        downloads = [
            a for a in sync_actions if a.name == SyncActionName.download and not _is_sentinel(a)
        ]
        uploads = [
            a for a in sync_actions if a.name == SyncActionName.upload and not _is_sentinel(a)
        ]
        deletes = [
            a for a in sync_actions if a.name == SyncActionName.delete and not _is_sentinel(a)
        ]
        synced = [
            a for a in sync_actions if a.name == SyncActionName.nothing and not _is_sentinel(a)
        ]

        # Detect renames: DELETE + DOWNLOAD pairs with same sha256sum and size.
        # When a file is renamed on the master, the client sees a delete of
        # the old path and a download of the new path.  If both share the
        # same sha256 checksum and size the operation is really just a rename.
        renames: list[tuple] = []  # (delete_action, download_action)
        if deletes and downloads:
            delete_by_hash: dict[tuple, list] = {}
            for action in deletes:
                key = (action.file.sha256sum, action.file.size or 0)
                if key[0]:  # sha256sum must be available
                    delete_by_hash.setdefault(key, []).append(action)

            matched_download_idx: set[int] = set()
            matched_delete_ids: set[int] = set()
            for i, dl_action in enumerate(downloads):
                key = (dl_action.file.sha256sum, dl_action.file.size or 0)
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
            log.info(
                f'[TO_RENAME   ] "{del_action.file.filepath}" -> "{dl_action.file.filepath}" ({size_str})'
            )
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


def _get_stability_delay_value(cli_value: int | None) -> int:
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


def _check_deletion_risk(
    local_path: str,
    lan_mode: bool,
    force_master: bool,
    sync_direction: str,
    auto_confirm: bool,
    logger: Any,
) -> None:
    """Check if sync settings could delete local files and warn the user.

    If the local directory is non-empty and the sync mode could cause
    file deletions, a warning is printed and – unless *auto_confirm* is
    True – the user is prompted for confirmation.  When *auto_confirm* is
    True the warning is still logged but no interactive prompt is shown.

    Raises SystemExit if the user declines to continue.
    """
    from os import walk

    from hsync_client.models.dir import is_hidden_dir
    from hsync_client.models.file import is_hidden_file

    # upload-only never deletes local files
    if sync_direction == "upload-only":
        return

    # force_master in LAN mode means this client is authoritative – safe
    if lan_mode and force_master:
        return

    # Only download-only mode can wipe local-only files at the initial sync.
    # Bidirectional (server or LAN) uploads local-only files instead of
    # deleting them; remote delete events are expected normal behaviour.
    if sync_direction != "download-only":
        return

    # A directory is considered "empty" for deletion-risk purposes when it
    # contains no user data: hidden/temp/backup files and the .hsynckeep
    # sentinel are system artefacts and do not represent user content.
    has_user_content = False
    try:
        for _root, dirs, files in walk(local_path):
            dirs[:] = [d for d in dirs if not is_hidden_dir(d)]
            for fname in files:
                if fname == HSYNCKEEP_FILENAME:
                    continue
                if is_hidden_file(fname):
                    continue
                has_user_content = True
                break
            if has_user_content:
                break
    except OSError:
        return
    if not has_user_content:
        return

    reason = (
        "download-only mode: remote deletions will be applied locally "
        "and local-only files may be removed"
    )

    logger.warning(
        f"The shared directory '{local_path}' is not empty and the current "
        f"sync settings could DELETE local files."
    )
    logger.warning(f"Reason: {reason}")

    if auto_confirm:
        logger.warning("--yes flag set – proceeding without confirmation.")
        return

    try:
        response = input("Do you want to continue? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        response = ""

    if response not in ("y", "yes"):
        logger.info("Sync cancelled by user.")
        raise SystemExit(0)


def _check_disk_space(local_path: str, min_free_mb: int, logger: Any) -> None:
    """Check if sufficient disk space is available before sync.

    Args:
        local_path: Path to the local sync directory.
        min_free_mb: Minimum free disk space required in MB. 0 = disabled.
        logger: Logger instance for output.

    Raises:
        InsufficientDiskSpaceError: If free space is below the threshold.
    """
    if min_free_mb <= 0:
        return

    from shutil import disk_usage

    from hsync_client.exceptions import InsufficientDiskSpaceError

    try:
        usage = disk_usage(local_path)
        free_mb = usage.free / (1024 * 1024)
        if free_mb < min_free_mb:
            raise InsufficientDiskSpaceError(
                message=(
                    f"Insufficient disk space: {free_mb:.0f} MB available, "
                    f"{min_free_mb} MB required"
                ),
                details={"available_mb": int(free_mb), "required_mb": min_free_mb},
            )
    except OSError as e:
        logger.warning(f"Could not check disk space: {e}")


def _start_hsyncignore_watcher(bucket_root: str, matcher: "IgnoreMatcher") -> "Any":
    """Start a watchdog observer that reloads *matcher* when ``.hsyncignore`` changes.

    Returns the running observer (so the caller can stop it on shutdown), or
    ``None`` if watchdog is unavailable or the directory does not exist.
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)

    try:
        from hsync_common.constants import HSYNCIGNORE_FILENAME
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        class _HsyncignoreHandler(FileSystemEventHandler):
            def on_modified(self, event) -> None:  # type: ignore[override]
                if Path(event.src_path).name == HSYNCIGNORE_FILENAME:
                    _log.debug(".hsyncignore changed — reloading ignore matcher")
                    matcher.reload()

            def on_created(self, event) -> None:  # type: ignore[override]
                self.on_modified(event)

        if not Path(bucket_root).exists():
            return None

        observer = Observer()
        observer.schedule(_HsyncignoreHandler(), path=bucket_root, recursive=False)
        observer.daemon = True
        observer.start()
        _log.debug(f"Hot-reload watcher started for {HSYNCIGNORE_FILENAME} in {bucket_root}")
        return observer
    except Exception as exc:  # pragma: no cover
        _log.warning(f"Could not start .hsyncignore hot-reload watcher: {exc}")
        return None


def run_sync(
    bucket: str,
    env_file: str | None,
    server_url: str | None,
    verify_ssl: bool,
    ca_cert: str | None,
    http_timeout: int,
    local_path: str,
    continuous: bool,
    resync: int | None,
    reconnect_delay: int,
    stability_delay: int | None,
    max_wait_timeout: int,
    filter_path: str | None,
    dryrun: bool,
    ignore_delete: bool,
    sync_direction: str,
    compression_enabled: bool,
    max_bandwidth_upload: int | None,
    max_bandwidth_download: int | None,
    lan_mode: bool,
    lan_port: int,
    discovery_timeout: int,
    lan_group: str | None,
    lan_https: bool,
    lan_persistent: bool,
    force_master: bool,
    debug: bool,
    quiet: bool,
    logfile: str | None,
    no_color: bool,
    legend: bool,
    api_key_fd: int | None = None,
    lan_key_fd: int | None = None,
    auto_confirm: bool = False,
    min_free_disk_space: int = 500,
    sync_hidden: bool = False,
    ignore_patterns: list[str] | None = None,
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
    api_key_from_fd: str | None = None
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
    lan_key_from_fd: str | None = None
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
    if not Path(local_path).exists():
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

    # Resolve API key once: FD-pipe value takes precedence over env var
    resolved_api_key = api_key_from_fd or get_api_key()

    # Security gate: never transmit the API key over an unencrypted channel.
    # The rule keys on the URL scheme the client connects to, so an HTTPS
    # reverse proxy in front of an HTTP backend is fully supported (the
    # client uses an https:// URL; the backend stays HTTP behind the proxy).
    if (
        not lan_mode
        and resolved_api_key
        and not effective_server_url.lower().startswith("https://")
    ):
        log.error(
            f"API key provided but server URL is not HTTPS ('{effective_server_url}'). "
            "Refusing to send credentials over an unencrypted connection. "
            "Use an https:// URL (a reverse proxy terminating TLS is supported)."
        )
        return

    # Populate global configuration dictionary
    config: HsyncConfig = {
        "API": {
            "buckets": f"{effective_server_url}/api/v1/buckets",
            "events": f"{ws_url}/api/v1/buckets",
        },
        "API_KEY": resolved_api_key,
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
            "Authorization": f"Bearer {resolved_api_key}",
            "X-Client-Name": Path(__file__).name,
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
        "SYNC_DIRECTION": sync_direction,
        "MAX_BANDWIDTH_UPLOAD": max_bandwidth_upload,
        "MAX_BANDWIDTH_DOWNLOAD": max_bandwidth_download,
        "MIN_FREE_DISK_SPACE": min_free_disk_space,
        "SYNC_HIDDEN": sync_hidden or get_sync_hidden(),
        "IGNORE_PATTERNS": ignore_patterns
        if ignore_patterns is not None
        else get_ignore_patterns(),
    }

    set_config(config)
    set_debug(debug)

    # Install the process-wide IgnoreMatcher so that all call sites that use
    # is_hidden_file / is_hidden_dir automatically benefit from the new filtering.
    _matcher = IgnoreMatcher(
        bucket_root=local_path,
        include_hidden=config["SYNC_HIDDEN"],
        extra_patterns=config["IGNORE_PATTERNS"],
    )
    set_active_matcher(_matcher)

    # Hot-reload watcher: rebuild the matcher when .hsyncignore changes.
    _hsyncignore_observer = _start_hsyncignore_watcher(local_path, _matcher)

    # Check if local directory exists at startup
    if not Path(local_path).exists():
        log.error(f"Local directory '{local_path}' does not exist")
        return

    # Check for file deletion risk before syncing
    if not dryrun and Path(local_path).exists():
        _check_deletion_risk(
            local_path=local_path,
            lan_mode=lan_mode,
            force_master=force_master,
            sync_direction=sync_direction,
            auto_confirm=auto_confirm,
            logger=log,
        )

    # Check disk space before syncing
    if not dryrun:
        _check_disk_space(local_path, min_free_disk_space, log)

    # Mask sensitive values before logging
    _safe_config = {**config}
    _safe_headers = {**config["REQUEST_HEADERS"]}
    auth = _safe_headers.get("Authorization", "")
    _safe_headers["Authorization"] = "Bearer ****" if auth else ""
    _safe_config["REQUEST_HEADERS"] = _safe_headers
    if _safe_config.get("LAN_KEY"):
        _safe_config["LAN_KEY"] = "****"
    log.debug("Local configuration:", extra={"json_data": _safe_config})

    # Display legend if requested
    if legend:
        log.info("Log legend:")
        log_labels = {
            ACTION_LABEL["nothing"]: "File already in sync (no action required)",
            ACTION_LABEL["skip"]: "File skipped",
            ACTION_LABEL["download"]: "Download file from server",
            ACTION_LABEL["upload"]: "Upload file to server",
            ACTION_LABEL["move"]: "Move/Rename file or directory",
            ACTION_LABEL["remove"]: "Remove file or directory",
            ACTION_LABEL["create"]: "Create directory",
            ACTION_LABEL["permission"]: "Set file/directory permission",
            ACTION_LABEL["symlink"]: "Create symbolic link",
        }
        for label, description in log_labels.items():
            log.info(f"  {label} {description}")

    # LAN Mode: Peer Discovery and Master Election
    if config["LAN_MODE"]:
        run_lan_mode(config, log)
        return

    # Server Connectivity Check with retry
    if not _check_server_connectivity(
        server_url=effective_server_url,
        timeout=min(config["HTTP_TIMEOUT"], 10),
        verify_ssl=not config["NO_VERIFY_SSL"],
        logger=log,
    ):
        return

    # Reset bandwidth throttle state for this session
    reset_bandwidth_throttle()

    # Log bandwidth limits if configured
    if (upload_limit := config.get("MAX_BANDWIDTH_UPLOAD")) is not None:
        log.info(f"Upload bandwidth limit: {bytes_to_human_readable(float(upload_limit))}/s")
    if (download_limit := config.get("MAX_BANDWIDTH_DOWNLOAD")) is not None:
        log.info(f"Download bandwidth limit: {bytes_to_human_readable(float(download_limit))}/s")

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

    async def _handle_set_bandwidth(**kw: object) -> None:
        upload = kw.get("upload")
        download = kw.get("download")

        if upload is not None:
            if upload == 0 or upload == "0":
                CONFIG["MAX_BANDWIDTH_UPLOAD"] = None
            elif isinstance(upload, int):
                CONFIG["MAX_BANDWIDTH_UPLOAD"] = upload
            elif isinstance(upload, str):
                try:
                    CONFIG["MAX_BANDWIDTH_UPLOAD"] = int(upload)
                except (ValueError, TypeError):
                    log.warning(f"Invalid upload bandwidth value: {upload!r}")
                    return
            else:
                log.warning(f"Invalid upload bandwidth value: {upload!r}")
                return

        if download is not None:
            if download == 0 or download == "0":
                CONFIG["MAX_BANDWIDTH_DOWNLOAD"] = None
            elif isinstance(download, int):
                CONFIG["MAX_BANDWIDTH_DOWNLOAD"] = download
            elif isinstance(download, str):
                try:
                    CONFIG["MAX_BANDWIDTH_DOWNLOAD"] = int(download)
                except (ValueError, TypeError):
                    log.warning(f"Invalid download bandwidth value: {download!r}")
                    return
            else:
                log.warning(f"Invalid download bandwidth value: {download!r}")
                return

        upload_str = (
            bytes_to_human_readable(CONFIG["MAX_BANDWIDTH_UPLOAD"]) + "/s"
            if CONFIG["MAX_BANDWIDTH_UPLOAD"]
            else "unlimited"
        )
        download_str = (
            bytes_to_human_readable(CONFIG["MAX_BANDWIDTH_DOWNLOAD"]) + "/s"
            if CONFIG["MAX_BANDWIDTH_DOWNLOAD"]
            else "unlimited"
        )
        log.info(f"Bandwidth limits updated: upload={upload_str}, download={download_str}")
        # Reset throttle state to apply new limits
        reset_bandwidth_throttle()

    handler.register("SET_BANDWIDTH", _handle_set_bandwidth)

    stdin_task = asyncio.create_task(handler.run())

    try:
        await coro
    finally:
        stdin_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stdin_task


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
    from requests import RequestException
    from requests import get as http_get

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
