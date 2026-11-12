# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Resolve the runtime inputs for ``run_sync`` (endpoints, identity, secrets, tuning).

Pure-ish resolvers that turn CLI options + env settings into the ``_ResolvedSecrets``
/ ``_ResolvedTuning`` dataclasses and the endpoint/port/identity values; the
orchestration that calls them stays in ``hsync_client.main_logic``.
"""

from __future__ import annotations

from typing import Any

from hsync_common.endpoint import server_fallback_url
from hsync_common.protocol.rdv_tokens import (
    WeakGroupPassphrase,
    derive_group_key,
    validate_group_key,
    validate_group_passphrase,
)

from hsync_client.config import (
    get_allow_deletions,
    get_api_key,
    get_autotune_enabled,
    get_connectivity_retry_attempts,
    get_connectivity_retry_max_wait,
    get_connectivity_retry_min_wait,
    get_p2p_allow_unauthenticated,
    get_p2p_allow_unauthenticated_writes,
    get_p2p_key,
    get_p2p_passphrase,
    get_server_url,
)
from hsync_client.config_build import (
    _get_http_timeout_value,
    _get_max_concurrent_transfers_value,
    _get_reconnect_delay_value,
    _get_transfer_retry_max_wait_value,
    _get_transfer_retry_min_wait_value,
    _ResolvedSecrets,
    _ResolvedTuning,
)
from hsync_client.constants import (
    CONNECTIVITY_RETRY_ATTEMPTS_DEFAULT,
    CONNECTIVITY_RETRY_MAX_WAIT_DEFAULT,
    CONNECTIVITY_RETRY_MIN_WAIT_DEFAULT,
)
from hsync_client.events import UiEventName, emit_event
from hsync_client.logger import set_client_id
from hsync_client.run_options import RunOptions
from hsync_client.startup_checks import _fatal_startup
from hsync_client.utils import get_cid, http_to_ws

_SERVER_PORT_PROBE_TIMEOUT_S = 3.0


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


def _probe_server_health(base_url: str, *, verify_ssl: bool, timeout: float) -> bool:
    """One-shot reachability probe (no retries) used to pick between ports."""
    from requests import RequestException
    from requests import get as http_get

    try:
        response = http_get(
            f"{base_url.rstrip('/')}/api/v1/health", timeout=timeout, verify=verify_ssl
        )
        return response.status_code < 500
    except RequestException:
        return False


def _resolve_server_port(server_url: str, *, verify_ssl: bool, log) -> str:
    """Fall back to 443 when a server left on the default port does not answer.

    A bare address normalizes to ``https://host:9753`` (``SERVER_DEFAULT_PORT``),
    but a deployment often terminates TLS on 443 behind a reverse proxy. Keep the
    configured URL when it answers, otherwise try the shared 443 fallback and use
    the first that does; an explicit non-default port is never second-guessed, and
    the scheme stays HTTPS (the endpoint grammar refuses anything else).
    """
    fallback = server_fallback_url(server_url)
    if fallback is None:
        return server_url
    if _probe_server_health(
        server_url, verify_ssl=verify_ssl, timeout=_SERVER_PORT_PROBE_TIMEOUT_S
    ):
        return server_url
    if not _probe_server_health(
        fallback, verify_ssl=verify_ssl, timeout=_SERVER_PORT_PROBE_TIMEOUT_S
    ):
        return server_url
    log.info(f"Server did not answer on the default port; using {fallback} (443)")
    return fallback


def _resolve_endpoints(options: RunOptions, log) -> tuple[str, str, bool]:
    """Resolve the sync-server URL (or the P2P placeholder) and the WS URL.

    Returns ``(effective_server_url, ws_url, using_p2p_placeholder)``. P2P mode
    without a configured server runs on a placeholder until the master is
    discovered: the RTT probe and the UI server event are deferred (probing the
    placeholder would always fail with a spurious warning).
    """
    # Determine server URL (CLI argument takes precedence over env var)
    effective_server_url = options.endpoints.server_url or get_server_url()

    using_p2p_placeholder = options.p2p.p2p_mode and not effective_server_url

    if not effective_server_url and not options.p2p.p2p_mode:
        _fatal_startup(
            "Server URL is required (use 'hsync server HOSTNAME' or set HSYNC_SERVER_URL)",
            log=log,
        )

    if options.p2p.p2p_mode and not effective_server_url:
        effective_server_url = "http://localhost:9753"

    # At this point, effective_server_url is guaranteed to be set
    # (either from args, env, or P2P mode placeholder)
    assert effective_server_url is not None
    if not using_p2p_placeholder:
        effective_server_url = _resolve_server_port(
            effective_server_url, verify_ssl=options.safety.verify_ssl, log=log
        )
        emit_event(UiEventName.server, url=effective_server_url)

    # Convert HTTP URL to WebSocket URL for events endpoint
    ws_url = http_to_ws(effective_server_url)
    return effective_server_url, ws_url, using_p2p_placeholder


def _resolve_identity(options: RunOptions, log) -> str:
    """Resolve and publish the client identity (CID) for this session."""
    # Client identity. P2P mode presents the persistent device identity
    # (Syncthing-style: one device ID per installation, anchored in
    # $HSYNC_DEVICE_DIR) so TOFU pins survive restarts and a changed
    # certificate for the same device ID is detectable. Remote mode keeps a
    # per-session random CID: it only feeds echo suppression — the API key
    # is the identity there.
    if options.p2p.p2p_mode:
        from hsync_client.identity import load_or_create_device_identity

        cid = load_or_create_device_identity().device_id
        set_client_id(cid)
        log.info(f"Device ID: {cid}")
    else:
        cid = get_cid()
        set_client_id(cid)
        log.info(f"Session CID: {cid}")
    emit_event(UiEventName.session, cid=cid)
    return cid


def _resolve_secrets(options: RunOptions, log, effective_server_url: str) -> _ResolvedSecrets:
    """Resolve API/P2P keys, passphrase derivation and the security gates."""
    # Resolve API key once: FD-pipe value takes precedence over env var
    api_key_from_fd = _read_secret_from_fd(options.secrets.api_key_fd)
    p2p_key_from_fd = _read_secret_from_fd(options.secrets.p2p_key_fd)
    p2p_passphrase_from_fd = _read_secret_from_fd(options.secrets.p2p_passphrase_fd)
    resolved_api_key = api_key_from_fd or get_api_key()
    resolved_p2p_key = p2p_key_from_fd or get_p2p_key() or None
    resolved_p2p_passphrase = p2p_passphrase_from_fd or get_p2p_passphrase() or None
    # Two input forms for the same secret: a raw key (>=32 chars, used as-is) or a
    # user-typed password (>=12 chars, derived to a 64-hex key). Never both.
    if resolved_p2p_key and resolved_p2p_passphrase:
        _fatal_startup("Set either HSYNC_P2P_KEY or HSYNC_P2P_PASSPHRASE, not both.", log=log)
    if resolved_p2p_passphrase:
        try:
            validate_group_passphrase(resolved_p2p_passphrase)
        except WeakGroupPassphrase as exc:
            _fatal_startup(f"HSYNC_P2P_PASSPHRASE is not acceptable: {exc}", log=log)
        resolved_p2p_key = derive_group_key(resolved_p2p_passphrase)
        # The shared password, not the derived key, is what a group shares: a
        # group whose devices mix the two input forms ends up with different
        # keys and only fails later with an authentication error.
        log.info(
            "P2P shared password accepted; group key derived "
            f"({len(resolved_p2p_key)} hex chars). "
            "Use the same password on every device."
        )
    # Fail fast on a weak shared key: it is the only entropy behind a bucket
    # token, so a short HSYNC_P2P_KEY would make a protected bucket guessable.
    # Reported here (like the missing-API-key gate below) instead of mid-sync.
    if resolved_p2p_key:
        try:
            validate_group_key(resolved_p2p_key)
        except ValueError as exc:
            _fatal_startup(f"HSYNC_P2P_KEY is not acceptable: {exc}", log=log)
    allow_unauthenticated_p2p_writes = (
        options.p2p.p2p_allow_unauthenticated_writes or get_p2p_allow_unauthenticated_writes()
    )
    allow_unauthenticated_p2p = (
        options.p2p.p2p_allow_unauthenticated or get_p2p_allow_unauthenticated()
    )
    allow_deletions = options.safety.allow_deletions or get_allow_deletions()

    # Fail fast on a missing API key: every authenticated server endpoint
    # requires a valid key, so an empty key can never sync. Reporting it here
    # avoids a late, opaque "Invalid API key" failure mid-sync. P2P mode has
    # its own shared-key policy and is exempt.
    if not options.p2p.p2p_mode and not resolved_api_key:
        _fatal_startup(
            "No API key configured. Set HSYNC_API_KEY (or pass it via "
            "--api-key-fd) to authenticate with the server.",
            log=log,
        )

    # Security gate: never transmit the API key over an unencrypted channel.
    # The rule keys on the URL scheme the client connects to, so an HTTPS
    # reverse proxy in front of an HTTP backend is fully supported (the
    # client uses an https:// URL; the backend stays HTTP behind the proxy).
    if not options.p2p.p2p_mode and not effective_server_url.lower().startswith("https://"):
        _fatal_startup(
            f"API key provided but server URL is not HTTPS ('{effective_server_url}'). "
            "Refusing to send credentials over an unencrypted connection. "
            "Use an https:// URL (a reverse proxy terminating TLS is supported).",
            log=log,
        )
    return _ResolvedSecrets(
        api_key=resolved_api_key,
        p2p_key=resolved_p2p_key,
        allow_deletions=allow_deletions,
        allow_unauthenticated_p2p=allow_unauthenticated_p2p,
        allow_unauthenticated_p2p_writes=allow_unauthenticated_p2p_writes,
    )


def _resolve_autotune(
    options: RunOptions, log, effective_server_url: str, using_p2p_placeholder: bool
) -> _ResolvedTuning:
    """Probe the network and resolve autotune-managed tuning values."""
    verify_ssl = options.safety.verify_ssl
    http_timeout = options.transfers.http_timeout
    local_path = options.sync.local_path
    continuous = options.sync.continuous
    reconnect_delay = options.transfers.reconnect_delay
    dryrun = options.sync.dryrun
    max_concurrent_transfers = options.transfers.max_concurrent_transfers
    no_autotune = options.transfers.no_autotune

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
            # to the post-rendezvous refresh in p2p_handler (slave role only).
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
    return _ResolvedTuning(
        autotune_enabled=autotune_enabled,
        profile=tuning,
        tuning_keys=_tuning_keys,
        resolved_http_timeout=_resolved_http_timeout,
        resolved_reconnect_delay=_resolved_reconnect_delay,
        resolved_retry_min=_resolved_retry_min,
        resolved_retry_max=_resolved_retry_max,
        resolved_max_concurrent=_resolved_max_concurrent,
        conn_retry_attempts=_conn_retry_attempts,
        conn_retry_min=_conn_retry_min,
        conn_retry_max=_conn_retry_max,
    )
