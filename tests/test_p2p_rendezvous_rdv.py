# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Unit tests for the rdv rendezvous plan (no network).

A fake client records the frames the plan sends and returns canned ``ack``/
``response`` frames, so the lease semantics (first register without endpoint,
second register with endpoint + capacity, refresh TTL-only, peer feed filtering,
leave/unmap on stop) are verified deterministically.
"""

from __future__ import annotations

import threading
import time

import pytest
from hsync_common.protocol.rendezvous import (
    validate_rdv_frame,
    validate_rdv_response,
)

from hsync_client.constants import (
    RDV_FAILURE_BACKOFF_BASE_S,
    RDV_FAILURE_BACKOFF_MAX_S,
    RDV_FAILURE_WARN_AFTER,
    RENDEZVOUS_QUERY_ACTIVE_INTERVAL,
    RENDEZVOUS_QUERY_BURST_COUNT,
    RENDEZVOUS_QUERY_BURST_INTERVAL,
    RENDEZVOUS_UNPUBLISHED_SAMPLE_RETRIES,
)
from hsync_client.p2p_peers import Peer, PeerManager
from hsync_client.p2p_rendezvous_rdv import (
    _TEST_ADVERTISED_ENDPOINTS_ENV,
    _TEST_ADVERTISED_ENDPOINTS_FILE_ENV,
    RdvRendezvous,
)
from hsync_client.rdv_client import (
    RendezvousFingerprintMismatch,
    RendezvousNotTrusted,
    RendezvousRefused,
    RendezvousUnreachable,
)
from hsync_client.stun import ObservedAddress

CID = "A1b2C3d4E5f6G7h8I9j0K"
OTHER = "B2c3D4e5F6g7H8i9J0k1L"
FP = "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"
ACK = {
    "protocol_version": "rdv/1",
    "type": "ack",
    "observed_endpoint": {"proto": "udp", "ip": "203.0.113.9", "port": 51000},
    "ttl": 60,
    "server_time": "2026-01-01T00:00:00Z",
}


def relayed_peer(
    cid, *, ip, port, proto="tcp", capacity=0, reachable=True, endpoints=None, **advert
):
    """A relayed ``peers[]`` entry: every field the rendezvous always echoes.

    The election advertisement keys are required on the wire (the node always
    relays them); ``advert`` overrides the defaults for a test. ``endpoints``
    replaces the default single-element candidate list.
    """
    if endpoints is None:
        endpoints = [{"proto": proto, "host": ip, "port": port}]
    peer = {
        "cid": cid,
        "endpoints": endpoints,
        "cert_fp": FP,
        "bucket": "shared",
        "group": "home",
        "version": "1.0.0",
        "capacity": capacity,
        "reachable": reachable,
        "server_ready": False,
        "prefer_master": False,
        "never_master": False,
        "inbound": False,
        "startup_time": 0.0,
        "sync_direction": "bidirectional",
    }
    peer.update(advert)
    return peer


RESPONSE = {
    "protocol_version": "rdv/1",
    "type": "response",
    "peers": [
        # A serving, prefer-master peer: every election input is relayed.
        relayed_peer(
            OTHER,
            ip="203.0.113.10",
            port=8443,
            capacity=4,
            server_ready=True,
            prefer_master=True,
            inbound=True,
            startup_time=1760000000.5,
            sync_direction="download-only",
        ),
        # Not dialable over the p2p data plane (UDP): skipped.
        relayed_peer("C3d4E5f6G7h8I9j0K1l2M", ip="203.0.113.11", port=60000, proto="udp"),
        # No published endpoint yet (rendezvous-only register): skipped.
        relayed_peer("D4e5F6g7H8i9J0k1L2m3N", ip="203.0.113.12", port=8443, reachable=False),
    ],
    "count": 3,
    "ttl": 60,
}


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.calls: list[tuple[str, dict]] = []

    def register(self, frame):
        self.calls.append(("register", frame))
        return dict(ACK)

    def refresh(self, frame):
        self.calls.append(("refresh", frame))
        return dict(ACK)

    def leave(self, frame):
        self.calls.append(("leave", frame))
        return None

    def query(self, frame):
        self.calls.append(("query", frame))
        return validate_rdv_response(dict(RESPONSE))


class FakeUpnp:
    def __init__(self):
        self.mapped: list[int] = []
        self.unmapped = False

    def map_port(self, port):
        self.mapped.append(port)
        return port

    def unmap(self):
        self.unmapped = True


def make_plan(**overrides):
    fake_upnp = FakeUpnp()
    kwargs = {
        "cid": CID,
        "bucket": "shared",
        "rendezvous_host": "rdv.example.net",
        "rendezvous_port": 443,
        "port": 8443,
        "group": "home",
        "cert_fingerprint": FP,
        "p2p_key": "0123456789abcdef0123456789abcdef",
        "peer_download_enabled": True,
        "capacity": 7,
        "client_factory": FakeClient,
        "upnp_factory": lambda: fake_upnp,
    }
    kwargs.update(overrides)
    plan = RdvRendezvous(**kwargs)
    return plan, fake_upnp


class TestLease:
    def test_first_register_has_no_endpoint_and_zero_capacity(self):
        plan, _ = make_plan()
        plan._register(publish_endpoint=False)
        kind, frame = plan._client.calls[-1]
        assert kind == "register"
        assert frame["endpoints"] == []
        assert frame["capacity"] == 0
        assert validate_rdv_frame(frame) is not None
        assert plan._observed == ObservedAddress(host="203.0.113.9", port=51000)

    def test_withdrawn_master_candidacy_is_advertised(self):
        plan, _ = make_plan(prefer_master=True)

        plan.set_master_eligibility(False)
        plan._register(publish_endpoint=False)
        _kind, frame = plan._client.calls[-1]

        assert frame["prefer_master"] is False
        assert frame["never_master"] is True

    def test_publish_endpoint_maps_upnp_and_sends_candidates(self):
        plan, fake_upnp = make_plan(upnp_enabled=True)
        plan.update_endpoint_ready(True)
        plan._publish_endpoint()
        assert fake_upnp.mapped == [8443]
        kind, frame = plan._client.calls[-1]
        assert kind == "register"
        assert frame["capacity"] == 7
        assert frame["endpoints"], "the second register must publish endpoints"
        assert frame["inbound"] is True
        assert plan._inbound is True
        assert validate_rdv_frame(frame) is not None

    def test_publish_endpoint_without_mapping_leaves_inbound_false(self):
        """No confirmed external port means relay-only: do not claim inbound."""
        plan, _ = make_plan()  # no UPnP mapping
        plan._publish_endpoint()
        _kind, frame = plan._client.calls[-1]
        assert frame["inbound"] is False
        assert plan._inbound is False

    def test_capacity_is_zero_without_peer_download(self):
        plan, _ = make_plan(peer_download_enabled=False)
        plan._publish_endpoint()
        assert plan._client.calls[-1][1]["capacity"] == 0

    def test_refresh_carries_connected_only(self):
        plan, _ = make_plan()
        plan.update_server_ready()
        plan.set_connected_clients(3)
        plan._refresh()
        kind, frame = plan._client.calls[-1]
        assert kind == "refresh"
        assert "endpoints" not in frame
        assert frame["connected"] == 3

    def test_token_derived_from_key(self):
        plan, _ = make_plan()
        plan._register(publish_endpoint=False)
        assert plan._client.calls[-1][1]["bucket_token"] is not None

    def test_no_token_without_key(self):
        plan, _ = make_plan(p2p_key=None)
        plan._register(publish_endpoint=False)
        assert "bucket_token" not in plan._client.calls[-1][1]


class TestEndpointPublication:
    def test_endpoint_published_flag(self):
        plan, _ = make_plan()
        assert plan.endpoint_published() is False
        plan._register(publish_endpoint=False)
        assert plan.endpoint_published() is False
        plan._publish_endpoint()
        assert plan.endpoint_published() is True

    def test_mdns_plan_is_always_published(self):
        """The mDNS plan keeps today's readiness timing (no publication step)."""
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        assert MdnsRendezvous(cid=CID, bucket="shared").endpoint_published() is True


class TestPeerFeed:
    def test_only_reachable_tcp_peers_are_fed(self):
        plan, _ = make_plan()
        manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan._peer_manager = manager
        plan._query_peers()
        peers = manager.get_alive_peers()
        assert [p.cid for p in peers] == [OTHER]
        assert peers[0].address == "203.0.113.10"
        assert peers[0].port == 8443
        # The election advertisement is relayed verbatim: the peer enters the
        # shared election with its real values, instead of the old fabricated
        # "reachable ⇒ serving master" mapping that let a read-only seeder
        # outrank a prefer-master node and made a never-master peer electable.
        assert peers[0].server_ready is True
        assert peers[0].prefer_master is True
        assert peers[0].never_master is False
        assert peers[0].inbound is True
        assert peers[0].startup_time == 1760000000.5
        assert peers[0].sync_direction == "download-only"

    def test_local_cid_is_never_added(self):
        plan, _ = make_plan(cid=OTHER)
        manager = PeerManager(local_cid=OTHER, bucket="shared", group="home")
        plan._peer_manager = manager
        plan._query_peers()
        assert manager.get_alive_peers() == []

    def test_same_subnet_candidate_is_promoted(self, monkeypatch):
        """A LAN candidate must become the primary dial address.

        Regression for the NAT-hairpin failure: two peers behind one NAT cannot
        reach each other's public endpoint (no NAT loopback), so the rendezvous
        relays the full candidate list and the requester must dial the
        same-subnet candidate instead of the publicly advertised one.
        """
        monkeypatch.setattr(
            "hsync_client.p2p_rendezvous_rdv.get_local_ip",
            lambda: "192.168.1.50",
        )
        monkeypatch.setattr(
            "hsync_client.p2p_rendezvous_rdv.get_local_ips",
            lambda: ["192.168.1.50"],
        )
        plan, _ = make_plan()
        public_first = relayed_peer(
            OTHER,
            ip="203.0.113.10",
            port=8443,
            endpoints=[
                {"proto": "tcp", "host": "203.0.113.10", "port": 8443},
                {"proto": "tcp", "host": "192.168.1.42", "port": 8443},
            ],
        )
        peer = plan._peer_from_wire(public_first)
        assert peer is not None
        assert (peer.address, peer.port) == ("192.168.1.42", 8443)
        assert peer.endpoints == (
            ("203.0.113.10", 8443),
            ("192.168.1.42", 8443),
        )

    def test_remote_candidate_keeps_the_advertised_order(self, monkeypatch):
        """With no same-subnet candidate the first advertised endpoint stays primary."""
        monkeypatch.setattr(
            "hsync_client.p2p_rendezvous_rdv.get_local_ip",
            lambda: "10.0.0.5",
        )
        monkeypatch.setattr(
            "hsync_client.p2p_rendezvous_rdv.get_local_ips",
            lambda: ["10.0.0.5"],
        )
        plan, _ = make_plan()
        remote = relayed_peer(
            OTHER,
            ip="203.0.113.10",
            port=8443,
            endpoints=[
                {"proto": "tcp", "host": "203.0.113.10", "port": 8443},
                {"proto": "tcp", "host": "192.168.1.42", "port": 8443},
            ],
        )
        peer = plan._peer_from_wire(remote)
        assert peer is not None
        assert (peer.address, peer.port) == ("203.0.113.10", 8443)

    def test_relayed_connected_count_is_mapped(self):
        """The relayed connected count feeds the slave's presence display."""
        plan, _ = make_plan()
        peer = plan._peer_from_wire(relayed_peer(OTHER, ip="203.0.113.10", port=8443, connected=2))
        assert peer is not None
        assert peer.connected == 2

    def test_missing_relayed_connected_count_defaults_to_zero(self):
        plan, _ = make_plan()
        peer = plan._peer_from_wire(relayed_peer(OTHER, ip="203.0.113.10", port=8443))
        assert peer is not None
        assert peer.connected == 0


class TestAdvertisement:
    """The local election advertisement is published on ``register``."""

    def test_register_carries_the_local_advertisement(self):
        plan, _ = make_plan(
            prefer_master=True,
            startup_time=1760000001.0,
            sync_direction="upload-only",
        )
        plan._register(publish_endpoint=False)
        kind, frame = plan._client.calls[-1]
        assert kind == "register"
        assert frame["server_ready"] is False
        assert frame["prefer_master"] is True
        assert frame["never_master"] is False
        assert frame["startup_time"] == 1760000001.0
        assert frame["sync_direction"] == "upload-only"
        assert validate_rdv_frame(frame) is not None

    def test_startup_time_defaults_to_the_process_start(self):
        plan, _ = make_plan()
        assert plan._startup_time > 0
        plan._register(publish_endpoint=False)
        assert plan._client.calls[-1][1]["startup_time"] == plan._startup_time

    def test_startup_time_uses_the_sntp_corrected_clock(self, monkeypatch):
        """The election tie-break must not depend on each host's raw clock."""
        import hsync_client.p2p_rendezvous as rendezvous_module

        monkeypatch.setattr(rendezvous_module, "now_corrected", lambda: 1760000123.0)
        plan, _ = make_plan()
        assert plan._startup_time == 1760000123.0

    def test_update_server_ready_marks_the_local_incumbency(self):
        """A serving master must win its own election (regression).

        The mDNS plan publishes the incumbent flag into the shared
        ``PeerManager`` (``_refresh_service``); the rendezvous plan used to update
        only its own lease state, so the next relayed ``server_ready`` peer
        demoted the local master and the roles ping-ponged until the data plane
        looped on the other node's read-only seeder endpoint.
        """
        plan, _ = make_plan(prefer_master=True)
        manager = PeerManager(
            local_cid=CID,
            bucket="shared",
            group="home",
            prefer_master=True,
            startup_time=plan._startup_time,
        )
        plan._peer_manager = manager
        manager.add_peer(
            Peer(
                cid=OTHER,
                bucket="shared",
                group="home",
                address="203.0.113.10",
                port=8443,
                use_https=True,
                cert_fingerprint=FP,
                server_ready=True,  # the peer is already serving
                startup_time=plan._startup_time - 60,  # and started earlier
            )
        )
        # Not serving yet: the incumbent peer outranks the local prefer_master.
        assert manager.is_master() is False

        plan.update_server_ready()
        assert manager.is_master() is True

        plan.update_server_not_ready()
        assert manager.is_master() is False

    def test_role_change_republishes_the_advertisement(self):
        """A promotion/yield re-registers until the node acknowledged the value."""
        plan, _ = make_plan()
        plan._register(publish_endpoint=False)
        assert plan._sent_advert_generation == plan._advert_generation

        plan.update_server_ready()
        assert plan._sent_advert_generation != plan._advert_generation
        plan._republish_advert()
        kind, frame = plan._client.calls[-1]
        assert kind == "register"
        assert frame["server_ready"] is True
        assert frame["endpoints"], "a serving node republishes its endpoint"
        assert plan._sent_advert_generation == plan._advert_generation

        plan.update_server_not_ready()
        plan._republish_advert()
        assert plan._client.calls[-1][1]["server_ready"] is False

    def test_a_refused_register_does_not_strand_a_stale_advertisement(self):
        """A refusal leaves the generation unsent so the next wake retries."""
        plan, _ = make_plan()

        def refuse(frame):
            raise RendezvousRefused("rate_limited", 30)

        plan._client.register = refuse
        plan.update_server_ready()
        with pytest.raises(RendezvousRefused):
            plan._republish_advert()
        assert plan._sent_advert_generation != plan._advert_generation

    def test_lease_loop_survives_a_uniform_refusal(self):
        """A refusal is not a transport failure: the loop keeps its lease state.

        Regression: the client used to raise ``RendezvousProtocolError`` ("expected
        a 'ack' frame, got 'response'"), which the lease loop treated as an
        unreachable node — resetting to a full first register on every wake and
        deepening the node's rate limit instead of backing off.
        """
        plan, _ = make_plan(refresh_interval=1.0)
        registrations = []

        def refuse(frame):
            registrations.append(frame)
            raise RendezvousRefused("rate_limited", 30)

        plan._client.register = refuse
        plan._client.query = lambda frame: None
        plan._running = True
        thread = threading.Thread(target=plan._lease_loop, name="test-lease", daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5.0
            while not registrations and time.monotonic() < deadline:
                time.sleep(0.05)
            assert registrations, "the lease loop never attempted a register"
        finally:
            plan._stop.set()
            plan._wake.set()
            thread.join(timeout=5.0)
        # The loop kept running (no crash) and did not immediately re-register:
        # the refusal is retried at the normal cadence, not as a fresh register.
        assert not thread.is_alive()
        assert len(registrations) == 1

    def test_a_token_required_refusal_reaches_the_front_end(self, monkeypatch):
        """A protected-bucket refusal is the one the user can act on.

        ``_log_refusal`` keeps ``rate_limited``/``overloaded`` log-only, but a
        ``token_required`` answer means the group key (shared password) is
        missing or wrong: emit a structured warning so the desktop shows it as
        a toast, not just as a log line.
        """
        plan, _ = make_plan()
        emitted: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            "hsync_client.p2p_rendezvous_rdv.emit_warning",
            lambda message, *, source, reason="": emitted.append((message, source, reason)),
        )

        plan._log_refusal(RendezvousRefused("token_required"))

        assert len(emitted) == 1
        message, source, reason = emitted[0]
        assert source == "rendezvous"
        assert reason == "token_required"
        assert "token_required" in message

    def test_a_transient_refusal_stays_log_only(self, monkeypatch):
        """``rate_limited``/``overloaded`` heal themselves: no structured event."""
        plan, _ = make_plan()
        emitted: list = []
        monkeypatch.setattr(
            "hsync_client.p2p_rendezvous_rdv.emit_warning",
            lambda *a, **k: emitted.append((a, k)),
        )

        plan._log_refusal(RendezvousRefused("rate_limited", 30))

        assert emitted == []

    def test_a_repeated_token_refusal_is_surfaced_once(self, monkeypatch):
        """The per-error dedup keeps the front-end from being re-notified."""
        plan, _ = make_plan()
        emitted: list = []
        monkeypatch.setattr(
            "hsync_client.p2p_rendezvous_rdv.emit_warning",
            lambda *a, **k: emitted.append((a, k)),
        )

        plan._log_refusal(RendezvousRefused("token_required"))
        plan._log_refusal(RendezvousRefused("token_required"))

        assert len(emitted) == 1

    def test_query_refusal_keeps_the_peers_already_known(self):
        """A refused query yields an empty sample by contract, not an exception."""
        plan, _ = make_plan()
        manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan._peer_manager = manager
        plan._query_peers()
        known = [p.cid for p in manager.get_alive_peers()]
        assert known == [OTHER]

        refused = {
            "protocol_version": "rdv/1",
            "type": "response",
            "peers": [],
            "count": 0,
            "ttl": 60,
            "error": "rate_limited",
            "retry_after": 42,
        }
        queries: list[dict] = []

        def refused_query(frame):
            queries.append(frame)
            return validate_rdv_response(dict(refused))

        plan._client.query = refused_query
        plan._next_query_at = 0.0  # bypass the cadence gate; exercise the refusal
        plan._query_peers()  # must not raise
        assert [p.cid for p in manager.get_alive_peers()] == known
        assert plan._next_query_at > time.monotonic(), "retry_after must arm a backoff"
        # A query inside the backoff window is skipped entirely (no network call).
        plan._query_peers()
        assert len(queries) == 1

    def test_query_overloaded_refusal_keeps_the_peers_already_known(self):
        """``overloaded`` (node capacity) is handled like a rate-limit refusal."""
        plan, _ = make_plan()
        manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan._peer_manager = manager
        plan._query_peers()
        known = [p.cid for p in manager.get_alive_peers()]
        assert known == [OTHER]

        refused = {
            "protocol_version": "rdv/1",
            "type": "response",
            "peers": [],
            "count": 0,
            "ttl": 60,
            "error": "overloaded",
            "retry_after": 60,
        }

        def refused_query(frame):
            return validate_rdv_response(dict(refused))

        plan._client.query = refused_query
        plan._next_query_at = 0.0  # bypass the cadence gate; exercise the refusal
        plan._query_peers()  # must not raise
        assert [p.cid for p in manager.get_alive_peers()] == known
        assert plan._next_query_at > time.monotonic(), "retry_after must arm a backoff"


class TestQueryBudget:
    def test_group_incomplete_keeps_the_sustainable_cadence(self):
        """A known master is not proof the group is complete.

        The serving peer relays how many clients it holds, so the group has at
        least ``1 + connected`` members: seeing fewer peers than that means the
        node's random sample missed someone, and only another query finds them —
        the refresh cadence would keep that peer invisible for up to 30 s.
        """
        plan, _ = make_plan()
        manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan._peer_manager = manager
        plan._burst_left = 0
        master = plan._peer_from_wire(
            relayed_peer(OTHER, ip="203.0.113.10", port=8443, server_ready=True, connected=2)
        )
        assert master is not None
        manager.add_peer(master)
        assert manager.get_master_cid() == OTHER
        assert plan._query_interval() == RENDEZVOUS_QUERY_ACTIVE_INTERVAL

    def test_complete_group_uses_the_refresh_cadence(self):
        """A two-node group is complete, so the extra query traffic stops."""
        plan, _ = make_plan()
        manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan._peer_manager = manager
        plan._burst_left = 0
        master = plan._peer_from_wire(
            relayed_peer(OTHER, ip="203.0.113.10", port=8443, server_ready=True, connected=1)
        )
        assert master is not None
        manager.add_peer(master)
        assert plan._query_interval() == plan._refresh_interval

    def test_request_sample_reopens_the_query_gate(self):
        """A WebSocket admission must not wait for the cadence to be listed."""
        plan, _ = make_plan()
        plan._peer_manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan._next_query_at = time.monotonic() + 30.0
        plan.request_sample()
        assert plan._next_query_at == 0.0
        assert plan._wake.is_set()
        plan._query_peers()
        assert [call for call in plan._client.calls if call[0] == "query"]

    def test_a_drawn_peer_without_endpoints_is_re_sampled_soon(self):
        """A peer that exists but is not dialable yet must not wait a cadence.

        Its first ``register`` carries no ``endpoints[]`` (the STUN echo comes
        first and the publication is the next lease tick), which is exactly the
        state a master draws right after admitting the peer's WebSocket.
        """
        plan, _ = make_plan()
        manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan._peer_manager = manager
        plan._burst_left = 0
        plan._client.query = lambda frame: validate_rdv_response(
            {
                "protocol_version": "rdv/1",
                "type": "response",
                "peers": [
                    relayed_peer(OTHER, ip="203.0.113.10", port=8443, reachable=False),
                ],
                "count": 1,
                "ttl": 60,
            }
        )
        plan._query_peers()
        assert manager.get_alive_peers() == []
        assert plan._unpublished_peer is True
        assert plan._next_query_at - time.monotonic() <= RENDEZVOUS_QUERY_BURST_INTERVAL
        # Bounded: a peer that never publishes stops earning retries and leaves
        # the plan on the sustainable cadence instead of the refresh one.
        for _ in range(RENDEZVOUS_UNPUBLISHED_SAMPLE_RETRIES + 2):
            plan._next_query_at = 0.0
            plan._query_peers()
        assert plan._unpublished_retries == RENDEZVOUS_UNPUBLISHED_SAMPLE_RETRIES
        assert plan._query_interval() == RENDEZVOUS_QUERY_ACTIVE_INTERVAL
        # The retries stop as soon as a sample draws no unpublished peer.
        plan._client.query = lambda frame: validate_rdv_response(
            {
                "protocol_version": "rdv/1",
                "type": "response",
                "peers": [relayed_peer(OTHER, ip="203.0.113.10", port=8443)],
                "count": 1,
                "ttl": 60,
            }
        )
        plan._next_query_at = 0.0
        plan._query_peers()
        assert plan._unpublished_retries == 0

    def test_the_lease_loop_wakes_when_a_sample_is_due(self):
        """An early scheduled sample must not wait the whole cadence.

        The loop re-checks the gate itself, so it has to sleep until the gate
        opens: a node-supplied ``retry_after`` and an unpublished-peer re-sample
        both schedule a sample sooner than the lease cadence.
        """
        plan, _ = make_plan()
        manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan._peer_manager = manager
        plan._burst_left = 0
        plan._next_query_at = time.monotonic() + 1.0
        assert plan._next_wake_delay() <= 1.05
        # A backoff is a floor, never a ceiling.
        assert plan._next_wake_delay(backoff=5.0) >= 5.0
        # The cadence applies once the gate is already open.
        plan._next_query_at = 0.0
        assert plan._next_wake_delay() == plan._query_interval()

    def test_queries_respect_the_cadence(self):
        """Steady-state queries are spaced by the refresh interval, not per tick."""
        plan, _ = make_plan()
        manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan._peer_manager = manager
        plan._query_peers()
        first = len([c for c in plan._client.calls if c[0] == "query"])
        assert first == 1
        plan._query_peers()  # still inside the window: skipped
        assert len([c for c in plan._client.calls if c[0] == "query"]) == first
        plan._next_query_at = 0.0  # window elapsed
        plan._query_peers()
        assert len([c for c in plan._client.calls if c[0] == "query"]) == first + 1


class TestLeaseFailurePolicy:
    """A slow round is not a dead node, and a retry must not hammer one."""

    @staticmethod
    def _failure() -> RendezvousUnreachable:
        return RendezvousUnreachable(
            "rendezvous rdv.example.net:443: timed out waiting for an ack",
            detail="timed out waiting for an ack",
        )

    def test_one_failure_does_not_warn_and_cancels_the_burst(self, monkeypatch):
        """A busy uplink looks exactly like this: INFO, no banner, no burst."""
        import hsync_client.p2p_rendezvous_rdv as rdv_module

        warned: list[str] = []
        monkeypatch.setattr(
            rdv_module, "emit_warning", lambda message, **_kw: warned.append(message)
        )
        plan, _ = make_plan()
        plan._burst_left = RENDEZVOUS_QUERY_BURST_COUNT

        delay = plan._record_lease_failure(self._failure())

        assert warned == []
        assert plan._burst_left == 0
        assert plan._consecutive_failures == 1
        # Each attempt already costs the whole reply budget: the next one waits.
        assert RDV_FAILURE_BACKOFF_BASE_S * 0.5 <= delay <= RDV_FAILURE_BACKOFF_BASE_S

    def test_a_run_of_failures_warns_once_without_a_duplicated_authority(self, monkeypatch):
        import hsync_client.p2p_rendezvous_rdv as rdv_module

        warned: list[str] = []
        monkeypatch.setattr(
            rdv_module, "emit_warning", lambda message, **_kw: warned.append(message)
        )
        plan, _ = make_plan()

        for _ in range(RDV_FAILURE_WARN_AFTER - 1):
            plan._record_lease_failure(self._failure())
        assert warned == []
        plan._record_lease_failure(self._failure())

        assert warned == [
            "Rendezvous rdv.example.net:443 unreachable: timed out waiting for an ack"
        ]

    def test_the_backoff_doubles_and_is_capped(self, monkeypatch):
        """Un-jittered retries would synchronize the peers behind one NAT."""
        import hsync_client.p2p_rendezvous_rdv as rdv_module

        monkeypatch.setattr(rdv_module, "emit_warning", lambda *_a, **_kw: None)
        plan, _ = make_plan()

        bounds = []
        for _ in range(6):
            delay = plan._record_lease_failure(self._failure())
            exponent = min(plan._consecutive_failures - 1, 3)
            nominal = min(RDV_FAILURE_BACKOFF_BASE_S * 2**exponent, RDV_FAILURE_BACKOFF_MAX_S)
            assert nominal * 0.5 <= delay <= nominal
            bounds.append(nominal)

        assert bounds[:4] == [5.0, 10.0, 20.0, 30.0]
        assert bounds[4:] == [RDV_FAILURE_BACKOFF_MAX_S] * 2

    def test_success_resets_the_streak_and_retracts_the_warning(self, monkeypatch):
        import hsync_client.p2p_rendezvous_rdv as rdv_module

        cleared: list[str] = []
        monkeypatch.setattr(rdv_module, "emit_warning_cleared", cleared.append)
        monkeypatch.setattr(rdv_module, "emit_warning", lambda *_a, **_kw: None)
        plan, _ = make_plan()
        plan._record_lease_failure(self._failure())

        plan._clear_lease_failure()

        assert plan._consecutive_failures == 0
        assert cleared == ["rendezvous"]


class TestWaitForRendezvous:
    def test_wait_never_queries_the_rendezvous(self):
        """Regression: the wait must not poll ``query`` (1 s poll → refusal storm).

        The rendezvous rate-limits ``query`` to 5/min per (IP, bucket); a rendezvous
        wait that queries itself exhausts the budget and is refused for the rest
        of the window. The lease loop owns the network cadence.
        """
        plan, _ = make_plan()
        plan._peer_manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan.wait_for_rendezvous(timeout=0.05)
        assert [c for c in plan._client.calls if c[0] == "query"] == []

    def test_wait_returns_as_soon_as_a_peer_is_known(self):
        plan, _ = make_plan()
        manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan._peer_manager = manager
        manager.add_peer(plan._peer_from_wire(RESPONSE["peers"][0]))
        start = time.monotonic()
        plan.wait_for_rendezvous(timeout=5.0)
        assert time.monotonic() - start < 1.0, "must wake on the peer-change event"
        assert [c for c in plan._client.calls if c[0] == "query"] == []

    def test_unreachable_rendezvous_does_not_abort_the_wait(self):
        """The wait never touches the network, so a dead rendezvous cannot break it."""
        plan, _ = make_plan()
        plan._peer_manager = PeerManager(local_cid=CID, bucket="shared", group="home")

        def boom(frame):
            raise RendezvousUnreachable("rendezvous rdv.example.net:443: ConnectionError")

        plan._client.query = boom
        plan.wait_for_rendezvous(timeout=0.05)

    def test_lease_loop_reports_an_unreachable_node_as_a_warning(self, monkeypatch):
        """A dead node is retried and reported after a run of failures, never disabled.

        The first failed rounds only log: one slow round is what a saturated
        uplink looks like (``_record_lease_failure``), so the warning — and the UI
        banner — describe a run of them. The backoff is compressed here so the
        escalation is reachable inside the test's window.
        """
        warnings: list[dict] = []
        monkeypatch.setattr(
            "hsync_client.p2p_rendezvous_rdv.emit_warning",
            lambda message, **kwargs: warnings.append({"message": message, **kwargs}),
        )
        monkeypatch.setattr("hsync_client.p2p_rendezvous_rdv.RDV_FAILURE_WARN_AFTER", 2)
        monkeypatch.setattr("hsync_client.p2p_rendezvous_rdv.RDV_FAILURE_BACKOFF_BASE_S", 0.01)
        monkeypatch.setattr("hsync_client.p2p_rendezvous_rdv.RDV_FAILURE_BACKOFF_MAX_S", 0.01)
        plan, _ = make_plan(refresh_interval=0.01)

        def unreachable(_frame):
            raise RendezvousUnreachable("ConnectionError")

        plan._client.register = unreachable
        plan._running = True
        thread = threading.Thread(target=plan._lease_loop, name="test-lease-dead", daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10.0
            while not warnings and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            plan._stop.set()
            plan._wake.set()
            thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert warnings, "a run of failures must reach the warning"
        assert {(w["source"], w["reason"]) for w in warnings} == {("rendezvous", "unreachable")}
        assert plan._consecutive_failures >= 2
        # A source that may come back is retried, not disabled.
        assert plan.source_disabled() is False

    def test_a_disabled_source_does_not_break_the_wait(self):
        """An untrusted node disables its source; the wait still returns.

        Regression: the wait used to re-raise the trust failure, which ended the
        whole p2p run even when the local network had peers to sync with.
        """
        plan, _ = make_plan()
        plan._peer_manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan._disable_source("pin mismatch")

        plan.wait_for_rendezvous(timeout=0.05)

        assert plan.source_disabled() is True

    def test_disable_source_logs_guidance_with_angle_brackets_literally(self):
        """A ``<...>`` in the recovery prose must reach the log, not crash it.

        Regression: ``--rendezvous-server-fingerprint <fp>`` made loguru's
        colorizer raise on the record, which dropped the ERROR and hid that the
        source stopped. The record must be emitted verbatim.
        """
        import io

        import loguru

        from hsync_client.logger import _format_record_colored

        buffer = io.StringIO()
        # The client's console formatter with ``colorize=True`` parses markup in
        # the message, reproducing the runtime sink: pre-fix the ``<fp>`` made it
        # raise and the ERROR record was dropped.
        sink = loguru.logger.add(
            buffer, format=_format_record_colored, colorize=True, enqueue=False
        )
        try:
            plan, _ = make_plan()
            plan._peer_manager = PeerManager(local_cid=CID, bucket="shared", group="home")
            plan._disable_source(
                "untrusted", guidance="pass --rendezvous-server-fingerprint <fp> to trust it"
            )
        finally:
            loguru.logger.remove(sink)

        assert plan.source_disabled() is True
        assert "pass --rendezvous-server-fingerprint <fp> to trust it" in buffer.getvalue()


class TestUntrustedNodeDisablesTheSource:
    def test_lease_loop_disables_the_source_on_a_pin_mismatch(self):
        """A ``RendezvousFingerprintMismatch`` stops this source, not the run."""
        plan, _ = make_plan(refresh_interval=1.0)

        def mismatch(frame):
            raise RendezvousFingerprintMismatch("pin mismatch")

        plan._client.register = mismatch
        plan._running = True
        thread = threading.Thread(target=plan._lease_loop, name="test-lease", daemon=True)
        thread.start()
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "the lease loop must stop on a pin mismatch"
        assert plan.source_disabled() is True
        # The remaining sources are untouched: the shared peer state still
        # delivers whatever the local network found.
        plan._peer_manager = PeerManager(local_cid=CID, bucket="shared", group="home")
        plan.wait_for_rendezvous(timeout=0.05)

    def test_lease_loop_disables_the_source_on_a_refused_prompt(self):
        """A declined/unpromptable trust disables the source: never re-prompt."""
        plan, _ = make_plan(refresh_interval=1.0)

        def refuse(frame):
            raise RendezvousNotTrusted("fingerprint not accepted")

        plan._client.register = refuse
        plan._running = True
        thread = threading.Thread(target=plan._lease_loop, name="test-lease", daemon=True)
        thread.start()
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "the lease loop must stop when trust is refused"
        assert plan.source_disabled() is True

    def test_lease_loop_logs_the_recovery_guidance_once(self):
        """The recovery block is logged where trust failed, in one line."""
        from loguru import logger as log

        plan, _ = make_plan(refresh_interval=1.0)
        reason = "rendezvous rdv.example.net:443: certificate fingerprint does not match"

        def mismatch(_frame):
            raise RendezvousFingerprintMismatch(
                reason,
                guidance="Rendezvous 'rdv.example.net:443' trust failed: GUIDANCE",
            )

        plan._client.register = mismatch
        messages: list[str] = []
        sink_id = log.add(messages.append, level="ERROR", format="{message}")
        plan._running = True
        thread = threading.Thread(target=plan._lease_loop, name="test-lease-guidance", daemon=True)
        try:
            thread.start()
            thread.join(timeout=5.0)
        finally:
            log.remove(sink_id)
        assert not thread.is_alive()
        assert any("GUIDANCE" in message for message in messages)

    def test_the_disable_is_published_as_a_structured_warning(self, monkeypatch):
        """The front-end learns the source degraded (one line, not the guidance)."""
        warnings: list[dict] = []
        monkeypatch.setattr(
            "hsync_client.p2p_rendezvous_rdv.emit_warning",
            lambda message, **kwargs: warnings.append({"message": message, **kwargs}),
        )
        plan, _ = make_plan()

        plan._disable_source(
            "rendezvous rdv.example.net:443: certificate fingerprint does not match",
            guidance="Rendezvous 'rdv.example.net:443' trust failed: GUIDANCE",
        )

        assert warnings == [
            {
                "message": "rendezvous rdv.example.net:443: certificate fingerprint does not "
                "match — rdv rendezvous stopped, local network discovery continues",
                "source": "rendezvous",
                "reason": "untrusted",
            }
        ]


class TestStop:
    def test_stop_sends_leave_and_unmaps(self):
        plan, fake_upnp = make_plan(upnp_enabled=True)
        plan._shared_upnp.external_port(plan._port)
        plan._running = True
        plan._thread = None
        plan.stop()
        assert any(kind == "leave" for kind, _ in plan._client.calls)
        assert fake_upnp.unmapped is True

    def test_stop_after_an_untrusted_node_skips_leave(self):
        """Never contact a node declared untrusted: no lease to release."""
        plan, fake_upnp = make_plan(upnp_enabled=True)
        plan._disable_source("pin mismatch")
        plan._shared_upnp.external_port(plan._port)
        plan._running = True
        plan._thread = None
        plan.stop()
        assert not any(kind == "leave" for kind, _ in plan._client.calls)
        assert fake_upnp.unmapped is True


def test_rendezvous_default_port_matches_the_shared_ssot() -> None:
    """The client's bare-host default is the shared rdv/1 default (spec §Transport)."""
    from hsync_common.constants import RDV_DEFAULT_PORT

    from hsync_client.constants import RENDEZVOUS_DEFAULT_PORT

    assert RENDEZVOUS_DEFAULT_PORT == RDV_DEFAULT_PORT == 443


def test_register_is_skipped_without_a_local_fingerprint() -> None:
    """No pin-comparable cert_fp means no register, never an invalid frame."""
    plan, _ = make_plan(cert_fingerprint=None)
    calls_before = len(plan._client.calls)
    plan._register(publish_endpoint=False)
    assert len(plan._client.calls) == calls_before


class TestNetworkChange:
    """An address change must republish the endpoints the rendezvous relays."""

    def test_a_changed_address_republishes_with_the_new_candidates(self, monkeypatch):
        plan, fake_upnp = make_plan(upnp_enabled=True)
        monkeypatch.setattr("hsync_client.p2p_rendezvous_rdv.get_local_ip", lambda: "203.0.113.10")
        monkeypatch.setattr(
            "hsync_client.p2p_rendezvous_rdv.get_local_ips", lambda: ["203.0.113.10"]
        )
        plan.update_server_ready()
        plan._publish_endpoint()

        _kind, first = plan._client.calls[-1]
        assert "203.0.113.10" in {endpoint["host"] for endpoint in first["endpoints"]}
        assert plan._published_local_ip == "203.0.113.10"

        monkeypatch.setattr("hsync_client.p2p_rendezvous_rdv.get_local_ip", lambda: "198.51.100.7")
        monkeypatch.setattr(
            "hsync_client.p2p_rendezvous_rdv.get_local_ips", lambda: ["198.51.100.7"]
        )
        assert plan._republish_stale_endpoint() is True
        assert plan._published_endpoints is False
        # The mapping belonged to the previous network: it is dropped and the
        # next publish maps the live port again.
        assert fake_upnp.unmapped is True

        plan._publish_endpoint()
        _kind, republished = plan._client.calls[-1]
        assert "198.51.100.7" in {endpoint["host"] for endpoint in republished["endpoints"]}
        assert "203.0.113.10" not in {endpoint["host"] for endpoint in republished["endpoints"]}
        assert plan._published_local_ip == "198.51.100.7"
        # The new network needs its own mapping: the port is mapped again.
        assert fake_upnp.mapped == [8443, 8443]

    def test_an_unchanged_address_keeps_the_publication(self, monkeypatch):
        plan, _ = make_plan()
        monkeypatch.setattr("hsync_client.p2p_rendezvous_rdv.get_local_ip", lambda: "203.0.113.10")
        monkeypatch.setattr(
            "hsync_client.p2p_rendezvous_rdv.get_local_ips", lambda: ["203.0.113.10"]
        )
        plan.update_server_ready()
        plan._publish_endpoint()
        assert plan._republish_stale_endpoint() is False
        assert plan._published_endpoints is True

    def test_nothing_to_republish_before_the_first_publication(self, monkeypatch):
        """A node that never advertised an endpoint has nothing to invalidate."""
        plan, _ = make_plan()
        monkeypatch.setattr("hsync_client.p2p_rendezvous_rdv.get_local_ip", lambda: "198.51.100.7")
        assert plan._republish_stale_endpoint() is False

    def test_a_network_change_wakes_the_lease_loop(self):
        from hsync_client.net_watch import NetworkSignature

        plan, _ = make_plan()
        plan._on_network_change(NetworkSignature(source_ip="198.51.100.7", online=True))
        assert plan._wake.is_set()

    def test_a_lease_loss_republishes_the_serving_master_endpoint(self):
        """A dropped lease must not strand a serving master without candidates."""
        plan, _ = make_plan()
        plan.update_server_ready()
        plan._publish_endpoint()
        plan._client.refresh = lambda frame: None  # the node dropped the lease
        plan._external_port = 8443
        plan._refresh()
        _kind, frame = plan._client.calls[-1]
        assert frame["endpoints"], "the re-register after a lost lease must carry endpoints"
        assert plan._published_endpoints is True


class TestAdvertisedEndpointsOverride:
    """The ``HSYNC_P2P_TEST_ADVERTISED_ENDPOINTS`` seam (relay-e2e enabler)."""

    def test_unset_keeps_the_derived_candidates(self, monkeypatch):
        monkeypatch.delenv(_TEST_ADVERTISED_ENDPOINTS_ENV, raising=False)
        plan, _ = make_plan()
        plan._observed = ObservedAddress(host="203.0.113.9", port=51000)
        assert plan._endpoint_wire(["192.0.2.10"]) == [
            {"proto": "tcp", "host": "203.0.113.9", "port": 8443},
            {"proto": "tcp", "host": "192.0.2.10", "port": 8443},
        ]

    def test_override_replaces_the_derived_candidates(self, monkeypatch):
        monkeypatch.setenv(_TEST_ADVERTISED_ENDPOINTS_ENV, "198.51.100.7:8443,198.51.100.8:8443")
        plan, _ = make_plan()
        plan._observed = ObservedAddress(host="203.0.113.9", port=51000)
        assert plan._endpoint_wire(["192.0.2.10"]) == [
            {"proto": "tcp", "host": "198.51.100.7", "port": 8443},
            {"proto": "tcp", "host": "198.51.100.8", "port": 8443},
        ]

    def test_override_is_published_in_the_register_frame(self, monkeypatch):
        """The published frame must carry the override verbatim (two dead candidates)."""
        monkeypatch.setenv(_TEST_ADVERTISED_ENDPOINTS_ENV, "198.51.100.7:8443,198.51.100.8:8443")
        plan, _ = make_plan()
        plan._register(publish_endpoint=True)
        _kind, frame = plan._client.calls[-1]
        assert frame["endpoints"] == [
            {"proto": "tcp", "host": "198.51.100.7", "port": 8443},
            {"proto": "tcp", "host": "198.51.100.8", "port": 8443},
        ]
        assert validate_rdv_frame(frame) is not None

    def test_non_routable_override_candidates_are_dropped(self, monkeypatch):
        """A loopback/link-local candidate must not invalidate the whole register."""
        monkeypatch.setenv(_TEST_ADVERTISED_ENDPOINTS_ENV, "127.0.0.1:8443,198.51.100.7:8443")
        plan, _ = make_plan()
        assert plan._endpoint_wire(["192.0.2.10"]) == [
            {"proto": "tcp", "host": "198.51.100.7", "port": 8443},
        ]

    def test_all_non_routable_candidates_yield_an_endpointless_register(self, monkeypatch):
        monkeypatch.setenv(_TEST_ADVERTISED_ENDPOINTS_ENV, "127.0.0.1:8443")
        plan, _ = make_plan()
        assert plan._endpoint_wire(["192.0.2.10"]) == []

    def test_malformed_override_fails_fast(self, monkeypatch):
        monkeypatch.setenv(_TEST_ADVERTISED_ENDPOINTS_ENV, "not-an-endpoint")
        plan, _ = make_plan()
        with pytest.raises(ValueError):
            plan._endpoint_wire(["192.0.2.10"])

    def test_file_seam_replaces_the_derived_candidates(self, monkeypatch, tmp_path):
        """The file seam feeds the register frame, re-read on every call."""
        seam = tmp_path / "endpoints"
        seam.write_text("198.51.100.7:8443,198.51.100.8:8443\n", encoding="utf-8")
        monkeypatch.setenv(_TEST_ADVERTISED_ENDPOINTS_FILE_ENV, str(seam))
        monkeypatch.delenv(_TEST_ADVERTISED_ENDPOINTS_ENV, raising=False)
        plan, _ = make_plan()
        assert plan._endpoint_wire(["192.0.2.10"]) == [
            {"proto": "tcp", "host": "198.51.100.7", "port": 8443},
            {"proto": "tcp", "host": "198.51.100.8", "port": 8443},
        ]

    def test_a_changed_file_seam_triggers_a_republish(self, monkeypatch, tmp_path):
        """Rewriting the seam file republishes the new candidates (runtime seam)."""
        seam = tmp_path / "endpoints"
        seam.write_text("192.0.2.20:8443\n", encoding="utf-8")
        monkeypatch.setenv(_TEST_ADVERTISED_ENDPOINTS_FILE_ENV, str(seam))
        monkeypatch.delenv(_TEST_ADVERTISED_ENDPOINTS_ENV, raising=False)
        plan, _ = make_plan()
        plan.update_server_ready()
        plan._publish_endpoint()
        assert plan._republish_stale_endpoint() is False

        seam.write_text("198.51.100.7:8443\n", encoding="utf-8")
        assert plan._republish_stale_endpoint() is True
        plan._publish_endpoint()
        _kind, frame = plan._client.calls[-1]
        assert frame["endpoints"] == [{"proto": "tcp", "host": "198.51.100.7", "port": 8443}]


class TestMasterClaimGate:
    """E2: no incumbency claim before the rendezvous has been seen once."""

    def test_not_settled_before_the_first_answered_query(self):
        plan, _ = make_plan()
        plan._started_at = time.monotonic()
        assert plan.discovery_settled() is False

        plan._observed_sample = True
        assert plan.discovery_settled() is True

    def test_unreachable_rendezvous_settles_after_the_grace(self):
        from hsync_client.constants import MASTER_CLAIM_SETTLE_S

        plan, _ = make_plan()
        plan._started_at = time.monotonic() - (MASTER_CLAIM_SETTLE_S + 1.0)
        assert plan.discovery_settled() is True
