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
from typing import Any, Literal

from hsync_common.format import format_bytes
from hsync_common.ignore import IgnoreMatcher, set_active_matcher
from hsync_common.models.file import clear_sha256_cache
from hsync_common.models.sync_action import SyncActionName
from hsync_common.protocol.handshake import PROTOCOL_VERSION_REST
from hsync_common.runtime import detect_container, is_tty_interactive, runtime_summary

from hsync_client.config import (
    CONFIG,
    HsyncConfig,
    get_api_key,
    get_autotune_enabled,
    get_compression_enabled,
    get_delta_enabled,
    get_discovery_timeout,
    get_heartbeat_interval,
    get_ignore_delete,
    get_ignore_patterns,
    get_lan_group,
    get_lan_key,
    get_lan_persistent,
    get_lan_port,
    get_lan_require_pairing,
    get_mass_delete_guard,
    get_mass_delete_percent,
    get_mass_delete_threshold,
    get_max_concurrent_transfers,
    get_never_master,
    get_peer_timeout,
    get_resync,
    get_server_url,
    get_stability_delay,
    load_env_file,
    set_config,
    set_debug,
)
from hsync_client.constants import (
    DISCOVERY_TIMEOUT_DEFAULT,
    FILE_STABILITY_INTERVAL_DEFAULT,
    HEARTBEAT_INTERVAL,
    HSYNC_VERSION,
    HSYNCKEEP_FILENAME,
    MAX_INBOUND_SIZE_MB_DEFAULT,
    PEER_TIMEOUT,
    RESYNC_DEFAULT,
    TRANSFER_RETRY_ATTEMPTS_DEFAULT,
    TRANSFER_RETRY_MAX_WAIT_DEFAULT,
    TRANSFER_RETRY_MIN_WAIT_DEFAULT,
    ActionLabel,
)
from hsync_client.events import UiEventName, emit_event, init_emitter
from hsync_client.lan_handler import run_lan_mode
from hsync_client.logger import set_client_id, setup_logging
from hsync_client.remote_websocket import remote_connect
from hsync_client.retry import retry_on_connection_error
from hsync_client.stdin_commands import StdinCommandHandler
from hsync_client.sync import (
    ensure_hsynckeep_files,
    get_files_local,
    get_files_remote,
    get_sync_actions,
    get_tombstones_remote,
    sync,
)
from hsync_client.utils import (
    _suppress_insecure_warning,
    diagnose_connection_error,
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
    - [SYNCED] - file is in sync (no action needed)
    - [TO DOWNLOAD] - file needs to be downloaded
    - [TO UPLOAD] - file needs to be uploaded
    - [TO DELETE] - file will be deleted
    """
    log.info("--- Preview (dry-run) ---")
    emit_event(UiEventName.preview_start)
    try:
        # Clear SHA-256 cache so every file is re-hashed with fresh content
        clear_sha256_cache()

        # Ensure empty directories have sentinel files
        ensure_hsynckeep_files()

        local_files = get_files_local()
        remote_files = get_files_remote()
        tombstones = get_tombstones_remote()
        lan_mode = CONFIG.get("LAN_MODE", False)
        sync_actions = get_sync_actions(
            remote_files, local_files, lan_mode=lan_mode, tombstones=tombstones
        )

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

        # Emit structured messages for each file status (text log + event channel)
        for action in synced:
            size = action.file.size or 0
            log.info(f'{ActionLabel.SYNCED} "{action.file.filepath}" ({format_bytes(size)})')
            emit_event(
                UiEventName.file_status, state="synced", file=action.file.filepath, size=size
            )
        for del_action, dl_action in renames:
            size = dl_action.file.size or 0
            log.info(
                f'{ActionLabel.TO_RENAME} "{del_action.file.filepath}" -> "{dl_action.file.filepath}" ({format_bytes(size)})'
            )
            emit_event(
                UiEventName.rename_status,
                src=del_action.file.filepath,
                dst=dl_action.file.filepath,
                size=size,
            )
        for action in downloads:
            size = action.file.size or 0
            log.info(f'{ActionLabel.TO_DOWNLOAD} "{action.file.filepath}" ({format_bytes(size)})')
            emit_event(
                UiEventName.file_status, state="to_download", file=action.file.filepath, size=size
            )
        for action in uploads:
            size = action.file.size or 0
            log.info(f'{ActionLabel.TO_UPLOAD} "{action.file.filepath}" ({format_bytes(size)})')
            emit_event(
                UiEventName.file_status, state="to_upload", file=action.file.filepath, size=size
            )
        for action in deletes:
            size = action.file.size or 0
            log.info(f'{ActionLabel.TO_DELETE} "{action.file.filepath}" ({format_bytes(size)})')
            emit_event(
                UiEventName.file_status, state="to_delete", file=action.file.filepath, size=size
            )

        log.info(
            f"Preview: {len(synced)} synced, {len(downloads)} downloads, "
            f"{len(uploads)} uploads, {len(deletes)} deletes, "
            f"{len(renames)} renames"
        )
    except Exception as e:
        log.error(f"Preview failed: {e}")
        emit_event(UiEventName.error, message=f"Preview failed: {e}")
    log.info("--- End Preview ---")
    emit_event(UiEventName.preview_end)


# Provenance of a resolved tuning value; feeds AUTOTUNE_MANAGED_KEYS so the LAN
# post-discovery refresh re-derives only autotune/default-sourced keys and never
# clobbers explicit CLI/env overrides. Single encode site: the resolvers below.
TuningSource = Literal["cli", "env", "autotune", "default"]


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


def _get_discovery_timeout_value(cli_value: int | None) -> int:
    """
    Determine the LAN discovery timeout value.

    Priority: CLI argument > environment variable > default constant.
    A value of 0 is valid and means "wait indefinitely".
    """
    if cli_value is not None:
        return cli_value

    env_value = get_discovery_timeout()
    if env_value is not None:
        return env_value

    return DISCOVERY_TIMEOUT_DEFAULT


def _get_resync_value(cli_value: int | None) -> int | None:
    """
    Determine the periodic resync interval (seconds).

    Priority: CLI argument > environment variable > default constant.
    Periodic resync is enabled by default as a safety net for missed
    file-system events; a value of 0 (env only) disables it.
    """
    if cli_value is not None:
        return cli_value

    env_value = get_resync()
    if env_value is not None:
        return env_value or None  # 0 disables periodic resync

    return RESYNC_DEFAULT


def _get_max_concurrent_transfers_value(cli_value: int | None, autotune: int | None = None) -> int:
    """
    Determine the max concurrent transfers value.

    Priority: CLI argument > environment variable > autotune > default constant.
    """
    from hsync_client.constants import MAX_CONCURRENT_TRANSFERS_DEFAULT

    if cli_value is not None:
        return cli_value
    env_value = get_max_concurrent_transfers()
    if env_value is not None:
        return env_value
    if autotune is not None:
        return autotune
    return MAX_CONCURRENT_TRANSFERS_DEFAULT


def _get_http_timeout_value(
    cli_value: int | None, autotune: int | None = None
) -> tuple[int, TuningSource]:
    """
    Determine the HTTP timeout value and its provenance.

    Priority: CLI argument > environment variable > autotune > default constant.
    The provenance tag feeds ``AUTOTUNE_MANAGED_KEYS``: only keys resolved from
    autotune/default may be re-derived by the LAN post-discovery refresh.
    """
    from hsync_client.config import get_http_timeout
    from hsync_client.constants import HTTP_TIMEOUT_DEFAULT

    if cli_value is not None:
        return cli_value, "cli"
    env_value = get_http_timeout()
    if env_value is not None:
        return env_value, "env"
    if autotune is not None:
        return autotune, "autotune"
    return HTTP_TIMEOUT_DEFAULT, "default"


def _get_reconnect_delay_value(
    cli_value: int | None, autotune: int | None = None
) -> tuple[int, TuningSource]:
    """
    Determine the reconnection delay value and its provenance.

    Priority: CLI argument > environment variable > autotune > default constant.
    """
    from hsync_client.config import get_reconnect_delay
    from hsync_client.constants import RECONNECT_DELAY_DEFAULT

    if cli_value is not None:
        return cli_value, "cli"
    env_value = get_reconnect_delay()
    if env_value is not None:
        return env_value, "env"
    if autotune is not None:
        return autotune, "autotune"
    return RECONNECT_DELAY_DEFAULT, "default"


def _get_transfer_retry_min_wait_value(autotune: int | None = None) -> tuple[float, TuningSource]:
    """
    Determine the per-file retry minimum wait value and its provenance.

    Priority: environment variable > autotune > default constant (no CLI flag).
    """
    from hsync_client.config import get_transfer_retry_min_wait

    env_value = get_transfer_retry_min_wait()
    if env_value is not None:
        return env_value, "env"
    if autotune is not None:
        return float(autotune), "autotune"
    return TRANSFER_RETRY_MIN_WAIT_DEFAULT, "default"


def _get_transfer_retry_max_wait_value(autotune: int | None = None) -> tuple[float, TuningSource]:
    """
    Determine the per-file retry maximum wait value and its provenance.

    Priority: environment variable > autotune > default constant (no CLI flag).
    """
    from hsync_client.config import get_transfer_retry_max_wait

    env_value = get_transfer_retry_max_wait()
    if env_value is not None:
        return env_value, "env"
    if autotune is not None:
        return float(autotune), "autotune"
    return TRANSFER_RETRY_MAX_WAIT_DEFAULT, "default"


def _get_mass_delete_guard_value(cli_value: bool | None) -> bool:
    """Determine mass-deletion guard on/off. Priority: CLI > env > default."""
    if cli_value is not None:
        return cli_value
    return get_mass_delete_guard()


def _get_mass_delete_threshold_value(cli_value: int | None) -> int:
    """Determine mass-deletion absolute threshold. Priority: CLI > env > default."""
    if cli_value is not None:
        return cli_value
    return get_mass_delete_threshold()


def _get_mass_delete_percent_value(cli_value: int | None) -> int:
    """Determine mass-deletion percentage threshold. Priority: CLI > env > default."""
    if cli_value is not None:
        return cli_value
    return get_mass_delete_percent()


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

    from hsync_common.models.dir import is_hidden_dir
    from hsync_common.models.file import is_hidden_file

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

    # No operator can answer the prompt without an interactive terminal
    # (containers, systemd units, cron, pipes): fail fast with a clear
    # message instead of blocking on input() or silently declining via EOF.
    if not is_tty_interactive():
        logger.error(
            "Deletion-risk confirmation required but no interactive terminal is "
            "available. Re-run with --yes to accept the risk and proceed."
        )
        raise SystemExit(1)

    try:
        response = input("Do you want to continue? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        response = ""

    if response not in ("y", "yes"):
        logger.info("Sync cancelled by user.")
        raise SystemExit(0)


def _check_lan_write_policy(
    lan_mode: bool,
    lan_key: str | None,
    sync_direction: str,
    auto_confirm: bool,
    logger: Any,
) -> None:
    """Require a shared LAN key for write-capable sync in non-interactive mode.

    A self-signed LAN transport hides traffic but does not prove the master's
    identity, so an unattended client that uploads data must authenticate with
    ``HSYNC_LAN_KEY``. The trust policy lives in
    :func:`hsync_common.protocol.pairing.lan_write_requires_key`; here we apply
    it. A run is unattended when ``auto_confirm`` (the ``--yes`` flag) is set or
    when no interactive terminal is available (container, systemd, cron, pipes):
    in both cases no operator can vouch for the peer out of band.

    Raises SystemExit when the policy is violated.
    """
    if not lan_mode:
        return

    from hsync_common.protocol.pairing import lan_write_requires_key

    interactive = not auto_confirm and is_tty_interactive()
    if not lan_write_requires_key(sync_direction, interactive=interactive):
        return

    # The shared key authenticates writes; over the HTTPS-only LAN transport a
    # present key always satisfies the policy.
    if lan_key:
        return

    logger.error(
        f"{ActionLabel.AUTH_REQUIRED} Non-interactive LAN write requires authentication. "
        "Set HSYNC_LAN_KEY to upload to the master, or run interactively "
        "(omit --yes), or use --sync-direction download-only."
    )
    emit_event(
        UiEventName.auth_required,
        message="A shared LAN key (HSYNC_LAN_KEY) is required for non-interactive writes",
    )
    raise SystemExit(1)


def _warn_if_lan_in_container(logger: Any) -> None:
    """Warn that mDNS peer discovery usually fails under container networking.

    LAN discovery relies on multicast, which does not cross the NAT boundary
    of the default bridge network used by container runtimes; LAN mode inside
    a container therefore requires host networking to find peers.
    """
    info = detect_container()
    if not info.is_container:
        return
    logger.warning(
        f"LAN mode inside a {info.runtime or 'unknown'} container: mDNS peer "
        "discovery cannot cross container bridge/NAT networking. Use host "
        "networking (e.g. 'docker run --network host') or peers will not be found."
    )


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
    http_timeout: int | None,
    local_path: str,
    continuous: bool,
    resync: int | None,
    reconnect_delay: int | None,
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
    discovery_timeout: int | None,
    lan_group: str | None,
    lan_persistent: bool,
    force_master: bool,
    never_master: bool,
    debug: bool,
    quiet: bool,
    logfile: str | None,
    no_color: bool,
    legend: bool,
    lan_require_pairing: bool = False,
    api_key_fd: int | None = None,
    lan_key_fd: int | None = None,
    events_json_fd: int | None = None,
    auto_confirm: bool = False,
    min_free_disk_space: int = 500,
    max_inbound_size: int = MAX_INBOUND_SIZE_MB_DEFAULT,
    ignore_patterns: list[str] | None = None,
    max_concurrent_transfers: int | None = None,
    no_autotune: bool = False,
    mass_delete_guard: bool | None = None,
    mass_delete_threshold: int | None = None,
    mass_delete_percent: int | None = None,
    delta_enabled: bool = True,
    delta_min_file_size: int | None = None,
    delta_min_file_size_lan: int | None = None,
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

    # Install the structured event emitter (P0-B). No-op when the parent did
    # not pass --events-json-fd, so a plain CLI run is unaffected.
    init_emitter(events_json_fd)

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
    log.info(runtime_summary())

    # Validate local directory exists
    if not Path(local_path).exists():
        log.error(f"Directory '{local_path}' does not exist")
        emit_event(UiEventName.path_not_found, path=local_path)
        return

    # Validate log file path
    if logfile and not is_valid_logfile(logfile):
        log.error(f"Invalid output file '{logfile}'")
        return

    # Determine server URL (CLI argument takes precedence over env var)
    effective_server_url = server_url or get_server_url()

    # LAN mode without a configured server URL runs on a placeholder until the
    # master is discovered: the RTT probe and the UI server event are deferred
    # (probing the placeholder would always fail with a spurious warning).
    using_lan_placeholder = lan_mode and not effective_server_url

    # Validate server URL is provided (not required in LAN mode)
    if not effective_server_url and not lan_mode:
        log.error("Server URL is required (use -s or set HSYNC_SERVER, or use --lan-mode)")
        return

    # In LAN mode without server URL, use placeholder
    if lan_mode and not effective_server_url:
        effective_server_url = "http://localhost:9753"

    # At this point, effective_server_url is guaranteed to be set
    # (either from args, env, or LAN mode placeholder)
    assert effective_server_url is not None
    if not using_lan_placeholder:
        emit_event(UiEventName.server, url=effective_server_url)

    # Convert HTTP URL to WebSocket URL for events endpoint
    ws_url = sub(r"^http:", "ws:", effective_server_url)
    ws_url = sub(r"^https:", "wss:", ws_url)

    # Generate unique client ID for this session
    cid = get_cid()
    set_client_id(cid)
    log.info(f"Session CID: {cid}")
    emit_event(UiEventName.session, cid=cid)

    # Resolve API key once: FD-pipe value takes precedence over env var
    resolved_api_key = api_key_from_fd or get_api_key()

    # Fail fast on a missing API key: every authenticated server endpoint
    # requires a valid key, so an empty key can never sync. Reporting it here
    # avoids a late, opaque "Invalid API key" failure mid-sync. LAN mode has
    # its own shared-key policy and is exempt.
    if not lan_mode and not resolved_api_key:
        log.error(
            "No API key configured. Set HSYNC_API_KEY (or pass it via "
            "--api-key-fd) to authenticate with the server."
        )
        return

    # Security gate: never transmit the API key over an unencrypted channel.
    # The rule keys on the URL scheme the client connects to, so an HTTPS
    # reverse proxy in front of an HTTP backend is fully supported (the
    # client uses an https:// URL; the backend stays HTTP behind the proxy).
    if not lan_mode and not effective_server_url.lower().startswith("https://"):
        log.error(
            f"API key provided but server URL is not HTTPS ('{effective_server_url}'). "
            "Refusing to send credentials over an unencrypted connection. "
            "Use an https:// URL (a reverse proxy terminating TLS is supported)."
        )
        return

    # Populate global configuration dictionary

    # ── Auto-tuning ────────────────────────────────────────────────────
    autotune_enabled = not no_autotune and (not dryrun or continuous) and get_autotune_enabled()
    tuning: Any = None  # TuningProfile | None
    if autotune_enabled:
        try:
            from urllib.parse import urlparse

            import hsync_client.autotune as autotune_mod
            from hsync_client.autotune import (
                ProbeResults,
                classify_bandwidth,
                classify_network,
                derive_profile,
                detect_storage_type,
                measure_rtt,
                profile_transfer_queue,
            )

            parsed = urlparse(effective_server_url)
            rtt_host = parsed.hostname or "localhost"
            rtt_port = parsed.port or (443 if parsed.scheme == "https" else 80)

            # LAN placeholder: no server to probe yet — defer RTT measurement
            # to the post-discovery refresh in lan_handler (slave role only).
            rtt_ms = None if using_lan_placeholder else measure_rtt(rtt_host, rtt_port)
            net_class = classify_network(rtt_ms)
            bw_class = classify_bandwidth(autotune_mod._bw_ema, None)
            storage_type = detect_storage_type(local_path)

            probe_results = ProbeResults(
                rtt_ms=rtt_ms,
                net_class=net_class,
                bw_class=bw_class,
                storage_type=storage_type,
                queue_profile=profile_transfer_queue([]),  # MIXED at startup
            )
            tuning = derive_profile(probe_results)
            log.info(f"autotune: {tuning.notes}")
        except Exception as e:
            log.warning(f"autotune: probe/policy failed ({e}); using safe defaults")
            autotune_enabled = False

    _autotune_timeout = tuning.http_timeout if tuning else None
    _autotune_retry_min = tuning.retry_min_wait if tuning else None
    _autotune_retry_max = tuning.retry_max_wait if tuning else None
    _autotune_reconnect = tuning.reconnect_delay if tuning else None
    _autotune_concurrency = tuning.concurrency if tuning else None

    # Resolve tuning values once: each resolver also tags the provenance, which
    # feeds AUTOTUNE_MANAGED_KEYS below (single encode site for the precedence
    # chain — no separate re-check of CLI params / env getters).
    _resolved_http_timeout, _http_timeout_src = _get_http_timeout_value(
        http_timeout, _autotune_timeout
    )
    _resolved_reconnect_delay, _reconnect_src = _get_reconnect_delay_value(
        reconnect_delay, _autotune_reconnect
    )
    _resolved_retry_min, _retry_min_src = _get_transfer_retry_min_wait_value(_autotune_retry_min)
    _resolved_retry_max, _retry_max_src = _get_transfer_retry_max_wait_value(_autotune_retry_max)

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
            "X-Protocol-Version": PROTOCOL_VERSION_REST,
        },
        "RECONNECT_DELAY": _resolved_reconnect_delay,
        "RESYNC": _get_resync_value(resync),
        "FILTER_PATH": filter_path,
        "STABILITY_DELAY": _get_stability_delay_value(stability_delay),
        "HTTP_TIMEOUT": _resolved_http_timeout,
        "MAX_WAIT_TIMEOUT": max_wait_timeout,
        "LAN_MODE": lan_mode,
        "LAN_PORT": lan_port or get_lan_port() or 0,
        "DISCOVERY_TIMEOUT": _get_discovery_timeout_value(discovery_timeout),
        "HEARTBEAT_INTERVAL": get_heartbeat_interval() or HEARTBEAT_INTERVAL,
        "PEER_TIMEOUT": get_peer_timeout() or PEER_TIMEOUT,
        "LAN_KEY": lan_key_from_fd or get_lan_key(),
        "LAN_GROUP": lan_group or get_lan_group(),
        "LAN_PERSISTENT": lan_persistent or get_lan_persistent(),
        "LAN_FORCE_MASTER": force_master,
        "LAN_NEVER_MASTER": never_master or get_never_master(),
        "LAN_REQUIRE_PAIRING": lan_require_pairing or get_lan_require_pairing(),
        "MASTER_CERT_FINGERPRINT": None,
        "IGNORE_DELETE": ignore_delete or get_ignore_delete(),
        "COMPRESSION_ENABLED": compression_enabled and get_compression_enabled(),
        "SERVER_COMPRESSION_ENABLED": False,  # Set during WebSocket handshake
        "DELTA_ENABLED": delta_enabled and get_delta_enabled(),
        "SERVER_DELTA_ENABLED": False,  # Set during WebSocket handshake / master election
        # CLI overrides for the delta size floor (None = use env settings)
        "DELTA_MIN_FILE_SIZE": delta_min_file_size,
        "DELTA_MIN_FILE_SIZE_LAN": delta_min_file_size_lan,
        "SYNC_DIRECTION": sync_direction,
        "MAX_BANDWIDTH_UPLOAD": max_bandwidth_upload,
        "MAX_BANDWIDTH_DOWNLOAD": max_bandwidth_download,
        "MIN_FREE_DISK_SPACE": min_free_disk_space,
        # Absolute cap (bytes) on a single inbound file, applied on top of the
        # peer-declared size (anti decompression-bomb); 0 = disabled.
        "MAX_INBOUND_SIZE": max_inbound_size * 1024 * 1024,
        "IGNORE_PATTERNS": ignore_patterns
        if ignore_patterns is not None
        else get_ignore_patterns(),
        "MASS_DELETE_GUARD": _get_mass_delete_guard_value(mass_delete_guard),
        "MASS_DELETE_THRESHOLD": _get_mass_delete_threshold_value(mass_delete_threshold),
        "MASS_DELETE_PERCENT": _get_mass_delete_percent_value(mass_delete_percent),
        "MAX_CONCURRENT_TRANSFERS": _get_max_concurrent_transfers_value(
            max_concurrent_transfers, _autotune_concurrency
        ),
        "TRANSFER_RETRY_ATTEMPTS": TRANSFER_RETRY_ATTEMPTS_DEFAULT,
        "TRANSFER_RETRY_MIN_WAIT": _resolved_retry_min,
        "TRANSFER_RETRY_MAX_WAIT": _resolved_retry_max,
        # Auto-tuning runtime state (updated below after set_config)
        "AUTOTUNE_ENABLED": autotune_enabled,
        "AUTOTUNE_STORAGE_TYPE": tuning.storage_type.value if (autotune_enabled and tuning) else "",
        # Keys the LAN post-discovery refresh may re-derive: only those
        # resolved from autotune/default (CLI/env overrides are preserved)
        "AUTOTUNE_MANAGED_KEYS": [
            key
            for key, src in (
                ("HTTP_TIMEOUT", _http_timeout_src),
                ("RECONNECT_DELAY", _reconnect_src),
                ("TRANSFER_RETRY_MIN_WAIT", _retry_min_src),
                ("TRANSFER_RETRY_MAX_WAIT", _retry_max_src),
            )
            if src in ("autotune", "default")
        ],
    }

    set_config(config)
    set_debug(debug)

    # Store the effective autotune decision for the reconnect path
    CONFIG["AUTOTUNE_ENABLED"] = autotune_enabled

    # Cache the autotune storage type so the per-cycle concurrency refresh
    # (sync.py) can re-derive without probing the filesystem again.
    if autotune_enabled and tuning:
        CONFIG["AUTOTUNE_STORAGE_TYPE"] = tuning.storage_type.value

    # When TLS verification is disabled (--insecure), silence urllib3's
    # per-request InsecureRequestWarning once up-front. The connectivity check
    # and clock-skew probe issue raw requests that bypass get_ssl_verify(), so
    # suppressing here ensures even the very first health check stays quiet.
    if config["NO_VERIFY_SSL"]:
        _suppress_insecure_warning()

    # Surface the bucket / LAN group on the structured channel so the GUI does
    # not have to scrape them from log wording.
    emit_event(UiEventName.bucket, bucket=config["REMOTE_BUCKET"])
    if config["LAN_GROUP"]:
        emit_event(UiEventName.group, group=config["LAN_GROUP"])

    # Install the process-wide IgnoreMatcher so that all call sites that use
    # is_hidden_file / is_hidden_dir automatically benefit from the new filtering.
    _matcher = IgnoreMatcher(
        bucket_root=local_path,
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

    # Enforce the LAN-key policy for unattended write-capable sync.
    if not dryrun:
        _check_lan_write_policy(
            lan_mode=config["LAN_MODE"],
            lan_key=config["LAN_KEY"],
            sync_direction=config["SYNC_DIRECTION"],
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
        log.info("  While watching, [LOCAL ...] marks events originated by this node's")
        log.info("  watcher, [REMOTE ...] events caused by a peer (master, slave or server).")
        log.info("  Unprefixed labels belong to full-sync reconciliation and transfers.")
        log_labels = {
            ActionLabel.IN_SYNC: "File already in sync (no action required)",
            ActionLabel.SKIP: "File skipped",
            ActionLabel.DOWNLOAD: "Download file from server/master",
            ActionLabel.UPLOAD: "Upload file to server/master",
            ActionLabel.RESUME: "Resume partial download/upload",
            ActionLabel.CREATE: "Create directory (reconcile or server log)",
            ActionLabel.MODIFY: "Modify file or directory content (reconcile/server)",
            ActionLabel.RENAME: "Rename/move file or directory (reconcile/server)",
            ActionLabel.MOVE: "Move/rename detected (debug trace)",
            ActionLabel.DELETE: "Delete file or directory (reconcile/server)",
            ActionLabel.PERMISSION: "Set file/directory permission",
            ActionLabel.SYMLINK: "Create symbolic link (reconcile/server)",
            ActionLabel.GUARD: "Mass-deletion guard tripped",
            ActionLabel.SECURITY: "Unsafe remote path rejected",
            ActionLabel.CONFLICT: "Conflict with remote version",
            ActionLabel.TOMBSTONE: "Deletion recorded while offline",
            ActionLabel.LOCAL_CREATE: "Local watcher: file/directory created",
            ActionLabel.LOCAL_MODIFY: "Local watcher: file/directory content modified",
            ActionLabel.LOCAL_RENAME: "Local watcher: file/directory renamed",
            ActionLabel.LOCAL_DELETE: "Local watcher: file/directory deleted",
            ActionLabel.LOCAL_SYMLINK: "Local watcher: symbolic link created",
            ActionLabel.LOCAL_IGNORE_DEL: "Local watcher: deletion skipped (ignore-delete mode)",
            ActionLabel.LOCAL_KEEP: "Local watcher: .hsynckeep sentinel created",
            ActionLabel.REMOTE_CREATE: "Peer-caused: file/directory created or applied",
            ActionLabel.REMOTE_MODIFY: "Peer-caused: file/directory content modified",
            ActionLabel.REMOTE_RENAME: "Peer-caused: file/directory renamed",
            ActionLabel.REMOTE_DELETE: "Peer-caused: file/directory deleted",
            ActionLabel.REMOTE_SYMLINK: "Peer-caused: symbolic link created",
            ActionLabel.REMOTE_IGNORE_DEL: "Peer-caused: deletion skipped (ignore-delete mode)",
            ActionLabel.REMOTE_SECURITY: "Peer-caused: unsafe path rejected",
            ActionLabel.REMOTE_ECHO: "Peer event echo suppressed (debug)",
            ActionLabel.REMOTE_DOWNLOAD: "Master serving file bytes to a peer",
        }
        for label, description in log_labels.items():
            log.info(f"  {label} {description}")

    # LAN Mode: Peer Discovery and Master Election
    if config["LAN_MODE"]:
        _warn_if_lan_in_container(log)
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

    # Diagnostic clock-skew check (server mode). LAN mode performs its own
    # measurement against the elected master after discovery.
    if not lan_mode:
        _warn_on_clock_skew(
            server_url=effective_server_url,
            timeout=min(config["HTTP_TIMEOUT"], 10),
            verify_ssl=not config["NO_VERIFY_SSL"],
            headers={k: str(v) for k, v in config["REQUEST_HEADERS"].items()},
            logger=log,
        )

    # Reset bandwidth throttle state for this session
    reset_bandwidth_throttle()

    # Log bandwidth limits if configured
    if (upload_limit := config.get("MAX_BANDWIDTH_UPLOAD")) is not None:
        log.info(f"Upload bandwidth limit: {format_bytes(float(upload_limit))}/s")
    if (download_limit := config.get("MAX_BANDWIDTH_DOWNLOAD")) is not None:
        log.info(f"Download bandwidth limit: {format_bytes(float(download_limit))}/s")

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
            format_bytes(CONFIG["MAX_BANDWIDTH_UPLOAD"]) + "/s"
            if CONFIG["MAX_BANDWIDTH_UPLOAD"]
            else "unlimited"
        )
        download_str = (
            format_bytes(CONFIG["MAX_BANDWIDTH_DOWNLOAD"]) + "/s"
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
        hint = diagnose_connection_error(e, server_url)
        if hint:
            logger.error(hint)
        else:
            logger.error(f"Cannot connect to server after retries: {server_url} ({e})")
        return False


def _warn_on_clock_skew(
    server_url: str,
    timeout: float,
    verify_ssl: bool,
    headers: dict[str, str],
    logger,
) -> None:
    """
    Emit a one-shot warning if the local clock is skewed from the server.

    Performs an NTP-like round-trip against ``/api/v1/time``. HSYNC relies on
    OS-level time synchronisation (NTP); a large offset can cause an incorrect
    sync direction for files modified close in time on different peers. This is
    purely diagnostic: timestamps are never silently rewritten.
    """
    from hsync_common.clock import CLOCK_SKEW_THRESHOLD, measure_clock_offset
    from requests import RequestException
    from requests import get as http_get

    try:
        result = measure_clock_offset(
            f"{server_url}/api/v1/time",
            http_get=http_get,
            headers=headers,
            verify=verify_ssl,
            timeout=timeout,
        )
        if result is None:
            logger.debug("Clock sync: /api/v1/time unavailable or missing timestamp, skipping")
            return

        offset, rtt = result
        if abs(offset) > CLOCK_SKEW_THRESHOLD:
            logger.warning(
                f"Clock skew detected with server: offset={offset:.1f}s, "
                f"RTT={rtt * 1000:.1f}ms. Synchronise the system clock (NTP); "
                f"large skew can cause an incorrect sync direction for files "
                f"modified close in time on different peers."
            )
        else:
            logger.debug(f"Clock sync with server: offset={offset:.3f}s, RTT={rtt * 1000:.1f}ms")
    except RequestException as e:
        logger.debug(f"Clock sync failed: {e}")
    except (ValueError, KeyError) as e:
        logger.debug(f"Clock sync: invalid response: {e}")
