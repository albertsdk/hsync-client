# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Server (and rendezvous) TLS trust resolution for the remote/p2p startup.

Pins/TOFU evaluation, terminal prompt and refusal logging evaluated before a
connection is opened. Pure orchestration over the shared trust stores, kept apart
from ``hsync_client.main_logic`` runtime wiring.
"""

from __future__ import annotations

from pathlib import Path

from hsync_common.endpoint import format_authority
from hsync_common.protocol.pairing import FingerprintPins, fingerprint_matches
from hsync_common.protocol.tls_trust import (
    TrustAction,
    TrustPolicy,
    evaluate_tls_trust,
    resolve_trust_policy,
)

from hsync_client.config import (
    HsyncConfig,
    get_rendezvous_server_trust,
    get_server_trust,
)
from hsync_client.events import UiEventName, emit_event
from hsync_client.server_trust import (
    default_server_trust_store,
    fetch_server_fingerprint,
    resolve_server_trust,
)
from hsync_client.trust_messages import (
    known_server_added_message,
    server_mismatch_guidance,
    server_pairing_needed_guidance,
)
from hsync_client.trust_prompt import build_trust_prompt, server_terminal_trust_prompt


def _flatten_pins(pins: FingerprintPins) -> tuple[str, ...]:
    """Unique pinned digests across endpoints (the trust policy needs presence)."""
    return tuple(dict.fromkeys(value for values in pins.values() for value in values))


def _resolve_rendezvous_server_trust(
    policy: str | None,
    fingerprints: tuple[str, ...],
    tofu: bool,
    verify_ssl: bool,
) -> TrustPolicy:
    """Resolve the rendezvous-server trust policy from flags/env (ADR-0035).

    Precedence: ``--insecure`` (``none``) > ``--rendezvous-server-fingerprint``
    (``strict``) > ``--rendezvous-server-tofu`` (``accept-new``) > the flag value >
    ``HSYNC_RENDEZVOUS_SERVER_TRUST`` > the ``ask`` default.

    The precedence itself is the shared SSOT
    (:func:`hsync_common.protocol.tls_trust.resolve_trust_policy`); only the
    ``ask`` fallback and the rendezvous-server env source are transport-specific.
    """
    return resolve_trust_policy(
        policy,
        fingerprints,
        tofu,
        verify_ssl,
        env_default=get_rendezvous_server_trust(),
        fallback=TrustPolicy.ask,
    )


class ServerTrustError(RuntimeError):
    """The sync-server certificate could not be trusted (fail closed, ADR-0036)."""


def _resolve_server_trust(
    policy: str | None,
    fingerprints: tuple[str, ...],
    tofu: bool,
    verify_ssl: bool,
) -> TrustPolicy:
    """Resolve the server trust policy from flags/env (ADR-0036).

    Precedence: ``--insecure`` (``none``) > ``--server-fingerprint`` (``strict``) >
    ``--server-tofu`` (``accept-new``) > the flag value > ``HSYNC_SERVER_TRUST`` >
    the ``chain`` default.
    """
    return resolve_server_trust(
        policy, fingerprints, tofu, verify_ssl, env_default=get_server_trust()
    )


def _log_and_emit_server_refusal(
    log,
    host: str,
    port: int,
    observed: str | None,
    *,
    stored: str | None,
    explicit: bool,
    store_path: Path,
) -> None:
    """Log the SSH-style guidance and emit the structured server trust event.

    An unpinned host (or an explicit-pin policy with no match) is a
    ``pairing_needed`` prompt; a changed pin is ``fingerprint_changed``. The
    event carries ``peer_address=host:port`` so a GUI can route it without parsing.
    """
    observed = observed or ""
    if stored is None and not explicit:
        log.error(server_pairing_needed_guidance(host, port, observed))
        emit_event(
            UiEventName.pairing_needed,
            scope="server",
            peer_address=format_authority(host, port),
            fingerprint=observed,
            message="Sync server requires explicit trust confirmation",
        )
    else:
        log.error(
            server_mismatch_guidance(
                host,
                port,
                observed,
                stored=stored,
                store_path=store_path,
                explicit_pins=explicit,
            )
        )
        emit_event(
            UiEventName.fingerprint_changed,
            scope="server",
            peer_address=format_authority(host, port),
            fingerprint=observed,
            message="Sync server certificate fingerprint changed",
        )


def _evaluate_server_trust(config: HsyncConfig, log, host: str, port: int) -> str | None:
    """Resolve the pinned server fingerprint for this run.

    Returns the fingerprint to pin, or ``None`` when the policy is ``chain``/
    ``none`` (standard verification, no pinning). Fail-closed on every refusal
    (changed/mismatched fingerprint, unpinned under ``strict``, ``ask`` without a
    terminal): it logs the SSH-style guidance, emits the structured trust event
    and raises :class:`ServerTrustError` so the caller stops the run.
    """
    policy = config["SERVER_TRUST"]
    if policy is TrustPolicy.chain or policy is TrustPolicy.none:
        return None

    observed = fetch_server_fingerprint(host, port, float(config.get("HTTP_TIMEOUT", 10)))
    store = default_server_trust_store()
    stored = store.get(host, port)
    explicit = tuple(config.get("SERVER_FINGERPRINTS") or ())

    if explicit:
        if observed is not None and fingerprint_matches(observed, explicit):
            store.set(host, port, observed, source="flag")
            log.info(known_server_added_message(host, port, store.path))
            return observed
        _log_and_emit_server_refusal(
            log, host, port, observed, stored=stored, explicit=True, store_path=store.path
        )
        raise ServerTrustError("server fingerprint does not match --server-fingerprint")

    action = evaluate_tls_trust(policy, stored, observed)
    if action is TrustAction.proceed:
        return stored
    if action is TrustAction.pin_and_proceed:
        if observed is None:  # pragma: no cover - evaluate_tls_trust guarantees it
            raise ServerTrustError("server presented no certificate")
        store.set(host, port, observed, source="tofu")
        log.info(known_server_added_message(host, port, store.path))
        return observed
    if action is TrustAction.prompt:
        prompt = build_trust_prompt(
            config, policy_key="SERVER_TRUST", prompt=server_terminal_trust_prompt
        )
        if prompt is None:
            _log_and_emit_server_refusal(
                log, host, port, observed, stored=None, explicit=False, store_path=store.path
            )
            raise ServerTrustError("no terminal to confirm the server certificate")
        if observed is not None and prompt(host, port, observed):
            store.set(host, port, observed, source="prompt")
            log.info(known_server_added_message(host, port, store.path))
            return observed
        log.error("Server certificate not accepted; refusing to connect.")
        raise ServerTrustError("server certificate rejected by the operator")

    _log_and_emit_server_refusal(
        log, host, port, observed, stored=stored, explicit=False, store_path=store.path
    )
    raise ServerTrustError("server certificate could not be trusted")
