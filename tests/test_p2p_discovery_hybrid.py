"""Combined mDNS + bootnode discovery (``p2p_discovery_hybrid``).

The bootstrap tests cover plan selection and the shared-manager fan-out; the
source-specific behaviour lives in the two plan test files.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hsync_client.p2p_discovery import DiscoveryFatalError, build_discovery_plan
from hsync_client.p2p_discovery_hybrid import HybridDiscovery

_CID = "A" * 21


def _config(**overrides):
    config = {
        "CID": _CID,
        "REMOTE_BUCKET": "shared",
        "P2P_LISTEN_PORT": 0,
        "P2P_GROUP": None,
        "P2P_FORCE_MASTER": False,
        "P2P_NEVER_MASTER": False,
        "SYNC_DIRECTION": "bidirectional",
    }
    config.update(overrides)
    return config


def _hybrid() -> tuple[HybridDiscovery, MagicMock, MagicMock, MagicMock]:
    mdns = MagicMock()
    mdns.port = 9000
    shared = MagicMock(name="shared_peer_manager")
    mdns.create_peer_manager.return_value = shared
    bootnode = MagicMock()
    return HybridDiscovery(mdns=mdns, bootnode=bootnode), mdns, bootnode, shared


class TestBuildDiscoveryPlan:
    def test_mdns_only_without_a_bootnode(self):
        with patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery") as mdns:
            plan = build_discovery_plan(_config(), cert_fingerprint="aa", port=9000)
        assert plan is mdns.return_value

    def test_hybrid_when_a_bootnode_is_set(self):
        """A configured bootnode adds the bootnode source next to mDNS."""
        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery") as mdns,
            patch("hsync_client.p2p_discovery_bootnode.BootnodeDiscovery") as bootnode,
        ):
            plan = build_discovery_plan(
                _config(BOOTNODE="node.example", BOOTNODE_PORT=443),
                cert_fingerprint="aa",
                port=9000,
            )
        assert isinstance(plan, HybridDiscovery)
        # The mDNS source is always present next to the bootnode source.
        assert plan._mdns is mdns.return_value
        assert plan._bootnode is bootnode.return_value
        assert bootnode.call_args.kwargs["bootnode_host"] == "node.example"
        assert bootnode.call_args.kwargs["bootnode_port"] == 443

    def test_bootnode_mode_floors_the_peer_liveness_timeout(self):
        """Regression: the liveness window must span >= 2 bootnode query cycles.

        A bootnode peer is re-seen only when its CID is drawn in the node's
        random sample, which arrives at the lease cadence. The shared
        PeerManager otherwise used the mDNS-derived window (default 15 s) and
        declared live cross-network peers stale between two samples, churning
        the election. Both children of the one shared manager must carry the
        same floored value.
        """
        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery") as mdns,
            patch("hsync_client.p2p_discovery_bootnode.BootnodeDiscovery") as bootnode,
        ):
            build_discovery_plan(
                _config(BOOTNODE="node.example", BOOTNODE_PORT=443),
                cert_fingerprint="aa",
                port=9000,
            )
        # default PEER_TIMEOUT 15 < 2 * refresh 30 => floored to 60
        assert bootnode.call_args.kwargs["peer_timeout"] == 60
        assert mdns.call_args.kwargs["peer_timeout"] == 60
        assert bootnode.call_args.kwargs["refresh_interval"] == 30

    def test_bootnode_mode_floor_follows_a_custom_refresh_interval(self):
        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery") as mdns,
            patch("hsync_client.p2p_discovery_bootnode.BootnodeDiscovery") as bootnode,
        ):
            build_discovery_plan(
                _config(BOOTNODE="node.example", BOOTNODE_REFRESH_INTERVAL=10),
                cert_fingerprint="aa",
                port=9000,
            )
        # 2 * 10 = 20 > default 15 => floored to 20
        assert bootnode.call_args.kwargs["peer_timeout"] == 20
        assert mdns.call_args.kwargs["peer_timeout"] == 20

    def test_bootnode_mode_preserves_an_explicitly_larger_timeout(self):
        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery") as mdns,
            patch("hsync_client.p2p_discovery_bootnode.BootnodeDiscovery") as bootnode,
        ):
            build_discovery_plan(
                _config(BOOTNODE="node.example", PEER_TIMEOUT=120),
                cert_fingerprint="aa",
                port=9000,
            )
        assert bootnode.call_args.kwargs["peer_timeout"] == 120
        assert mdns.call_args.kwargs["peer_timeout"] == 120

    def test_mdns_only_keeps_the_configured_timeout(self):
        """Without a bootnode the mDNS cadence governs; no floor is applied."""
        with patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery") as mdns:
            build_discovery_plan(_config(PEER_TIMEOUT=20), cert_fingerprint="aa", port=9000)
        assert mdns.call_args.kwargs["peer_timeout"] == 20


class TestHybridLifecycle:
    def test_start_shares_one_peer_manager_between_both_plans(self):
        hybrid, mdns, bootnode, shared = _hybrid()
        manager = hybrid.start()
        assert manager is shared
        assert hybrid.peer_manager is shared
        mdns.create_peer_manager.assert_called_once()
        mdns.start.assert_called_once_with(register_service=True, peer_manager=shared)
        bootnode.start.assert_called_once_with(register_service=True, peer_manager=shared)

    def test_stop_stops_both_plans(self):
        hybrid, mdns, bootnode, _ = _hybrid()
        hybrid.start()
        hybrid.stop()
        bootnode.stop.assert_called_once()
        mdns.stop.assert_called_once()

    def test_port_comes_from_the_mdns_child(self):
        hybrid, _, _, _ = _hybrid()
        assert hybrid.port == 9000


class TestHybridWait:
    def test_waits_on_the_shared_manager_and_reports_once(self):
        hybrid, mdns, bootnode, shared = _hybrid()
        hybrid.start()
        hybrid.wait_for_discovery(timeout=5.0)
        shared.wait_for_change.assert_called_once_with(5.0)
        bootnode.check_fatal.assert_called()
        # Presence is emitted exactly once, by the mDNS child; the bootnode
        # child contributes only the discovery log.
        mdns.emit_discovery_status.assert_called_once()
        bootnode.log_peer_count.assert_called_once()

    def test_bootnode_trust_failure_aborts_the_wait(self):
        hybrid, _, bootnode, _ = _hybrid()
        hybrid.start()
        bootnode.check_fatal.side_effect = DiscoveryFatalError("pin mismatch")
        with pytest.raises(DiscoveryFatalError):
            hybrid.wait_for_discovery(timeout=5.0)


class TestHybridFanOut:
    def test_endpoint_ready_reaches_both_plans(self):
        hybrid, mdns, bootnode, _ = _hybrid()
        hybrid.update_endpoint_ready(True)
        mdns.update_endpoint_ready.assert_called_once_with(True)
        bootnode.update_endpoint_ready.assert_called_once_with(True)

    def test_server_ready_reaches_both_plans(self):
        hybrid, mdns, bootnode, _ = _hybrid()
        hybrid.update_server_ready()
        mdns.update_server_ready.assert_called_once()
        bootnode.update_server_ready.assert_called_once()
        hybrid.update_server_not_ready()
        mdns.update_server_not_ready.assert_called_once()
        bootnode.update_server_not_ready.assert_called_once()

    def test_connected_clients_reach_both_plans(self):
        hybrid, mdns, bootnode, _ = _hybrid()
        hybrid.set_connected_clients(3)
        mdns.set_connected_clients.assert_called_once_with(3)
        bootnode.set_connected_clients.assert_called_once_with(3)
        provider = lambda: 1  # noqa: E731
        hybrid.set_connected_clients_provider(provider)
        mdns.set_connected_clients_provider.assert_called_once_with(provider)
        bootnode.set_connected_clients_provider.assert_called_once_with(provider)

    def test_endpoint_published_requires_every_source(self):
        hybrid, mdns, bootnode, _ = _hybrid()
        mdns.endpoint_published.return_value = True
        bootnode.endpoint_published.return_value = False
        assert hybrid.endpoint_published() is False
        bootnode.endpoint_published.return_value = True
        assert hybrid.endpoint_published() is True
