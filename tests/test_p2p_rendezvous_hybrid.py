"""Combined mDNS + rdv rendezvous (``p2p_rendezvous_hybrid``).

The rendezvous tests cover plan selection and the shared-manager fan-out; the
source-specific behaviour lives in the two plan test files.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hsync_common.endpoint import HostPort

from hsync_client.p2p_rendezvous import build_rendezvous_plan
from hsync_client.p2p_rendezvous_hybrid import HybridRendezvous

_CID = "A" * 21


def _config(**overrides):
    config = {
        "CID": _CID,
        "REMOTE_BUCKET": "shared",
        "P2P_LISTEN_PORT": 0,
        "P2P_GROUP": None,
        "P2P_PREFER_MASTER": False,
        "P2P_NEVER_MASTER": False,
        "SYNC_DIRECTION": "bidirectional",
    }
    config.update(overrides)
    return config


def _hybrid() -> tuple[HybridRendezvous, MagicMock, MagicMock, MagicMock]:
    mdns = MagicMock()
    mdns.port = 9000
    shared = MagicMock(name="shared_peer_manager")
    mdns.create_peer_manager.return_value = shared
    rendezvous = MagicMock()
    return HybridRendezvous(mdns=mdns, rendezvous=rendezvous), mdns, rendezvous, shared


class TestBuildRendezvousPlan:
    def test_mdns_only_without_a_rendezvous(self):
        with patch("hsync_client.p2p_rendezvous_mdns.MdnsRendezvous") as mdns:
            plan = build_rendezvous_plan(_config(), cert_fingerprint="aa", port=9000)
        assert plan is mdns.return_value

    def test_hybrid_when_a_rendezvous_server_is_set(self):
        """A configured discovery server adds its source next to mDNS."""
        with (
            patch("hsync_client.p2p_rendezvous_mdns.MdnsRendezvous") as mdns,
            patch("hsync_client.p2p_rendezvous_rdv.RdvRendezvous") as rendezvous,
        ):
            plan = build_rendezvous_plan(
                _config(RENDEZVOUS_SERVER_ENDPOINTS=(HostPort("node.example", 443),)),
                cert_fingerprint="aa",
                port=9000,
            )
        assert isinstance(plan, HybridRendezvous)
        # The mDNS source is always present next to the rendezvous source.
        assert plan._mdns is mdns.return_value
        assert plan._rendezvous is rendezvous.return_value
        assert rendezvous.call_args.kwargs["rendezvous_host"] == "node.example"
        assert rendezvous.call_args.kwargs["rendezvous_port"] == 443

    def test_rendezvous_mode_floors_the_peer_liveness_timeout(self):
        """Regression: the liveness window must span >= 2 rendezvous query cycles.

        A rendezvous peer is re-seen only when its CID is drawn in the node's
        random sample, which arrives at the lease cadence. The shared
        PeerManager otherwise used the mDNS-derived window (default 15 s) and
        declared live cross-network peers stale between two samples, churning
        the election. Both children of the one shared manager must carry the
        same floored value.
        """
        with (
            patch("hsync_client.p2p_rendezvous_mdns.MdnsRendezvous") as mdns,
            patch("hsync_client.p2p_rendezvous_rdv.RdvRendezvous") as rendezvous,
        ):
            build_rendezvous_plan(
                _config(RENDEZVOUS_SERVER_ENDPOINTS=(HostPort("node.example", 443),)),
                cert_fingerprint="aa",
                port=9000,
            )
        # default PEER_TIMEOUT 15 < 2 * refresh 30 => floored to 60
        assert rendezvous.call_args.kwargs["peer_timeout"] == 60
        assert mdns.call_args.kwargs["peer_timeout"] == 60
        assert rendezvous.call_args.kwargs["refresh_interval"] == 30

    def test_rendezvous_mode_floor_follows_a_custom_refresh_interval(self):
        with (
            patch("hsync_client.p2p_rendezvous_mdns.MdnsRendezvous") as mdns,
            patch("hsync_client.p2p_rendezvous_rdv.RdvRendezvous") as rendezvous,
        ):
            build_rendezvous_plan(
                _config(
                    RENDEZVOUS_SERVER_ENDPOINTS=(HostPort("node.example", 443),),
                    RENDEZVOUS_SERVER_REFRESH_INTERVAL=10,
                ),
                cert_fingerprint="aa",
                port=9000,
            )
        # 2 * 10 = 20 > default 15 => floored to 20
        assert rendezvous.call_args.kwargs["peer_timeout"] == 20
        assert mdns.call_args.kwargs["peer_timeout"] == 20

    def test_rendezvous_mode_preserves_an_explicitly_larger_timeout(self):
        with (
            patch("hsync_client.p2p_rendezvous_mdns.MdnsRendezvous") as mdns,
            patch("hsync_client.p2p_rendezvous_rdv.RdvRendezvous") as rendezvous,
        ):
            build_rendezvous_plan(
                _config(
                    RENDEZVOUS_SERVER_ENDPOINTS=(HostPort("node.example", 443),), PEER_TIMEOUT=120
                ),
                cert_fingerprint="aa",
                port=9000,
            )
        assert rendezvous.call_args.kwargs["peer_timeout"] == 120
        assert mdns.call_args.kwargs["peer_timeout"] == 120

    def test_mdns_only_keeps_the_configured_timeout(self):
        """Without a rendezvous the mDNS cadence governs; no floor is applied."""
        with patch("hsync_client.p2p_rendezvous_mdns.MdnsRendezvous") as mdns:
            build_rendezvous_plan(_config(PEER_TIMEOUT=20), cert_fingerprint="aa", port=9000)
        assert mdns.call_args.kwargs["peer_timeout"] == 20


class TestHybridLifecycle:
    def test_start_shares_one_peer_manager_between_both_plans(self):
        hybrid, mdns, rendezvous, shared = _hybrid()
        manager = hybrid.start()
        assert manager is shared
        assert hybrid.peer_manager is shared
        mdns.create_peer_manager.assert_called_once()
        mdns.start.assert_called_once_with(register_service=True, peer_manager=shared)
        rendezvous.start.assert_called_once_with(register_service=True, peer_manager=shared)

    def test_stop_stops_both_plans(self):
        hybrid, mdns, rendezvous, _ = _hybrid()
        hybrid.start()
        hybrid.stop()
        rendezvous.stop.assert_called_once()
        mdns.stop.assert_called_once()

    def test_port_comes_from_the_mdns_child(self):
        hybrid, _, _, _ = _hybrid()
        assert hybrid.port == 9000


class TestHybridWait:
    def test_waits_on_the_shared_manager_and_reports_once(self):
        hybrid, mdns, rendezvous, shared = _hybrid()
        rendezvous.source_disabled.return_value = False
        hybrid.start()
        hybrid.wait_for_rendezvous(timeout=5.0)
        shared.wait_for_change.assert_called_once_with(5.0)
        # Presence is emitted exactly once, by the mDNS child; the rendezvous
        # child contributes only the rendezvous log.
        mdns.emit_rendezvous_status.assert_called_once()
        rendezvous.log_peer_count.assert_called_once()

    def test_a_disabled_rendezvous_source_never_fails_the_wait(self):
        """An untrusted rendezvous degrades rendezvous to mDNS (regression).

        The run must keep syncing on the local network: the hybrid wait returns
        normally and the mDNS peer state stays the source of truth.
        """
        hybrid, mdns, rendezvous, shared = _hybrid()
        rendezvous.source_disabled.return_value = True
        hybrid.start()

        hybrid.wait_for_rendezvous(timeout=5.0)

        shared.wait_for_change.assert_called_once_with(5.0)
        mdns.emit_rendezvous_status.assert_called_once()


class TestHybridFanOut:
    def test_endpoint_ready_reaches_both_plans(self):
        hybrid, mdns, rendezvous, _ = _hybrid()
        hybrid.update_endpoint_ready(True)
        mdns.update_endpoint_ready.assert_called_once_with(True)
        rendezvous.update_endpoint_ready.assert_called_once_with(True)

    def test_server_ready_reaches_both_plans(self):
        hybrid, mdns, rendezvous, _ = _hybrid()
        hybrid.update_server_ready()
        mdns.update_server_ready.assert_called_once()
        rendezvous.update_server_ready.assert_called_once()
        hybrid.update_server_not_ready()
        mdns.update_server_not_ready.assert_called_once()
        rendezvous.update_server_not_ready.assert_called_once()

    def test_connected_clients_reach_both_plans(self):
        hybrid, mdns, rendezvous, _ = _hybrid()
        hybrid.set_connected_clients(3)
        mdns.set_connected_clients.assert_called_once_with(3)
        rendezvous.set_connected_clients.assert_called_once_with(3)
        provider = lambda: 1  # noqa: E731
        hybrid.set_connected_clients_provider(provider)
        mdns.set_connected_clients_provider.assert_called_once_with(provider)
        rendezvous.set_connected_clients_provider.assert_called_once_with(provider)

    def test_master_eligibility_withdrawal_reaches_both_plans(self):
        hybrid, mdns, rendezvous, _ = _hybrid()

        hybrid.set_master_eligibility(False)

        mdns.set_master_eligibility.assert_called_once_with(False)
        rendezvous.set_master_eligibility.assert_called_once_with(False)

        hybrid.set_master_eligibility(True)

        mdns.set_master_eligibility.assert_called_with(True)
        rendezvous.set_master_eligibility.assert_called_with(True)

    def test_endpoint_published_requires_every_source(self):
        hybrid, mdns, rendezvous, _ = _hybrid()
        rendezvous.source_disabled.return_value = False
        mdns.endpoint_published.return_value = True
        rendezvous.endpoint_published.return_value = False
        assert hybrid.endpoint_published() is False
        rendezvous.endpoint_published.return_value = True
        assert hybrid.endpoint_published() is True

    def test_a_disabled_source_is_skipped_for_readiness(self):
        """A source that can never publish must not delay master readiness.

        Regression: with an untrusted rendezvous the AND-composition held the
        master in the bounded readiness wait for an endpoint that could never
        arrive, on top of the source carrying no traffic at all.
        """
        hybrid, mdns, rendezvous, _ = _hybrid()
        mdns.endpoint_published.return_value = True
        rendezvous.endpoint_published.return_value = False
        rendezvous.source_disabled.return_value = True
        assert hybrid.endpoint_published() is True
