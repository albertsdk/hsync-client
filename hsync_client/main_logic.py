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
from pathlib import Path
from typing import Any, Literal

from hsync_common.format import format_bytes
from hsync_common.ignore import IgnoreMatcher, set_active_matcher
from hsync_common.protocol.handshake import PROTOCOL_VERSION_REST
from hsync_common.runtime import detect_container, is_tty_interactive, runtime_summary

from hsync_client.config import (
    CONFIG,
    HsyncConfig,
    get_api_key,
    get_autotune_enabled,
    get_bootnode,
    get_compression_enabled,
    get_connectivity_retry_attempts,
    get_connectivity_retry_max_wait,
    get_connectivity_retry_min_wait,
    get_delta_enabled,
    get_discovery_timeout,
    get_heartbeat_interval,
    get_ignore_delete,
    get_ignore_patterns,
    get_mass_delete_guard,
    get_mass_delete_percent,
    get_mass_delete_threshold,
    get_max_concurrent_transfers,
    get_never_master,
    get_p2p_allow_unauthenticated_writes,
    get_p2p_group,
    get_p2p_key,
    get_p2p_listen_port,
    get_p2p_peer_download,
    get_p2p_peer_read_cap,
    get_p2p_peer_sources_max,
    get_p2p_require_pairing,
    get_peer_timeout,
    get_resync,
    get_server_url,
    get_stability_delay,
    load_env_file,
    set_config,
    set_debug,
)
from hsync_client.constants import (
    BOOTNODE_DEFAULT_PORT,
    CLIENT_NAME,
    CONNECTIVITY_RETRY_ATTEMPTS_DEFAULT,
    CONNECTIVITY_RETRY_MAX_WAIT_DEFAULT,
    CONNECTIVITY_RETRY_MIN_WAIT_DEFAULT,
    DISCOVERY_TIMEOUT_DEFAULT,
    FILE_STABILITY_INTERVAL_DEFAULT,
    HEADER_CLIENT_NAME,
    HEADER_CLIENT_VERSION,
    HEADER_PROTOCOL_VERSION,
    HEARTBEAT_INTERVAL,
    HSYNC_VERSION,
    HSYNCKEEP_FILENAME,
    MAX_INBOUND_SIZE_MB_DEFAULT,
    PEER_TIMEOUT,
    RESYNC_DEFAULT,
    TRANSFER_RETRY_ATTEMPTS_DEFAULT,
    TRANSFER_RETRY_MAX_WAIT_DEFAULT,
    TRANSFER_RETRY_MIN_WAIT_DEFAULT,
)
from hsync_client.events import UiEventName, emit_event, init_emitter
from hsync_client.exitcodes import ExitCode, exit_code_for_result
from hsync_client.logger import flush_logs, set_client_id, setup_logging
from hsync_client.preview import _run_preview, _run_stdin_wait, _with_stdin_commands
from hsync_client.sync import (
    log_initial_sync_start,
    sync,
)
from hsync_client.utils import (
    _suppress_insecure_warning,
    diagnose_connection_error,
    get_cid,
    http_to_ws,
    is_valid_logfile,
    reset_bandwidth_throttle,
)


def _read_secret_from_fd(fd: int | None) -> str | None:
    """Read a secret string from a file descriptor, or None if unavailable."""
    if fd is None:
        return None
    import os

    try:
        with os.fdopen(fd, "r") as f:
            value = f.read().strip()
            return value or None
    except Exception:
        return None


# Provenance of a resolved tuning value; feeds AUTOTUNE_MANAGED_KEYS so the P2P
# post-discovery refresh re-derives only autotune/default-sourced keys and never
# clobbers explicit CLI/env overrides. Single encode site: the resolvers below.
TuningSource = Literal["cli", "env", "autotune", "default"]


def _first_defined[T](cli: T | None, env: T | None, default: T) -> T:
    """CLI > env > default."""
    if cli is not None:
        return cli
    if env is not None:
        return env
    return default


def _resolve_with_provenance[T](
    cli: T | None,
    env: T | None,
    default: T,
    autotune: T | None = None,
) -> tuple[T, TuningSource]:
    """CLI > env > autotune > default, with a provenance tag."""
    if cli is not None:
        return cli, "cli"
    if env is not None:
        return env, "env"
    if autotune is not None:
        return autotune, "autotune"
    return default, "default"


def _get_stability_delay_value(cli_value: int | None) -> int:
    """Priority: CLI argument > environment variable > default constant."""
    return _first_defined(cli_value, get_stability_delay(), FILE_STABILITY_INTERVAL_DEFAULT)


def _get_discovery_timeout_value(cli_value: int | None) -> int:
    """Priority: CLI > env > default. ``0`` means wait indefinitely."""
    return _first_defined(cli_value, get_discovery_timeout(), DISCOVERY_TIMEOUT_DEFAULT)


def _get_resync_value(cli_value: int | None) -> int | None:
    """Priority: CLI > env > default. Env ``0`` disables periodic resync."""
    if cli_value is not None:
        return cli_value
    env_value = get_resync()
    if env_value is not None:
        return env_value or None
    return RESYNC_DEFAULT


def _get_max_concurrent_transfers_value(
    cli_value: int | None, autotune: int | None = None
) -> tuple[int, TuningSource]:
    """Priority: CLI > env > autotune > default. Provenance feeds AUTOTUNE_MANAGED_KEYS."""
    from hsync_client.constants import MAX_CONCURRENT_TRANSFERS_DEFAULT

    return _resolve_with_provenance(
        cli_value, get_max_concurrent_transfers(), MAX_CONCURRENT_TRANSFERS_DEFAULT, autotune
    )


def _get_http_timeout_value(
    cli_value: int | None, autotune: int | None = None
) -> tuple[int, TuningSource]:
    """Priority: CLI > env > autotune > default."""
    from hsync_client.config import get_http_timeout
    from hsync_client.constants import HTTP_TIMEOUT_DEFAULT

    return _resolve_with_provenance(cli_value, get_http_timeout(), HTTP_TIMEOUT_DEFAULT, autotune)


def _get_reconnect_delay_value(
    cli_value: int | None, autotune: int | None = None
) -> tuple[int, TuningSource]:
    """Priority: CLI > env > autotune > default."""
    from hsync_client.config import get_reconnect_delay
    from hsync_client.constants import RECONNECT_DELAY_DEFAULT

    return _resolve_with_provenance(
        cli_value, get_reconnect_delay(), RECONNECT_DELAY_DEFAULT, autotune
    )


def _get_transfer_retry_min_wait_value(autotune: int | None = None) -> tuple[float, TuningSource]:
    """Priority: env > autotune > default (no CLI flag)."""
    from hsync_client.config import get_transfer_retry_min_wait

    env_value = get_transfer_retry_min_wait()
    value, source = _resolve_with_provenance(
        None, env_value, TRANSFER_RETRY_MIN_WAIT_DEFAULT, autotune
    )
    return float(value), source


def _get_transfer_retry_max_wait_value(autotune: int | None = None) -> tuple[float, TuningSource]:
    """Priority: env > autotune > default (no CLI flag)."""
    from hsync_client.config import get_transfer_retry_max_wait

    env_value = get_transfer_retry_max_wait()
    value, source = _resolve_with_provenance(
        None, env_value, TRANSFER_RETRY_MAX_WAIT_DEFAULT, autotune
    )
    return float(value), source


def _get_mass_delete_guard_value(cli_value: bool | None) -> bool:
    """Priority: CLI > env > default."""
    if cli_value is not None:
        return cli_value
    return get_mass_delete_guard()


def _get_mass_delete_threshold_value(cli_value: int | None) -> int:
    """Priority: CLI > env > default."""
    if cli_value is not None:
        return cli_value
    return get_mass_delete_threshold()


def _get_mass_delete_percent_value(cli_value: int | None) -> int:
    """Priority: CLI > env > default."""
    if cli_value is not None:
        return cli_value
    return get_mass_delete_percent()


def _check_deletion_risk(
    local_path: str,
    p2p_mode: bool,
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

    # force_master in P2P mode means this peer serves files to slaves — the
    # deletion-risk guard concerns serving, not authority (no peer is
    # authoritative under the causal model).
    if p2p_mode and force_master:
        return

    # Only download-only mode can wipe local-only files at the initial sync.
    # Bidirectional (server or P2P) uploads local-only files instead of
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

    warnings = [
        f"The shared directory '{local_path}' is not empty and the current "
        f"sync settings could DELETE local files.",
        f"Reason: {reason}",
    ]
    for line in warnings:
        logger.warning(line)

    if auto_confirm:
        logger.warning("--yes flag set – proceeding without confirmation.")
        return

    # No operator can answer the prompt without an interactive terminal
    # (containers, systemd units, cron, pipes): fail fast with a clear
    # message instead of blocking on input() or silently declining via EOF.
    if not is_tty_interactive():
        error = (
            "Deletion-risk confirmation required but no interactive terminal is "
            "available. Re-run with --yes to accept the risk and proceed."
        )
        logger.error(error)
        # The loguru sinks are asynchronous (enqueue=True): a message written
        # right before SystemExit can be lost. Echo the full diagnostic through
        # the synchronous CLI error channel too, so the operator always sees why
        # the run aborted.
        print("Error: " + " ".join([*warnings, error]), file=sys.stderr)
        raise SystemExit(1)

    try:
        response = input("Do you want to continue? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        response = ""

    if response not in ("y", "yes"):
        logger.info("Sync cancelled by user.")
        raise SystemExit(0)


def _check_p2p_write_policy(
    p2p_mode: bool,
    p2p_key: str | None,
    sync_direction: str,
    logger: Any,
    allow_unauthenticated_writes: bool = False,
) -> None:
    """Require authorization for every write-capable P2P sync.

    A self-signed P2P transport hides traffic but does not prove the master's
    identity, so a client that uploads data must authenticate with
    ``HSYNC_P2P_KEY`` unless the dangerous explicit opt-in is enabled. The policy lives in
    :func:`hsync_common.protocol.pairing.p2p_write_requires_key`; here we apply it.

    Raises SystemExit when the policy is violated.
    """
    if not p2p_mode:
        return

    if p2p_key:
        return

    from hsync_client.preview import _p2p_write_blocked

    if _p2p_write_blocked(
        sync_direction,
        allow_unauthenticated_writes=allow_unauthenticated_writes,
        log=logger,
        hint=(
            "Set HSYNC_P2P_KEY, use --sync-direction download-only, or explicitly accept "
            "the risk with --allow-unauthenticated-writes."
        ),
    ):
        raise SystemExit(1)


def _warn_if_p2p_in_container(logger: Any) -> None:
    """Warn that mDNS peer discovery usually fails under container networking.

    P2P discovery relies on multicast, which does not cross the NAT boundary
    of the default bridge network used by container runtimes; P2P mode inside
    a container therefore requires host networking to find peers.
    """
    info = detect_container()
    if not info.is_container:
        return
    logger.warning(
        f"P2P mode inside a {info.runtime or 'unknown'} container: mDNS peer "
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
    """Start a watcher that reloads *matcher* when ``.hsyncignore`` changes.

    Returns the running :class:`~hsync_client.watcher.WatcherManager` (so the
    caller can stop it on shutdown), or ``None`` if watchdog is unavailable or
    the directory does not exist.
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)

    try:
        from hsync_common.constants import HSYNCIGNORE_FILENAME
        from watchdog.events import FileSystemEventHandler

        class _HsyncignoreHandler(FileSystemEventHandler):
            def on_modified(self, event) -> None:  # type: ignore[override]
                if Path(event.src_path).name == HSYNCIGNORE_FILENAME:
                    _log.debug(".hsyncignore changed — reloading ignore matcher")
                    matcher.reload()
                    from hsync_client.p2p_inventory import refresh_active_inventory

                    refresh_active_inventory()

            def on_created(self, event) -> None:  # type: ignore[override]
                self.on_modified(event)

        if not Path(bucket_root).exists():
            return None

        from hsync_client.watcher import WatcherManager

        # On an inotify-limit failure the watcher degrades to polling (logged
        # with [WATCHER FALLBACK]) instead of leaving the hot-reload off.
        watcher = WatcherManager(bucket_root, recursive=False)
        watcher.start(_HsyncignoreHandler())
        _log.debug(f"Hot-reload watcher started for {HSYNCIGNORE_FILENAME} in {bucket_root}")
        return watcher
    except Exception as exc:  # pragma: no cover
        _log.warning(f"Could not start .hsyncignore hot-reload watcher: {exc}")
        return None


def _safe_config_for_log(config: HsyncConfig, env_file: str | None) -> dict[str, Any]:
    """Build the redacted config dump for the verbose "Local configuration" log.

    Masks ``API_KEY``, ``P2P_KEY`` and the ``Authorization`` header, and appends
    a ``PATHS`` section resolving the filesystem locations the client uses
    (env file, device identity dir, state dir, SQLite DB, instance-lock dir).
    """
    from hsync_common.models.folder import default_state_dir, default_syncstate_db_path

    from hsync_client.identity import default_device_dir, default_locks_dir
    from hsync_client.utils import redact_headers

    safe: dict[str, Any] = {**config}
    safe["REQUEST_HEADERS"] = redact_headers(config["REQUEST_HEADERS"])
    safe["API_KEY"] = "****"
    if safe.get("P2P_KEY"):
        safe["P2P_KEY"] = "****"
    safe["PATHS"] = {
        "ENV_FILE": env_file,
        "DEVICE_DIR": str(default_device_dir()),
        "STATE_DIR": str(default_state_dir()),
        "STATE_DB": str(default_syncstate_db_path()),
        "LOCKS_DIR": str(default_locks_dir()),
    }
    return safe


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
    p2p_mode: bool,
    p2p_listen_port: int,
    discovery_timeout: int | None,
    p2p_group: str | None,
    force_master: bool,
    never_master: bool,
    debug: bool,
    quiet: bool,
    logfile: str | None,
    no_color: bool,
    p2p_require_pairing: bool = False,
    api_key_fd: int | None = None,
    p2p_key_fd: int | None = None,
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
    delta_min_file_size_p2p: int | None = None,
    p2p_peer_download_enabled: bool = True,
    p2p_allow_unauthenticated_writes: bool = False,
    p2p_bootnode: str | None = None,
    p2p_bootnode_port: int | None = None,
    p2p_upnp_enabled: bool | None = None,
) -> None:
    """
    Main synchronization logic.

    This function orchestrates the entire sync process including:
    - Loading environment files
    - Setting up logging
    - Validating configuration
    - Running sync (single or continuous)

    By default the client runs continuously (daemon): it keeps the connection
    up and retries an unreachable server forever, mirroring P2P mode. A
    one-shot run (CLI ``--once``) maps to ``continuous=False`` and exits after
    a single cycle (or after the finite startup connectivity retries).
    """
    # Read secrets from file descriptors if provided (secure passing from parent)
    api_key_from_fd = _read_secret_from_fd(api_key_fd)
    p2p_key_from_fd = _read_secret_from_fd(p2p_key_fd)

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
        sys.exit(1)

    # Validate log file path
    if logfile and not is_valid_logfile(logfile):
        log.error(f"Invalid output file '{logfile}'")
        sys.exit(1)

    # Determine server URL (CLI argument takes precedence over env var)
    effective_server_url = server_url or get_server_url()

    # P2P mode without a configured server URL runs on a placeholder until the
    # master is discovered: the RTT probe and the UI server event are deferred
    # (probing the placeholder would always fail with a spurious warning).
    using_p2p_placeholder = p2p_mode and not effective_server_url

    # Validate server URL is provided (not required in P2P mode)
    if not effective_server_url and not p2p_mode:
        log.error("Server URL is required (use 'hsync server HOSTNAME' or set HSYNC_SERVER)")
        sys.exit(1)

    # In P2P mode without server URL, use placeholder
    if p2p_mode and not effective_server_url:
        effective_server_url = "http://localhost:9753"

    # At this point, effective_server_url is guaranteed to be set
    # (either from args, env, or P2P mode placeholder)
    assert effective_server_url is not None
    if not using_p2p_placeholder:
        emit_event(UiEventName.server, url=effective_server_url)

    # Convert HTTP URL to WebSocket URL for events endpoint
    ws_url = http_to_ws(effective_server_url)

    # Client identity. P2P mode presents the persistent device identity
    # (Syncthing-style: one device ID per installation, anchored in
    # $HSYNC_DEVICE_DIR) so TOFU pins survive restarts and a changed
    # certificate for the same device ID is detectable. Remote mode keeps a
    # per-session random CID: it only feeds echo suppression — the API key
    # is the identity there.
    if p2p_mode:
        from hsync_client.identity import load_or_create_device_identity

        cid = load_or_create_device_identity().device_id
        set_client_id(cid)
        log.info(f"Device ID: {cid}")
    else:
        cid = get_cid()
        set_client_id(cid)
        log.info(f"Session CID: {cid}")
    emit_event(UiEventName.session, cid=cid)

    # Resolve API key once: FD-pipe value takes precedence over env var
    resolved_api_key = api_key_from_fd or get_api_key()
    resolved_p2p_key = p2p_key_from_fd or get_p2p_key()
    allow_unauthenticated_p2p_writes = (
        p2p_allow_unauthenticated_writes or get_p2p_allow_unauthenticated_writes()
    )

    # Fail fast on a missing API key: every authenticated server endpoint
    # requires a valid key, so an empty key can never sync. Reporting it here
    # avoids a late, opaque "Invalid API key" failure mid-sync. P2P mode has
    # its own shared-key policy and is exempt.
    if not p2p_mode and not resolved_api_key:
        log.error(
            "No API key configured. Set HSYNC_API_KEY (or pass it via "
            "--api-key-fd) to authenticate with the server."
        )
        sys.exit(1)

    # Security gate: never transmit the API key over an unencrypted channel.
    # The rule keys on the URL scheme the client connects to, so an HTTPS
    # reverse proxy in front of an HTTP backend is fully supported (the
    # client uses an https:// URL; the backend stays HTTP behind the proxy).
    if not p2p_mode and not effective_server_url.lower().startswith("https://"):
        log.error(
            f"API key provided but server URL is not HTTPS ('{effective_server_url}'). "
            "Refusing to send credentials over an unencrypted connection. "
            "Use an https:// URL (a reverse proxy terminating TLS is supported)."
        )
        sys.exit(1)

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
                measure_bandwidth_active,
                measure_rtt,
                profile_transfer_queue,
            )

            parsed = urlparse(effective_server_url)
            rtt_host = parsed.hostname or "localhost"
            rtt_port = parsed.port or (443 if parsed.scheme == "https" else 80)

            # P2P placeholder: no server to probe yet — defer RTT measurement
            # to the post-discovery refresh in p2p_handler (slave role only).
            rtt_ms = None if using_p2p_placeholder else measure_rtt(rtt_host, rtt_port)
            net_class = classify_network(rtt_ms)
            # Active bandwidth probe seeds the class at startup when the passive
            # EMA has no data yet (no transfers have run). Skipped for the P2P
            # placeholder — no server URL exists until a master is discovered.
            active_mbps = None
            if not using_p2p_placeholder and autotune_mod._bw_ema is None:
                active_mbps = measure_bandwidth_active(effective_server_url, verify=verify_ssl)
            bw_class = classify_bandwidth(autotune_mod._bw_ema, active_mbps)
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
    _resolved_max_concurrent, _max_concurrent_src = _get_max_concurrent_transfers_value(
        max_concurrent_transfers, _autotune_concurrency
    )

    # Single source of truth for the tuning keys: both the user-override log
    # (cli/env sources) and AUTOTUNE_MANAGED_KEYS (autotune/default sources)
    # are derived from this sequence, so the two can never drift apart.
    # Tuple: (config key, display label, resolved value, provenance, unit).
    _tuning_keys = [
        ("HTTP_TIMEOUT", "timeout", _resolved_http_timeout, _http_timeout_src, "s"),
        ("RECONNECT_DELAY", "reconnect", _resolved_reconnect_delay, _reconnect_src, "s"),
        ("TRANSFER_RETRY_MIN_WAIT", "retry_min", _resolved_retry_min, _retry_min_src, "s"),
        ("TRANSFER_RETRY_MAX_WAIT", "retry_max", _resolved_retry_max, _retry_max_src, "s"),
        (
            "MAX_CONCURRENT_TRANSFERS",
            "concurrency",
            _resolved_max_concurrent,
            _max_concurrent_src,
            "",
        ),
    ]

    # Surface user overrides so the startup summary reflects the EFFECTIVE tuning,
    # not just autotune's recommendation (e.g. --max-concurrent-transfers 20 stays
    # 20 even when autotune would derive 4). Only logged when something is set
    # explicitly via CLI/env, to avoid noise on a fully-autotuned run.
    _overrides = [
        f"{label}={value}{unit} ({src})"
        for _key, label, value, src, unit in _tuning_keys
        if src in ("cli", "env")
    ]
    if _overrides:
        log.info(f"autotune: user overrides in effect: {', '.join(_overrides)}")

    # Connectivity check retry (env var or default; not autotune-managed)
    _conn_retry_attempts = get_connectivity_retry_attempts()
    if _conn_retry_attempts is None:
        _conn_retry_attempts = CONNECTIVITY_RETRY_ATTEMPTS_DEFAULT
    _conn_retry_min = get_connectivity_retry_min_wait()
    if _conn_retry_min is None:
        _conn_retry_min = float(CONNECTIVITY_RETRY_MIN_WAIT_DEFAULT)
    _conn_retry_max = get_connectivity_retry_max_wait()
    if _conn_retry_max is None:
        _conn_retry_max = float(CONNECTIVITY_RETRY_MAX_WAIT_DEFAULT)

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
            "Authorization": f"Bearer {resolved_p2p_key or '' if p2p_mode else resolved_api_key}",
            HEADER_CLIENT_NAME: CLIENT_NAME,
            HEADER_CLIENT_VERSION: HSYNC_VERSION,
            HEADER_PROTOCOL_VERSION: PROTOCOL_VERSION_REST,
        },
        "RECONNECT_DELAY": _resolved_reconnect_delay,
        "RESYNC": _get_resync_value(resync),
        "FILTER_PATH": filter_path,
        "STABILITY_DELAY": _get_stability_delay_value(stability_delay),
        "HTTP_TIMEOUT": _resolved_http_timeout,
        "MAX_WAIT_TIMEOUT": max_wait_timeout,
        "P2P_MODE": p2p_mode,
        "P2P_LISTEN_PORT": p2p_listen_port or get_p2p_listen_port() or 0,
        "BOOTNODE": p2p_bootnode or get_bootnode(),
        "BOOTNODE_PORT": p2p_bootnode_port or BOOTNODE_DEFAULT_PORT,
        "UPNP_ENABLED": (
            p2p_upnp_enabled
            if p2p_upnp_enabled is not None
            else bool(p2p_bootnode or get_bootnode())
        ),
        "DISCOVERY_TIMEOUT": _get_discovery_timeout_value(discovery_timeout),
        "HEARTBEAT_INTERVAL": get_heartbeat_interval() or HEARTBEAT_INTERVAL,
        "PEER_TIMEOUT": get_peer_timeout() or PEER_TIMEOUT,
        "P2P_KEY": resolved_p2p_key,
        "P2P_GROUP": p2p_group or get_p2p_group(),
        # One-shot is derived solely from --once: continuous is the default
        # and no env/flag knob for "persistence" exists.
        "ONE_SHOT": not continuous,
        "P2P_FORCE_MASTER": force_master,
        "P2P_NEVER_MASTER": never_master or get_never_master(),
        "P2P_REQUIRE_PAIRING": p2p_require_pairing or get_p2p_require_pairing(),
        "P2P_PEER_DOWNLOAD": p2p_peer_download_enabled and get_p2p_peer_download(),
        "P2P_PEER_READ_CAP": get_p2p_peer_read_cap(),
        "P2P_PEER_SOURCES_MAX": get_p2p_peer_sources_max(),
        "P2P_ALLOW_UNAUTHENTICATED_WRITES": allow_unauthenticated_p2p_writes,
        "MASTER_CERT_FINGERPRINT": None,
        "IGNORE_DELETE": ignore_delete or get_ignore_delete(),
        "COMPRESSION_ENABLED": compression_enabled and get_compression_enabled(),
        "SERVER_COMPRESSION_ENABLED": False,  # Set during WebSocket handshake
        "DELTA_ENABLED": delta_enabled and get_delta_enabled(),
        "SERVER_DELTA_ENABLED": False,  # Set during WebSocket handshake / master election
        # CLI overrides for the delta size floor (None = use env settings)
        "DELTA_MIN_FILE_SIZE": delta_min_file_size,
        "DELTA_MIN_FILE_SIZE_P2P": delta_min_file_size_p2p,
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
        "MAX_CONCURRENT_TRANSFERS": _resolved_max_concurrent,
        "TRANSFER_RETRY_ATTEMPTS": TRANSFER_RETRY_ATTEMPTS_DEFAULT,
        "TRANSFER_RETRY_MIN_WAIT": _resolved_retry_min,
        "TRANSFER_RETRY_MAX_WAIT": _resolved_retry_max,
        "CONNECTIVITY_RETRY_ATTEMPTS": _conn_retry_attempts,
        "CONNECTIVITY_RETRY_MIN_WAIT": _conn_retry_min,
        "CONNECTIVITY_RETRY_MAX_WAIT": _conn_retry_max,
        # Auto-tuning runtime state (updated below after set_config)
        "AUTOTUNE_ENABLED": autotune_enabled,
        "AUTOTUNE_STORAGE_TYPE": tuning.storage_type.value if (autotune_enabled and tuning) else "",
        # Keys the P2P post-discovery refresh AND the per-cycle concurrency
        # refresh may re-derive: only those resolved from autotune/default
        # (CLI/env overrides are preserved). Derived from `_tuning_keys` so it
        # stays in sync with the user-override log above.
        "AUTOTUNE_MANAGED_KEYS": [
            key
            for key, _label, _value, src, _unit in _tuning_keys
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
    # per-request InsecureRequestWarning once up-front so that even the very
    # first autotune bandwidth probe stays quiet.
    if config["NO_VERIFY_SSL"]:
        _suppress_insecure_warning()

    # Surface the bucket / P2P group on the structured channel so the GUI does
    # not have to scrape them from log wording.
    emit_event(UiEventName.bucket, bucket=config["REMOTE_BUCKET"])
    if config["P2P_GROUP"]:
        emit_event(UiEventName.group, group=config["P2P_GROUP"])

    # Install the process-wide IgnoreMatcher so that all call sites that use
    # is_hidden_file / is_hidden_dir automatically benefit from the new filtering.
    _matcher = IgnoreMatcher(
        bucket_root=local_path,
        extra_patterns=config["IGNORE_PATTERNS"],
    )
    set_active_matcher(_matcher)

    # Hot-reload watcher: rebuild the matcher when .hsyncignore changes.
    _hsyncignore_observer = _start_hsyncignore_watcher(local_path, _matcher)

    # Check for file deletion risk before syncing
    if not dryrun:
        _check_deletion_risk(
            local_path=local_path,
            p2p_mode=p2p_mode,
            force_master=force_master,
            sync_direction=sync_direction,
            auto_confirm=auto_confirm,
            logger=log,
        )

    # Enforce the P2P-key policy for every write-capable sync.
    if not dryrun:
        _check_p2p_write_policy(
            p2p_mode=config["P2P_MODE"],
            p2p_key=config["P2P_KEY"],
            sync_direction=config["SYNC_DIRECTION"],
            logger=log,
            allow_unauthenticated_writes=config["P2P_ALLOW_UNAUTHENTICATED_WRITES"],
        )

    # Check disk space before syncing
    if not dryrun:
        _check_disk_space(local_path, min_free_disk_space, log)

    # Mask sensitive values before logging (shared helper, DRY with transfer.py)
    log.debug("Local configuration:", extra={"json_data": _safe_config_for_log(config, env_file)})

    # Cross-process instance lock: two hsync processes must never sync the
    # same local directory — they would race the same folder UUID in
    # syncstate.db and echo each other's watcher events. The
    # flock is held for the whole run (P2P or remote) and is released by the
    # kernel even on SIGKILL; see hsync_client.identity.InstanceLock.
    instance_lock = _acquire_instance_lock(local_path, log)
    try:
        # P2P Mode: Peer Discovery and Master Election
        if config["P2P_MODE"]:
            from hsync_client.p2p_handler import run_p2p_mode

            _warn_if_p2p_in_container(log)
            sys.exit(run_p2p_mode(config, log))

        # Server Connectivity Check with retry. In one-shot mode an
        # unreachable server is a failed run: exit non-zero instead of
        # returning silently with a success exit code. In continuous mode
        # (the default) the client stays alive and the reconnect loop below
        # retries the connection forever, mirroring P2P mode.
        if not _check_server_connectivity(
            server_url=effective_server_url,
            timeout=min(config["HTTP_TIMEOUT"], 10),
            verify_ssl=not config["NO_VERIFY_SSL"],
            logger=log,
        ):
            if not continuous:
                sys.exit(1)
            log.warning(
                "Server unreachable at startup; keeping the client alive and "
                "retrying (continuous mode)."
            )

        # Clock offset (ADR-0023): the server is the time authority; failures
        # degrade to offset 0 and never block the run.
        from hsync_client.timesync import probe_server_clock_offset

        probe_server_clock_offset(effective_server_url, logger=log)

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
        elif config["DRYRUN"]:
            # One-shot dry-run: show the preview only — running sync() here would
            # perform real transfers despite --dry-run.
            _run_preview(log)
        else:
            # One-shot sync. The exit code feeds the shell from here on: a
            # completed cycle maps its verdict (transfer errors, deletion
            # guard, conflicts) through the documented code table, while
            # fatal failures already logged by sync() become FATAL (1)
            # without a raw traceback on stderr.
            try:
                log_initial_sync_start()
                result = run(sync())
            except SystemExit:
                raise
            except Exception:
                # sync() already logged the error and emitted the structured
                # event (or the debug traceback when verbose). A bare
                # exception here must simply fail the run.
                sys.exit(int(ExitCode.FATAL))
            sys.exit(
                exit_code_for_result(
                    failed_transfers=result.failed,
                    guard_tripped=result.guard_tripped,
                    conflicts=result.conflicts,
                )
            )
    finally:
        # The async sinks must be drained before the process leaves: this
        # finally runs for every exit inside the run (including the sys.exit
        # calls that map the cycle verdict to an exit code), so the last
        # diagnostic line is never lost to the writer-thread race.
        flush_logs()
        # Compact syncstate.db once at process exit (best-effort): the state
        # stores are no longer writing here, so the VACUUM cannot race a flush.
        from hsync_client.state_maintenance import vacuum_on_shutdown

        vacuum_on_shutdown()
        instance_lock.release()
        # Second drain: lines emitted by the shutdown steps above are flushed
        # too (the first call already guaranteed the fatal diagnostic).
        flush_logs()


def _acquire_instance_lock(local_path: str, log) -> "Any":
    """Acquire the per-directory instance lock, failing fast with a clear error.

    Two live hsync processes on the same synced directory would corrupt each
    other's folder state; the lock holder's PID (when readable) is included
    in the message so the conflicting process is easy to find.
    """
    from hsync_client.identity import InstanceLock, InstanceLockError

    try:
        return InstanceLock.acquire(local_path)
    except InstanceLockError as e:
        log.error(str(e))
        # The message IS the refusal: with asynchronous sinks it must be
        # flushed before the process terminates or the user sees a bare
        # non-zero exit with no explanation.
        flush_logs()
        sys.exit(1)


async def _run_with_stdin_commands(log) -> None:
    """Run remote_connect with a background stdin command reader."""
    from hsync_client.remote_websocket import remote_connect

    await _with_stdin_commands(log, remote_connect())


def _check_server_connectivity(
    server_url: str,
    timeout: float,
    verify_ssl: bool,
    logger,
) -> bool:
    """
    Check server connectivity with automatic retry on transient failures.

    Uses tenacity for exponential backoff retry. Attempt count, min wait, and
    max wait are read from CONFIG (settable via HSYNC_CONNECTIVITY_RETRY_*
    environment variables); defaults: 7 attempts, 1-30 s wait.

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
    from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

    max_attempts = int(
        CONFIG.get("CONNECTIVITY_RETRY_ATTEMPTS", CONNECTIVITY_RETRY_ATTEMPTS_DEFAULT)
    )
    min_wait = float(CONFIG.get("CONNECTIVITY_RETRY_MIN_WAIT", CONNECTIVITY_RETRY_MIN_WAIT_DEFAULT))
    max_wait = float(CONFIG.get("CONNECTIVITY_RETRY_MAX_WAIT", CONNECTIVITY_RETRY_MAX_WAIT_DEFAULT))

    def _before_sleep_health(retry_state):
        logger.warning(
            f"Server connectivity check attempt {retry_state.attempt_number}/{max_attempts} "
            f"failed; retrying in {retry_state.next_action.sleep:.0f}s..."
        )

    @retry(
        retry=retry_if_exception_type(RequestException),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        before_sleep=_before_sleep_health,
        reraise=True,
    )
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
