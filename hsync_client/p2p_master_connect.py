# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Master endpoint selection, pin/trust evaluation and relay wiring for the slave flow.

The slave connection policy (probe/rank endpoints, evaluate the master pin, start
a relay tunnel) extracted from ``hsync_client.p2p_handler``; the P2P orchestration
that drives it stays there.
"""

import asyncio
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from hsync_common.endpoint import format_authority
from hsync_common.hashing import is_sha256_hex
from hsync_common.protocol.pairing import (
    PinDecision,
    evaluate_pin,
    normalize_fingerprint,
)
from hsync_common.protocol.tls_trust import (
    DEFAULT_SELF_SIGNED_POLICY,
    TrustAction,
    evaluate_tls_trust,
)

from hsync_client.config import HsyncConfig
from hsync_client.constants import (
    MASTER_PROBE_FAST_TIMEOUT_S,
    RELAY_ACCEPT_TIMEOUT_S,
    RELAY_OPEN_TIMEOUT_S,
    RELAY_RETRY_BACKOFF_S,
    RELAY_SESSION_WAIT_S,
    ActionLabel,
)
from hsync_client.events import (
    UiEventName,
    emit_event,
    emit_warning,
    emit_warning_cleared,
)
from hsync_client.net_utils import get_local_ips
from hsync_client.p2p_peers import dial_candidates
from hsync_client.p2p_rendezvous import (
    RendezvousPlan,
)
from hsync_client.pinning import default_pin_store
from hsync_client.rdv_relay import RelayAcceptor, RelayDialer
from hsync_client.ssl_utils import create_fingerprint_ssl_context, verify_peer_cert_fingerprint
from hsync_client.trust_messages import (
    known_peer_added_message,
    peer_mismatch_guidance,
    peer_pairing_needed_guidance,
)
from hsync_client.trust_prompt import build_peer_trust_prompt

# Per-candidate reachability budget when a master advertises several data-plane
# endpoints (rdv rendezvous). An unreachable candidate must not stall the
# connection for the full request timeout before the next one is tried; a
# same-subnet candidate that fails fast falls back to the public endpoint.
MASTER_CANDIDATE_PROBE_TIMEOUT_S = 3.0

# Relay tunnel state for the slave flow: at most one dialer/acceptor pair is live.
_active_relay_acceptor: RelayAcceptor | None = None
_active_relay_dialer: RelayDialer | None = None
_relay_liveness: tuple[Any, str] | None = None
_relay_retry_at = 0.0
_relay_hint_masters: set[str] = set()


def _mark_trust_notice(
    notified: dict[tuple[str, str], str] | None,
    kind: str,
    cid: str,
    fingerprint: str,
) -> bool:
    """Dedupe a trust refusal; return True when it was already notified.

    ``notified`` maps ``(kind, cid)`` to the last fingerprint notified. The first
    time a given fingerprint is seen for a ``(kind, cid)`` pair it is recorded and
    the call returns ``False`` (the caller logs/emits); a repeat of the same
    fingerprint returns ``True`` (suppress). With ``notified`` None every call
    returns ``False`` and nothing is recorded (direct/test callers).
    """
    if notified is None:
        return False
    key = (kind, cid)
    if notified.get(key) == fingerprint:
        return True
    notified[key] = fingerprint
    return False


def _clear_trust_notice(
    notified: dict[tuple[str, str], str] | None,
    cid: str,
) -> None:
    """Forget the recorded refusals for *cid* once its connection proceeds."""
    if notified is None:
        return
    notified.pop(("pairing_needed", cid), None)
    notified.pop(("fingerprint_changed", cid), None)


def _refuse_pairing_needed(
    master,
    log,
    bucket: str,
    fingerprint: str,
    notified: dict[tuple[str, str], str] | None,
) -> bool:
    """Refuse an unconfirmed first-seen master and surface ``pairing_needed``."""
    if not _mark_trust_notice(notified, "pairing_needed", master.cid, fingerprint):
        log.error(peer_pairing_needed_guidance(master.cid, bucket, master.address, fingerprint))
        emit_event(
            UiEventName.pairing_needed,
            scope="master",
            peer_cid=master.cid,
            peer_address=master.address,
            fingerprint=fingerprint,
            message="P2P master requires explicit pairing confirmation",
        )
    return False


def _refuse_changed_fingerprint(
    master,
    log,
    store,
    bucket: str,
    fingerprint: str,
    notified: dict[tuple[str, str], str] | None,
) -> bool:
    """Refuse a master whose pinned fingerprint no longer matches."""
    if not _mark_trust_notice(notified, "fingerprint_changed", master.cid, fingerprint):
        log.error(
            peer_mismatch_guidance(master.cid, bucket, master.address, fingerprint, store.path)
        )
        emit_event(
            UiEventName.fingerprint_changed,
            scope="master",
            peer_cid=master.cid,
            peer_address=master.address,
            fingerprint=fingerprint,
            message="P2P master certificate fingerprint changed",
        )
    return False


def _evaluate_master_pin(
    config: "HsyncConfig",
    master,
    log,
    notified: dict[tuple[str, str], str] | None = None,
) -> bool:
    """Apply the P2P master trust policy; return whether the connection may proceed.

    The decision is the shared one in :mod:`hsync_common.protocol.tls_trust`
    (:func:`evaluate_tls_trust`); here we supply its confirmation channel, persist
    the pin and surface structured security events so a front-end without a
    terminal (desktop, Android) can prompt instead.

    Three outcomes refuse the connection (return ``False``):

    * **First sighting under ``ask``** — an SSH-style prompt is rendered when a
      terminal is available. Without one the client fails closed and emits
      ``pairing_needed``; the operator pins the peer out of band.
    * **First sighting under ``strict``** — refused without a prompt until the
      fingerprint is pinned out of band.
    * **Fingerprint changed** — the certificate no longer matches the pin. Every
      policy refuses it (possible man-in-the-middle).

    ``notified`` (see :func:`_mark_trust_notice`) dedupes the refusal blocks and
    their events: the rendezvous loop retries continuously, so without it the
    guidance would repeat every iteration. The entry is cleared once the
    connection proceeds (:func:`_clear_trust_notice`), so a later flip re-notifies.
    """
    from hsync_client.pinning import PinStoreError

    store = default_pin_store()
    bucket = config["REMOTE_BUCKET"]
    raw_fingerprint = master.cert_fingerprint
    if not isinstance(raw_fingerprint, str):
        log.error(
            f"{ActionLabel.SECURITY} P2P master {master.cid} advertised an invalid "
            "certificate fingerprint. Refusing to connect."
        )
        return False
    fingerprint = normalize_fingerprint(raw_fingerprint)
    if not is_sha256_hex(fingerprint):
        log.error(
            f"{ActionLabel.SECURITY} P2P master {master.cid} advertised a non-SHA-256 "
            "certificate fingerprint. Refusing to connect."
        )
        return False

    try:
        stored = store.get(master.cid, bucket)
        # ``run_sync`` always sets the key; a hand-built config without it falls
        # back to the SSOT default (fail closed on an unknown master), never to
        # silent auto-trust.
        policy = config.get("P2P_TRUST", DEFAULT_SELF_SIGNED_POLICY)
        action = evaluate_tls_trust(policy, stored, fingerprint)
        if action is TrustAction.prompt:
            prompt = build_peer_trust_prompt(config)
            if prompt is not None and prompt(master.cid, master.address, fingerprint):
                action = TrustAction.pin_and_proceed
        if action is TrustAction.refuse:
            if evaluate_pin(stored, fingerprint) is PinDecision.changed:
                return _refuse_changed_fingerprint(
                    master, log, store, bucket, fingerprint, notified
                )
            return _refuse_pairing_needed(master, log, bucket, fingerprint, notified)

        decision = store.check_and_record(master.cid, bucket, fingerprint)
    except PinStoreError as exc:
        log.error(
            f"{ActionLabel.SECURITY} Cannot establish durable trust for P2P master "
            f"{master.cid}: {exc}. Refusing to connect."
        )
        return False
    if decision is PinDecision.changed:
        # A concurrent run repinned the peer between the policy check and the
        # locked read-modify-write: honour the pin that won.
        return _refuse_changed_fingerprint(master, log, store, bucket, fingerprint, notified)
    _clear_trust_notice(notified, master.cid)
    if decision is PinDecision.trusted:
        log.info(known_peer_added_message(master.cid, bucket, store.path))
    return True


def _probe_master_endpoint(host: str, port: int, fingerprint: str, timeout: float) -> bool:
    """Whether *host:port* completes a fingerprint-pinned TLS handshake.

    The check is the handshake itself: any completed handshake whose peer
    certificate matches the pin proves the endpoint is dialable, independent of
    the HTTP status a keyed master would answer an unauthenticated request with
    (``401``). The certificate is verified on the live connection, never on a
    separate probe, so there is no TOCTOU window.
    """
    try:
        context = create_fingerprint_ssl_context(fingerprint)
        with socket.create_connection((host, port), timeout=timeout) as connection:
            with context.wrap_socket(connection, server_hostname=None) as tls:
                return verify_peer_cert_fingerprint(tls, fingerprint)
    except (OSError, ValueError):
        return False


def _probe_round(
    candidates: tuple[tuple[str, int], ...], fingerprint: str, timeout: float
) -> tuple[str, int] | None:
    """Probe every candidate at once and return the highest-priority reachable one.

    The candidate order is the *preference* (host/LAN candidates first, so two
    peers behind the same NAT use the local path), and the probes run in
    parallel: probing them one at a time made a peer that advertises several
    undialable endpoints — the carrier-grade-NAT case — wait one timeout per
    candidate before the relay was even considered. Every probe of a round still
    finishes before the round returns, so the round costs one timeout, not the
    sum of them.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        host, port = candidates[0]
        return candidates[0] if _probe_master_endpoint(host, port, fingerprint, timeout) else None
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        futures = [
            pool.submit(_probe_master_endpoint, host, port, fingerprint, timeout)
            for host, port in candidates
        ]
        reachable = [bool(future.result()) for future in futures]
    for index, ok in enumerate(reachable):
        if ok:
            return candidates[index]
    return None


def _select_master_endpoint(master, log) -> tuple[str, int] | None:
    """Pick the first reachable master candidate, host/LAN candidates first.

    A peer that advertised several candidates is probed in host/LAN-first order
    and the first candidate whose pinned handshake succeeds wins. This is what
    lets two peers behind the same NAT use the local path while a remote peer
    still falls back to its public endpoint. A **rendezvous** peer that
    advertised a single candidate is probed the same way: returning it unverified
    would strand a slave on an advertising-only private IP — the data-plane
    connect fails, and because an endpoint was returned the caller never falls
    back to the rendezvous data relay, so two peers behind carrier-grade NAT
    would loop on ``Connecting…`` forever even with ``--relay`` on. An **mDNS**
    peer (no advertised candidate list) keeps its single address without a probe:
    its data-plane connection is its own reachability check, exactly as before.
    ``None`` means no candidate is reachable now and the caller may try the relay.

    The probes run in two rounds: a short one first, which answers for every peer
    that is dialable at all (a LAN connect completes in milliseconds), and the
    full budget only for the candidates that stayed silent. A reachable candidate
    therefore costs milliseconds instead of a timeout, while a slow-but-valid
    public endpoint still gets its chance.
    """
    advertised = tuple(getattr(master, "endpoints", ()) or ())
    candidates = dial_candidates(
        advertised,
        master.address,
        master.port,
        get_local_ips(),
    )
    if not advertised and len(candidates) == 1:
        # mDNS: the peer announced one address directly; connect-time is the
        # check (no rendezvous relay exists for a LAN-only plan anyway).
        return candidates[0]
    for timeout in (MASTER_PROBE_FAST_TIMEOUT_S, MASTER_CANDIDATE_PROBE_TIMEOUT_S):
        selected = _probe_round(candidates, master.cert_fingerprint, timeout)
        if selected is not None:
            if selected != (master.address, master.port):
                log.info(
                    f"Selected master endpoint {format_authority(*selected)} of {len(candidates)}"
                )
            return selected
    return None


def _start_master_relay(p2p_sync: RendezvousPlan, log) -> None:
    """Ask the rendezvous to relay inbound sessions to this master (once per stint).

    Idempotent across the rendezvous loop's one-second re-entries: the acceptor is
    kept while it is alive, so a stable master holds exactly one listener control
    stream. A node whose rendezvous has no relay enabled simply keeps serving
    direct peers (debug line, no warning: it is the node operator's choice), and
    the attempt is not repeated before ``RELAY_RETRY_BACKOFF_S``.
    """
    global _active_relay_acceptor, _relay_retry_at
    if _active_relay_acceptor is not None and _active_relay_acceptor.active:
        return
    now = time.monotonic()
    if now < _relay_retry_at:
        return
    acceptor = p2p_sync.relay_acceptor(p2p_sync.port)
    if acceptor is None:
        return
    if acceptor.start(timeout=RELAY_ACCEPT_TIMEOUT_S):
        _active_relay_acceptor = acceptor
        log.info(
            f"{ActionLabel.RELAY} listening on the rendezvous — peers without a "
            "reachable inbound port can sync through it"
        )
        return
    acceptor.stop()
    _relay_retry_at = now + RELAY_RETRY_BACKOFF_S
    log.debug("Rendezvous data relay unavailable; serving directly reachable peers only")


def _stop_master_relay() -> None:
    """Withdraw the master's relay listener (role retirement / shutdown)."""
    global _active_relay_acceptor
    acceptor, _active_relay_acceptor = _active_relay_acceptor, None
    if acceptor is not None:
        acceptor.stop()


def _close_relay_dialer() -> None:
    """Drop the requester-side tunnel (idempotent)."""
    global _active_relay_dialer, _relay_liveness
    dialer, _active_relay_dialer = _active_relay_dialer, None
    if dialer is not None:
        dialer.close()
    liveness, _relay_liveness = _relay_liveness, None
    if liveness is not None:
        peer_manager, cid = liveness
        peer_manager.mark_relay_tunnel(cid, False)


def _relay_master_endpoint(
    plan: RendezvousPlan | None, master: Any, log, peer_manager
) -> tuple[str, int] | None:
    """Open a relayed tunnel to the master; return the loopback endpoint.

    This is the last resort after every advertised candidate failed: two peers
    behind carrier-grade NAT have no dialable address at all, so the only
    remaining path is through the rendezvous that already introduced them. ``None``
    keeps the caller's normal "no reachable endpoint, retry rendezvous" behaviour.
    An adopted tunnel also marks *master* as reachable through it: the node pairs
    a session only for a target that has a listener, so the open tunnel holds the
    peer's liveness whatever the discovery samples say.
    """
    global _active_relay_dialer, _relay_liveness, _relay_retry_at
    if plan is None:
        _close_relay_dialer()
        return None
    now = time.monotonic()
    if now < _relay_retry_at:
        return None
    # A tunnel that is still open to the same master stays: closing it would
    # invalidate every connection the HTTP client and the WebSocket already hold
    # on its loopback endpoint — and any request in flight — which the peer sees
    # as an SSL EOF. The tunnel keeps that port while it repairs its own session,
    # so the caller waits for a session instead of replacing it; only a tunnel
    # that is gone, or one that targets another master, is recreated.
    dialer = _active_relay_dialer
    if dialer is not None and dialer.alive and dialer.target_cid == master.cid:
        if dialer.session_up:
            return dialer.endpoint
        if dialer.repairing:
            # The session is coming back: wait for it instead of replacing the
            # tunnel (a new tunnel would bind a new loopback port and strand the
            # connections the HTTP client and the WebSocket already hold).
            endpoint = dialer.wait_session(RELAY_SESSION_WAIT_S)
            if endpoint is not None:
                return endpoint
        # The node refused the last attempt (the master stopped listening) or the
        # wait expired: no endpoint for this cycle, and no retry backoff — the
        # tunnel's own repair loop paces its attempts.
        _warn_relay_unavailable(master, dialer, log)
        return None
    _close_relay_dialer()
    dialer = plan.relay_dialer(master.cid)
    if dialer is None:
        return None
    endpoint = dialer.open(RELAY_OPEN_TIMEOUT_S)
    if endpoint is None:
        _relay_retry_at = now + RELAY_RETRY_BACKOFF_S
        _warn_relay_unavailable(master, dialer, log)
        return None
    _active_relay_dialer = dialer
    _relay_liveness = (peer_manager, master.cid)
    peer_manager.mark_relay_tunnel(master.cid, True)
    _relay_hint_masters.discard(master.cid)
    # The relay pairs now: the "no data relay" problem it raised no longer holds.
    emit_warning_cleared("relay")
    log.info(
        f"{ActionLabel.RELAY} master {master.cid} has no reachable inbound port; "
        "relaying the data plane through the rendezvous"
    )
    return endpoint


def _warn_relay_unavailable(master: Any, dialer: Any, log) -> None:
    """Emit one actionable hint when the node could not pair the data relay.

    Two peers behind carrier-grade NAT have no dialable address, so the relay
    through the rendezvous is their only path. When the node answers every relay
    frame with ``relay_unavailable`` (the common case: it was started without
    ``--relay``), the user only sees the generic "No reachable endpoint" warning
    and cannot act on it. This logs and publishes — through the structured
    warning channel the desktop surfaces as a banner — one actionable message per
    master, naming the node-side fix. Emitted at most once per master (the
    rendezvous loop retries underneath) and never for a peer that is expected to
    be reachable by other means: the call only fires after every direct candidate
    failed and the relay was refused.
    """
    if getattr(dialer, "refusal", None) != "relay_unavailable":
        return
    if master.cid in _relay_hint_masters:
        return
    _relay_hint_masters.add(master.cid)
    message = (
        f"{ActionLabel.RELAY} master {master.cid} is unreachable directly and the "
        "rendezvous node could not relay the data plane. If both devices are "
        "behind carrier-grade NAT (mobile data, hotspot, CGNAT), start the node "
        "with 'hsync-rendezvous run --relay'."
    )
    log.warning(message)
    emit_warning(message, source="relay", reason="relay_unavailable")


class _MasterEndpointGuard:
    """Detect that the master's advertised endpoint changed mid-session.

    The slave pins the master's data-plane URL for the whole session
    (``config["API"]``), so a master that moves networks would keep being dialed
    at its stale address until the session is abandoned. The guard watches the
    shared :class:`PeerManager` and reports the change, so the session re-enters
    the rendezvous loop and re-selects the master endpoint (directly, or through
    the relay). The watched signature is the master's dial-relevant fields, so a
    heartbeat that only refreshes ``last_seen`` never fires it.
    """

    def __init__(
        self,
        peer_manager: Any,
        master_cid: str,
        *,
        address: str,
        port: int,
        endpoints: Any,
    ) -> None:
        self._peer_manager = peer_manager
        self._master_cid = master_cid
        self._signature = self._signature_of(address, port, endpoints)
        self._event = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _signature_of(address: str, port: int, endpoints: Any) -> tuple[Any, ...]:
        return (address, port, tuple(endpoints or ()))

    def _current_signature(self) -> tuple[Any, ...] | None:
        master = self._peer_manager.get_master()
        if master is None or master.cid != self._master_cid:
            return None
        return self._signature_of(master.address, master.port, master.endpoints)

    def start(self) -> None:
        """Start the watcher thread (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._watch, name="hsync-master-endpoint", daemon=True
        )
        self._thread.start()

    def _watch(self) -> None:
        # A one-second poll keeps the reaction prompt without sharing the
        # rendezvous loop's change-generation (a second ``wait_for_change``
        # waiter would consume its wakeups).
        while not self._stop.wait(1.0):
            if self._current_signature() != self._signature:
                self._event.set()
                return

    async def wait_async(self) -> None:
        """Await the endpoint change without blocking the event loop.

        Polling (not a worker thread) keeps it cancellable: the connection
        teardown cancels this task, and a thread blocked on the event would keep
        the loop's executor shutdown waiting.
        """
        while not self._event.is_set():
            await asyncio.sleep(0.2)

    def close(self) -> None:
        """Stop the watcher and release any waiter (idempotent)."""
        self._stop.set()
        self._event.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
