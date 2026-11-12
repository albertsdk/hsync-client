# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso


"""
HSYNC Client - Main synchronization logic.

This module contains the core synchronization logic extracted from main.py.
"""

import sys
from asyncio import run
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hsync_common.disk import check_free_space
from hsync_common.format import format_bytes
from hsync_common.ignore import IgnoreMatcher, set_active_matcher
from hsync_common.runtime import runtime_summary

from hsync_client.asyncio_utils import is_event_loop_teardown_error
from hsync_client.config import (
    CONFIG,
    HsyncConfig,
    load_env_file,
    set_config,
    set_debug,
)
from hsync_client.config_build import (
    _build_config,
    _ResolvedSecrets,
    _ResolvedTuning,
)
from hsync_client.constants import (
    CONNECTIVITY_RETRY_ATTEMPTS_DEFAULT,
    CONNECTIVITY_RETRY_MAX_WAIT_DEFAULT,
    CONNECTIVITY_RETRY_MIN_WAIT_DEFAULT,
    HSYNC_VERSION,
)
from hsync_client.events import UiEventName, emit_event, init_emitter
from hsync_client.exitcodes import ExitCode, exit_code_for_result
from hsync_client.logger import flush_logs, setup_logging
from hsync_client.preview import _run_preview, _run_stdin_wait, _with_stdin_commands
from hsync_client.run_options import RunOptions
from hsync_client.runtime_resolvers import (
    _resolve_autotune,
    _resolve_endpoints,
    _resolve_identity,
    _resolve_secrets,
)
from hsync_client.security_warnings import log_active_bypasses
from hsync_client.server_trust_resolution import (
    ServerTrustError,
    _evaluate_server_trust,
)
from hsync_client.startup_checks import (
    _check_deletion_risk,
    _check_p2p_keyless_policy,
    _check_p2p_write_policy,
    _fatal_startup,
    _warn_if_p2p_in_container,
)
from hsync_client.sync import (
    log_initial_sync_start,
    sync,
)
from hsync_client.utils import (
    _suppress_insecure_warning,
    diagnose_connection_error,
    is_valid_logfile,
    reset_bandwidth_throttle,
)


def _check_disk_space(local_path: str, min_free_bytes: int, logger: Any) -> None:
    """Check if sufficient disk space is available before sync.

    Args:
        local_path: Path to the local sync directory.
        min_free_bytes: Minimum free disk space required, in bytes (0 = disabled).
        logger: Logger instance for output.

    Raises:
        InsufficientDiskSpaceError: If free space is below the threshold.
    """
    if min_free_bytes <= 0:
        return

    from hsync_client.exceptions import InsufficientDiskSpaceError

    space = check_free_space(local_path, reserve_bytes=min_free_bytes)
    if space.error is not None:
        logger.warning(f"Could not check disk space: {space.error}")
        return
    if not space.ok:
        raise InsufficientDiskSpaceError(
            message=(
                f"Insufficient disk space: {format_bytes(space.free_bytes)} available, "
                f"{format_bytes(min_free_bytes)} required"
            ),
            details={"available_bytes": space.free_bytes, "required_bytes": min_free_bytes},
        )


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
                    from hsync_client.p2p_inventory_publisher import refresh_active_inventory

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
    from hsync_common.models.folder import default_state_dir, state_db_override

    from hsync_client.identity import default_device_dir, default_locks_dir
    from hsync_client.utils import redact_headers

    safe: dict[str, Any] = {**config}
    safe["REQUEST_HEADERS"] = redact_headers(config["REQUEST_HEADERS"])
    safe["API_KEY"] = "****"
    if safe.get("P2P_KEY"):
        safe["P2P_KEY"] = "****"
    override = state_db_override()
    safe["PATHS"] = {
        "ENV_FILE": env_file,
        "DEVICE_DIR": str(default_device_dir()),
        "STATE_DIR": str(default_state_dir()),
        # Explicit single-DB override only; the per-folder layout is resolved
        # lazily per bucket and has no single path.
        "STATE_DB": str(override) if override is not None else "",
        "LOCKS_DIR": str(default_locks_dir()),
    }
    return safe


def _init_runtime(options: RunOptions) -> "Any":
    """Load env/secrets, set up the emitter and logging; validate local paths.

    Returns the configured logger. Fatal startup conditions (missing
    ``--env-file``, missing local directory, invalid logfile) exit here with
    the documented codes before any sync-side state is touched.
    """
    # Install the structured event emitter. No-op when the parent did
    # not pass --events-json-fd, so a plain CLI run is unaffected.
    init_emitter(options.logging.events_json_fd)

    if options.secrets.env_file:
        try:
            load_env_file(options.secrets.env_file)
        except FileNotFoundError as e:
            _fatal_startup(str(e))

    log = setup_logging(
        debug=options.logging.debug,
        quiet=options.logging.quiet,
        logfile=(
            options.logging.logfile
            if options.logging.logfile and is_valid_logfile(options.logging.logfile)
            else None
        ),
        color=options.logging.color,
    )

    # ── PyInstaller workaround: prevent corrupt netrc module crash ──────
    # The ``requests`` library calls ``get_netrc_auth()`` on every HTTP
    # request, which does ``from netrc import ...``.  In some PyInstaller
    # bundles the ``netrc`` module is corrupted (zlib decompression error)
    # and the resulting ``zlib.error`` is *not* caught by requests'
    # ``except (ImportError, AttributeError)`` handler.
    # Pre-registering a lightweight stub in ``sys.modules`` avoids the
    # decompression entirely while keeping the ``requests`` code-path
    # happy (it will simply find no credentials and return ``None``).
    try:
        import netrc as _netrc_test  # noqa: F401 - probe only
    except Exception:
        from types import ModuleType

        _netrc_stub = ModuleType("netrc")
        _netrc_stub.netrc = lambda *_a, **_kw: None  # type: ignore[attr-defined]
        _netrc_stub.NetrcParseError = Exception  # type: ignore[attr-defined]
        sys.modules["netrc"] = _netrc_stub
        log.debug("Registered netrc stub module (PyInstaller workaround)")

    log.info(f"HSYNC Client v{HSYNC_VERSION}")
    log.info(runtime_summary())

    if not Path(options.sync.local_path).exists():
        log.error(f"Directory '{options.sync.local_path}' does not exist")
        emit_event(UiEventName.path_not_found, path=options.sync.local_path)
        sys.exit(1)

    if options.logging.logfile and not is_valid_logfile(options.logging.logfile):
        _fatal_startup(f"Invalid output file '{options.logging.logfile}'", log=log)
    return log


def _finish_startup(
    options: RunOptions,
    config: HsyncConfig,
    _t: _ResolvedTuning,
    resolved: _ResolvedSecrets,
    log,
) -> Any:
    """Cache autotune state, install the matcher/observer, take the run lock."""
    local_path = options.sync.local_path
    dryrun = options.sync.dryrun
    sync_direction = options.sync.sync_direction
    p2p_mode = options.p2p.p2p_mode
    prefer_master = options.p2p.prefer_master
    allow_deletions = resolved.allow_deletions
    min_free_disk_space = options.transfers.min_free_disk_space
    env_file = options.secrets.env_file
    autotune_enabled = _t.autotune_enabled
    tuning = _t.profile

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

    # Surface one concise warning per adopted security bypass, before any prompt.
    log_active_bypasses(
        log,
        allow_deletions=config["ALLOW_DELETIONS"],
        allow_unauthenticated_writes=config["P2P_ALLOW_UNAUTHENTICATED_WRITES"],
        allow_unauthenticated_p2p=config["P2P_ALLOW_UNAUTHENTICATED"],
        rendezvous_server_trust=config["RENDEZVOUS_SERVER_TRUST"],
        server_trust=config["SERVER_TRUST"],
    )

    if not dryrun:
        _check_deletion_risk(
            local_path=local_path,
            p2p_mode=p2p_mode,
            prefer_master=prefer_master,
            sync_direction=sync_direction,
            allow_deletions=allow_deletions,
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

    # Keyless P2P confirmation: authorization is orthogonal to a dry-run, which
    # still performs rendezvous/TLS/connection (and a keyless master still serves
    # reads), so the gate applies even when no actions are executed.
    _check_p2p_keyless_policy(
        p2p_mode=config["P2P_MODE"],
        p2p_key=config["P2P_KEY"],
        allow_unauthenticated_p2p=config["P2P_ALLOW_UNAUTHENTICATED"],
        allow_unauthenticated_writes=config["P2P_ALLOW_UNAUTHENTICATED_WRITES"],
        logger=log,
    )

    if not dryrun:
        _check_disk_space(local_path, min_free_disk_space, log)

    # Mask sensitive values before logging (shared helper, DRY with transfer.py)
    log.debug("Local configuration:", extra={"json_data": _safe_config_for_log(config, env_file)})

    # Cross-process instance lock: two hsync processes must never sync the
    # same local directory — they would race the same folder UUID in
    # state DB and echo each other's watcher events. The
    # flock is held for the whole run (P2P or remote) and is released by the
    # kernel even on SIGKILL; see hsync_client.identity.InstanceLock.
    instance_lock = _acquire_instance_lock(local_path, log)

    # The folder is ours: this is the explicit "the engine is running" marker.
    # The Desktop treats an exit before it as a startup failure (its tail carries
    # the client's own reason), so the instance-lock refusal above must stay on
    # the startup side of the marker. The gates still evaluated below (server
    # trust and connectivity in remote mode) refuse through the exit code and the
    # log line, which the Desktop reports as a fatal exit.
    emit_event(UiEventName.ready)
    return instance_lock


def _run_remote_mode(
    options: RunOptions, config: HsyncConfig, log, effective_server_url: str
) -> None:
    """Resolve server trust, probe connectivity, run the remote cycle loop."""
    continuous = options.sync.continuous

    # Server TLS trust (ADR-0036): resolve the pin BEFORE the connectivity
    # probe so chain validation and the pinned fingerprint stay consistent.
    # chain/none -> no pin; a refusal is fatal (fail closed).
    _server_parts = urlsplit(effective_server_url)
    server_host = _server_parts.hostname or ""
    server_port = _server_parts.port or (443 if _server_parts.scheme == "https" else 80)
    try:
        server_pin = _evaluate_server_trust(config, log, server_host, server_port)
    except ServerTrustError:
        flush_logs()
        sys.exit(1)
    if server_pin:
        CONFIG["SERVER_CERT_FINGERPRINT"] = server_pin

    # Server Connectivity Check with retry. In one-shot mode an
    # unreachable server is a failed run: exit non-zero instead of
    # returning silently with a success exit code. In continuous mode
    # (the default) the client stays alive and the reconnect loop below
    # retries the connection forever, mirroring P2P mode.
    if not _check_server_connectivity(
        server_url=effective_server_url,
        timeout=min(config["HTTP_TIMEOUT"], 10),
        verify_ssl=not config["NO_VERIFY_SSL"] and not server_pin,
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
        if not run(_run_stdin_wait(log)):
            # Nothing applied and no way to command this process any more: the
            # live loop is not gated on DRYRUN, so starting it here would make a
            # "preview only" run write files. Exit instead, exactly like the p2p
            # dry-run park.
            log.info("Dry-run finished without applying a plan; exiting.")
            return
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


def run_sync(options: RunOptions) -> None:
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

    log = _init_runtime(options)

    effective_server_url, ws_url, using_p2p_placeholder = _resolve_endpoints(options, log)

    cid = _resolve_identity(options, log)

    resolved = _resolve_secrets(options, log, effective_server_url)

    tuning = _resolve_autotune(options, log, effective_server_url, using_p2p_placeholder)

    config = _build_config(options, resolved, cid, effective_server_url, ws_url, tuning)

    set_config(config)
    set_debug(options.logging.debug)

    instance_lock = _finish_startup(options, config, tuning, resolved, log)
    try:
        # P2P Mode: Peer Rendezvous and Master Election
        if config["P2P_MODE"]:
            from hsync_client.p2p_handler import run_p2p_mode

            _warn_if_p2p_in_container(log)
            sys.exit(run_p2p_mode(config, log))

        _run_remote_mode(options, config, log, effective_server_url)
    except RuntimeError as exc:
        # Same interrupted-shutdown tail as in p2p mode: a stop signal during the
        # loop teardown is a clean exit, not a failure. Leaving it unhandled would
        # print a traceback and give the desktop a crash instead of a stop.
        if not is_event_loop_teardown_error(exc):
            raise
        log.info("Shutting down...")
    finally:
        # The async sinks must be drained before the process leaves: this
        # finally runs for every exit inside the run (including the sys.exit
        # calls that map the cycle verdict to an exit code), so the last
        # diagnostic line is never lost to the writer-thread race.
        flush_logs()
        # Compact state DB once at process exit (best-effort): the state
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

    The refusal is also published on the structured error channel
    (``reason="startup"``): hsync-desktop drives the engine through events, and
    a refusal it only sees as exit code 1 is indistinguishable from a clean
    stop — the operator would find the reason in the log only.
    """
    from hsync_client.identity import InstanceLock, InstanceLockError

    try:
        return InstanceLock.acquire(local_path)
    except InstanceLockError as e:
        message = str(e)
        log.error(message)
        # The message IS the refusal: with asynchronous sinks it must be
        # flushed before the process terminates or the user sees a bare
        # non-zero exit with no explanation.
        flush_logs()
        emit_event(UiEventName.error, message=message, reason="startup")
        sys.exit(int(ExitCode.FATAL))


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
