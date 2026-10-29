"""Fan-out over several discovery servers (``MultiBootnodeDiscovery``).

The per-server lease is covered by ``test_p2p_discovery_bootnode``; here the
composition contract is pinned: one shared ``PeerManager``, readiness that does
not depend on a dead server, per-source failure isolation, and the composite
relay wiring (the relay is per node, so a dialer must try them and an acceptor
must register on all of them).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hsync_client.boot_relay import FailoverRelayDialer, GroupRelayAcceptor
from hsync_client.p2p_discovery_bootnode import MultiBootnodeDiscovery, SharedUpnpMapping


def _source(**overrides) -> MagicMock:
    """A lease source double with the surface the composite drives."""
    source = MagicMock(name="discovery_source")
    source.port = 8443
    source.start.return_value = source.create_peer_manager.return_value
    source.endpoint_published.return_value = True
    source.source_disabled.return_value = False
    source.relay_dialer.return_value = None
    source.relay_acceptor.return_value = None
    for name, value in overrides.items():
        setattr(source, name, value)
    return source


class TestComposition:
    def test_an_empty_source_list_is_refused(self):
        with pytest.raises(ValueError):
            MultiBootnodeDiscovery([])

    def test_start_shares_one_peer_manager_across_every_source(self):
        first, second = _source(), _source()
        plan = MultiBootnodeDiscovery([first, second])

        manager = plan.start(register_service=True)

        assert manager is first.create_peer_manager.return_value
        for source in (first, second):
            source.start.assert_called_once_with(register_service=True, peer_manager=manager)
        assert second.create_peer_manager.call_count == 0
        assert plan.peer_manager is manager

    def test_stop_stops_every_source_and_drops_the_shared_mapping(self):
        first, second = _source(), _source()
        shared = MagicMock(name="shared_upnp")
        plan = MultiBootnodeDiscovery([first, second], shared_upnp=shared)

        plan.stop()

        first.stop.assert_called_once_with()
        second.stop.assert_called_once_with()
        shared.unmap.assert_called_once_with()

    def test_the_serving_port_comes_from_the_first_source(self):
        first, second = _source(), _source()
        second.port = 9999

        assert MultiBootnodeDiscovery([first, second]).port == 8443


class TestReadiness:
    def test_one_published_source_is_enough(self):
        published, pending = _source(), _source()
        pending.endpoint_published.return_value = False

        plan = MultiBootnodeDiscovery([published, pending])

        assert plan.endpoint_published() is True

    def test_a_disabled_source_is_skipped(self):
        disabled, pending = _source(), _source()
        disabled.source_disabled.return_value = True
        disabled.endpoint_published.return_value = False
        pending.endpoint_published.return_value = True

        plan = MultiBootnodeDiscovery([disabled, pending])

        assert plan.endpoint_published() is True

    def test_every_source_disabled_means_the_plan_is_disabled(self):
        first, second = _source(), _source()
        first.source_disabled.return_value = True
        second.source_disabled.return_value = False

        plan = MultiBootnodeDiscovery([first, second])
        assert plan.source_disabled() is False

        second.source_disabled.return_value = True
        assert plan.source_disabled() is True
        assert plan.endpoint_published() is False


class TestBroadcast:
    def test_lifecycle_and_publication_calls_reach_every_source(self):
        first, second = _source(), _source()
        plan = MultiBootnodeDiscovery([first, second])

        plan.update_endpoint_ready(True)
        plan.update_server_ready()
        plan.update_server_not_ready()
        plan.set_connected_clients(3)
        plan.set_master_eligibility(False)
        plan.refresh_election_advertisement()
        plan.set_connected_clients_provider(None)

        for source in (first, second):
            source.update_endpoint_ready.assert_called_once_with(True)
            source.update_server_ready.assert_called_once_with()
            source.update_server_not_ready.assert_called_once_with()
            source.set_connected_clients.assert_called_once_with(3)
            source.set_master_eligibility.assert_called_once_with(False)
            source.refresh_election_advertisement.assert_called_once_with()
            source.set_connected_clients_provider.assert_called_once_with(None)

    def test_the_peer_count_is_logged_once_not_once_per_source(self):
        first, second = _source(), _source()

        MultiBootnodeDiscovery([first, second]).log_peer_count()

        first.log_peer_count.assert_called_once_with()
        second.log_peer_count.assert_not_called()


class TestRelay:
    def test_the_dialer_tries_each_server_in_order(self):
        refused, accepted = _source(), _source()
        refused_dialer = MagicMock(name="refused_dialer")
        refused_dialer.open.return_value = None
        accepted_dialer = MagicMock(name="accepted_dialer")
        accepted_dialer.open.return_value = ("127.0.0.1", 5555)
        refused.relay_dialer.return_value = refused_dialer
        accepted.relay_dialer.return_value = accepted_dialer

        dialer = MultiBootnodeDiscovery([refused, accepted]).relay_dialer("peer-cid")

        assert isinstance(dialer, FailoverRelayDialer)
        assert dialer.open() == ("127.0.0.1", 5555)
        refused_dialer.open.assert_called_once()
        accepted_dialer.open.assert_called_once()
        dialer.close()
        refused_dialer.close.assert_called_once()
        accepted_dialer.close.assert_called_once()

    def test_no_source_supporting_the_relay_yields_none(self):
        assert MultiBootnodeDiscovery([_source(), _source()]).relay_dialer("cid") is None
        assert MultiBootnodeDiscovery([_source()]).relay_acceptor(8443) is None

    def test_the_acceptor_registers_on_every_server(self):
        first, second = _source(), _source()
        first.relay_acceptor.return_value = MagicMock(name="acceptor_a")
        second.relay_acceptor.return_value = MagicMock(name="acceptor_b")

        acceptor = MultiBootnodeDiscovery([first, second]).relay_acceptor(8443)

        assert isinstance(acceptor, GroupRelayAcceptor)
        assert acceptor.start() is True
        first.relay_acceptor.return_value.start.assert_called_once()
        second.relay_acceptor.return_value.start.assert_called_once()
        acceptor.stop()
        first.relay_acceptor.return_value.stop.assert_called_once()
        second.relay_acceptor.return_value.stop.assert_called_once()


class TestSharedUpnpMapping:
    def test_ports_are_mapped_once_for_every_publisher(self):
        mapper = MagicMock(name="upnp_mapper")
        mapper.map_port.return_value = 40001
        shared = SharedUpnpMapping(lambda: mapper)

        assert shared.external_port(8443) == 40001
        assert shared.external_port(8443) == 40001
        mapper.map_port.assert_called_once_with(8443)

    def test_unmap_forgets_the_value_so_the_next_publish_remaps(self):
        mapper = MagicMock(name="upnp_mapper")
        mapper.map_port.side_effect = [40001, 40002]
        shared = SharedUpnpMapping(lambda: mapper)
        assert shared.external_port(8443) == 40001

        shared.unmap()
        mapper.unmap.assert_called_once_with()
        assert shared.external_port(8443) == 40002

    def test_a_failed_teardown_is_swallowed(self):
        mapper = MagicMock(name="upnp_mapper")
        mapper.map_port.return_value = 40001
        mapper.unmap.side_effect = OSError("router gone")
        shared = SharedUpnpMapping(lambda: mapper)
        shared.external_port(8443)

        shared.unmap()  # must not raise: the teardown is best-effort

        mapper.unmap.assert_called_once_with()
        # The mapping was forgotten, so the next publish maps the live port again.
        assert shared.external_port(8443) == 40001
        assert mapper.map_port.call_count == 2
