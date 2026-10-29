# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for MdnsRendezvous class in P2P module."""

import socket
import threading
import time
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from hsync_common.protocol.handshake import PROTOCOL_VERSION_P2P


class TestLANSync:
    """Tests for MdnsRendezvous class."""

    def test_p2p_sync_creation(self):
        """Test creating a MdnsRendezvous instance."""
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        assert lan.port > 0  # Random port assigned
        assert lan.group is None
        assert lan.use_https is False

    def test_p2p_sync_with_group(self):
        """Test MdnsRendezvous with group parameter."""
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos", group="team-a")
        assert lan.group == "team-a"

    def test_p2p_sync_with_https(self):
        """Test MdnsRendezvous with HTTPS enabled."""
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(
            cid="test123", bucket="photos", use_https=True, cert_fingerprint="abc123def456"
        )
        assert lan.use_https is True
        assert lan.cert_fingerprint == "abc123def456"

    def test_p2p_sync_with_prefer_master(self):
        """Test MdnsRendezvous with prefer_master."""
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(
            cid="test123",
            bucket="photos",
            prefer_master=True,
            sync_direction="bidirectional",
        )
        assert lan.peer_manager is None  # Not started yet

    def test_p2p_sync_multiple_stops(self):
        """Test that stopping multiple times is safe."""
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        lan.start(register_service=False)
        lan.stop()
        lan.stop()  # Should not raise

    def test_p2p_sync_stop_without_start(self):
        """Test stopping without starting is safe."""
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        lan.stop()  # Should not raise

    def test_wait_for_rendezvous_announces_waiting_once(self):
        """The "waiting" notice is logged once; later polls drop to debug."""
        from unittest.mock import patch

        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        lan._peer_manager = MagicMock()
        lan._peer_manager.get_alive_peers.return_value = []
        # Local node is master: presence never consults a remote master peer.
        lan._peer_manager.get_master_cid.return_value = "test123"
        lan._peer_manager.get_master.return_value = None

        with (
            patch("hsync_client.p2p_rendezvous_mdns.time.sleep"),
            patch("hsync_client.p2p_rendezvous_mdns.log") as mock_log,
        ):
            lan.wait_for_rendezvous(timeout=1)
            lan.wait_for_rendezvous(timeout=1)

        waiting_info = [
            c
            for c in mock_log.info.call_args_list
            if c.args and "for peer rendezvous" in str(c.args[0])
        ]
        waiting_debug = [
            c
            for c in mock_log.debug.call_args_list
            if c.args and "for peer rendezvous" in str(c.args[0])
        ]
        assert len(waiting_info) == 1, "waiting notice must log once at info level"
        assert len(waiting_debug) == 1, "subsequent polls must drop to debug"

    def test_wait_for_rendezvous_logs_result_only_on_change(self):
        """Rendezvous results are logged only when the peer count changes."""
        from unittest.mock import patch

        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        lan._peer_manager = MagicMock()

        peer = MagicMock()
        peer.cid = "peer-1"
        peer.address = "10.0.0.2"
        peer.port = 9000
        peer.connected = 0
        # Local node is master: presence never consults a remote master peer.
        lan._peer_manager.get_master_cid.return_value = "test123"
        lan._peer_manager.get_master.return_value = None
        # Alone for two polls, then a peer appears on the third.
        lan._peer_manager.get_alive_peers.side_effect = [[], [], [peer]]

        with (
            patch("hsync_client.p2p_rendezvous_mdns.time.sleep"),
            patch("hsync_client.p2p_rendezvous_mdns.log") as mock_log,
        ):
            lan.wait_for_rendezvous(timeout=1)
            lan.wait_for_rendezvous(timeout=1)
            lan.wait_for_rendezvous(timeout=1)

        infos = [str(c.args[0]) for c in mock_log.info.call_args_list if c.args]
        alone_msgs = [m for m in infos if "this client is alone" in m]
        discovered_msgs = [m for m in infos if "Discovered 1 peer(s)" in m]
        assert len(alone_msgs) == 1, "the 'alone' state must log once, not on every poll"
        assert len(discovered_msgs) == 1, "a new peer count must log once on change"

    def test_wait_for_rendezvous_connected_clients_logged_once(self):
        """A master with connected WS clients reports them instead of "alone"."""
        from unittest.mock import patch

        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        lan._peer_manager = MagicMock()
        lan._peer_manager.get_alive_peers.return_value = []
        lan._peer_manager.get_master_cid.return_value = "test123"
        lan._peer_manager.get_master.return_value = None
        lan.set_connected_clients_provider(lambda: 2)

        with (
            patch("hsync_client.p2p_rendezvous_mdns.time.sleep"),
            patch("hsync_client.p2p_rendezvous_mdns.log") as mock_log,
        ):
            lan.wait_for_rendezvous(timeout=1)
            lan.wait_for_rendezvous(timeout=1)

        infos = [str(c.args[0]) for c in mock_log.info.call_args_list if c.args]
        connected_msgs = [m for m in infos if "2 client(s) connected" in m]
        alone_msgs = [m for m in infos if "this client is alone" in m]
        assert len(connected_msgs) == 1, "the connected count must log once, not on every poll"
        assert len(alone_msgs) == 0, "a master with connected clients is not alone"

    def test_wait_for_rendezvous_waits_on_peer_change_event(self):
        """The rendezvous wait delegates to the peer manager's change event."""
        from unittest.mock import patch

        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        lan._peer_manager = MagicMock()
        lan._peer_manager.get_alive_peers.return_value = []
        lan._peer_manager.get_master_cid.return_value = "test123"
        lan._peer_manager.get_master.return_value = None

        with patch("hsync_client.p2p_rendezvous_mdns.time.sleep") as mock_sleep:
            lan.wait_for_rendezvous(timeout=7)

        lan._peer_manager.wait_for_change.assert_called_once_with(7)
        mock_sleep.assert_not_called()

    def test_connected_clients_coalesces_record_refresh(self):
        """A join/leave burst triggers one debounced mDNS re-registration.

        The record update must not flood the network with one re-registration
        per event: only the last change within the coalescing window refreshes
        the record.
        """
        from unittest.mock import patch

        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        lan._service_info = MagicMock()  # a serving registered master
        # __init__ already provides a real threading.Lock with no timer, which
        # is exactly the state this test needs. Re-instantiating the lock via
        # type(...)() is not portable: _thread.LockType is only constructible
        # from Python 3.13 onward (CPython GH-101102) and raises
        # "TypeError: cannot create '_thread.lock' instances" on 3.12.
        lan._refresh_timer = None

        # Cancel every timer the debounce schedules: the test simulates the
        # single firing that would happen after the last change.
        with patch("hsync_client.p2p_rendezvous_mdns.threading.Timer") as mock_timer_cls:
            mock_timer_cls.side_effect = lambda *a, **k: MagicMock(name="timer", **k)
            lan.set_connected_clients(1)
            lan.set_connected_clients(2)
            lan.set_connected_clients(1)
            assert mock_timer_cls.call_count == 3, "one cancelled timer per change"

        # Simulate the last timer's callback firing once.
        lan._refresh_timer = None
        with patch.object(lan, "_refresh_service") as mock_service:
            lan._refresh_after_coalesce()
            assert mock_service.call_count == 1, "exactly one record refresh"
        lan._refresh_timer = None

    def test_stop_cancels_pending_refresh(self):
        """stop() cancels a pending debounced refresh before touching Zeroconf."""
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        lan._service_info = MagicMock()
        lan._running = True

        lan.set_connected_clients(2)
        timer = lan._refresh_timer
        assert timer is not None

        timer.cancel()  # keep the pending timer from firing after the test
        with patch.object(lan, "_refresh_service") as mock_service:
            lan.stop()
            mock_service.assert_not_called()
        assert lan._refresh_timer is None


class TestGetFreePort:
    """Tests for the get_free_port helper."""

    def test_returns_a_bindable_port(self):
        from hsync_client.p2p_peers import get_free_port

        port = get_free_port()
        assert isinstance(port, int)
        assert 0 < port < 65536
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", port))  # must still be free


class TestStartStop:
    """Tests for MdnsRendezvous.start/stop with Zeroconf mocked out."""

    def _start(self, lan, **kwargs):
        with (
            patch("hsync_client.p2p_rendezvous_mdns.Zeroconf") as mock_zc_cls,
            patch("hsync_client.p2p_rendezvous_mdns.ServiceBrowser") as mock_browser_cls,
        ):
            pm = lan.start(**kwargs)
        return pm, mock_zc_cls, mock_browser_cls

    def test_start_without_registration(self):
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        pm, mock_zc_cls, mock_browser_cls = self._start(lan, register_service=False)
        try:
            assert pm is lan.peer_manager
            assert pm.local_cid == "test123"
            mock_zc_cls.assert_called_once()
            mock_browser_cls.assert_called_once()
            # No service registered yet.
            mock_zc_cls.return_value.register_service.assert_not_called()
            assert lan._service_info is None
            assert lan._running is True
        finally:
            lan.stop()

    def test_start_with_registration(self):
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        _pm, mock_zc_cls, _mb = self._start(lan, register_service=True)
        try:
            mock_zc_cls.return_value.register_service.assert_called_once()
            assert lan._service_info is not None
        finally:
            lan.stop()

    def test_stop_unregisters_and_closes(self):
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        with (
            patch("hsync_client.p2p_rendezvous_mdns.Zeroconf") as mock_zc_cls,
            patch("hsync_client.p2p_rendezvous_mdns.ServiceBrowser"),
        ):
            lan.start(register_service=True)
            lan.stop()
        zc = mock_zc_cls.return_value
        zc.unregister_service.assert_called_once()
        zc.close.assert_called_once()
        assert lan._running is False


class TestRegisterService:
    """Tests for the mDNS TXT record built by _register_service."""

    def _started_p2p(self, **kwargs):
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos", **kwargs)
        with (
            patch("hsync_client.p2p_rendezvous_mdns.Zeroconf") as mock_zc_cls,
            patch("hsync_client.p2p_rendezvous_mdns.ServiceBrowser"),
        ):
            lan.start(register_service=False)
        return lan, mock_zc_cls.return_value

    def _registered_properties(self, lan, zc) -> dict[bytes, bytes]:
        lan.register_service()
        info = zc.register_service.call_args.args[0]
        return dict(info.properties)

    def test_basic_properties(self):
        lan, zc = self._started_p2p()
        try:
            props = self._registered_properties(lan, zc)
            assert props[b"cid"] == b"test123"
            assert props[b"bucket"] == b"photos"
            assert props[b"protocol"] == PROTOCOL_VERSION_P2P.encode()
            assert props[b"sync_direction"] == b"bidirectional"
            assert b"startup_time" in props
            assert b"version" in props
            # The liveness heartbeat is always advertised (opaque counter).
            assert props[b"heartbeat"] == b"0"
            assert props[b"peer_download"] == b"true"
            # Conditional keys stay absent when the feature is off.
            for key in (
                b"group",
                b"https",
                b"cert_fp",
                b"server_ready",
                b"prefer_master",
                b"never_master",
            ):
                assert key not in props
        finally:
            lan.stop()

    def test_master_eligibility_withdrawn_and_restored(self):
        """A withdrawn candidacy flips the advertised election flags."""
        lan, zc = self._started_p2p(prefer_master=True)
        try:
            lan.register_service()
            props = dict(zc.register_service.call_args.args[0].properties)
            assert props[b"prefer_master"] == b"true"

            lan.set_master_eligibility(False)
            props = dict(zc.update_service.call_args.args[0].properties)
            assert b"prefer_master" not in props
            assert props[b"never_master"] == b"true"

            lan.set_master_eligibility(True)
            props = dict(zc.update_service.call_args.args[0].properties)
            assert props[b"prefer_master"] == b"true"
            assert b"never_master" not in props
        finally:
            lan.stop()

    def test_heartbeat_increments_and_reannounces(self):
        """The liveness counter increments and re-announces via update_service."""
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        with (
            patch("hsync_client.p2p_rendezvous_mdns.Zeroconf") as mock_zc_cls,
            patch("hsync_client.p2p_rendezvous_mdns.ServiceBrowser"),
        ):
            lan.start(register_service=True)
            zc = mock_zc_cls.return_value
            try:
                props = dict(zc.register_service.call_args.args[0].properties)
                assert props[b"heartbeat"] == b"0"
                lan._heartbeat()
                info = zc.update_service.call_args.args[0]
                assert dict(info.properties)[b"heartbeat"] == b"1"
            finally:
                lan.stop()

    def test_optional_properties(self):
        lan, zc = self._started_p2p(
            group="team-a",
            use_https=True,
            cert_fingerprint="ff00",
            prefer_master=True,
            sync_direction="upload-only",
        )
        try:
            props = self._registered_properties(lan, zc)
            assert props[b"group"] == b"team-a"
            # Endpoint material is dishonest until a listener has bound.
            assert b"https" not in props
            assert b"cert_fp" not in props
            assert props[b"prefer_master"] == b"true"
            assert props[b"sync_direction"] == b"upload-only"
            assert b"never_master" not in props
        finally:
            lan.stop()

    def test_never_master_property(self):
        lan, zc = self._started_p2p(never_master=True)
        try:
            props = self._registered_properties(lan, zc)
            assert props[b"never_master"] == b"true"
        finally:
            lan.stop()

    def test_compression_and_delta_flags(self, monkeypatch):
        from hsync_client import p2p_rendezvous_mdns as p2p_module

        monkeypatch.setattr(
            p2p_module,
            "CONFIG",
            {"COMPRESSION_ENABLED": True, "DELTA_ENABLED": True},
        )
        lan, zc = self._started_p2p()
        try:
            props = self._registered_properties(lan, zc)
            assert props[b"compression"] == b"true"
            assert props[b"delta"] == b"true"
        finally:
            lan.stop()

    def test_register_service_twice_is_idempotent(self):
        lan, zc = self._started_p2p()
        try:
            lan.register_service()
            lan.register_service()  # second call: log + return, no re-register
            assert zc.register_service.call_count == 1
        finally:
            lan.stop()

    def test_register_service_without_zeroconf_is_noop(self):
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        lan._register_service()  # _zeroconf is None: must not raise
        assert lan._service_info is None


class TestUpdateServerReady:
    """Tests for update_server_ready re-registration."""

    def test_update_without_registration_warns(self):
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        with patch("hsync_client.p2p_rendezvous_mdns.log") as mock_log:
            lan.update_server_ready()  # never started: warn, don't raise
        assert len(mock_log.warning.call_args_list) == 1

    def test_update_reregisters_with_server_ready(self):
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        with (
            patch("hsync_client.p2p_rendezvous_mdns.Zeroconf") as mock_zc_cls,
            patch("hsync_client.p2p_rendezvous_mdns.ServiceBrowser"),
        ):
            lan.start(register_service=True)
            zc = mock_zc_cls.return_value
            lan.update_server_ready()
            try:
                # In-place TXT update: no goodbye (unregister) must ever be sent
                # for a server_ready flip — slaves must see a value change, not
                # a spurious master removal.
                zc.unregister_service.assert_not_called()
                zc.update_service.assert_called_once()
                new_info = zc.update_service.call_args.args[0]
                assert dict(new_info.properties)[b"server_ready"] == b"true"
                # The local node marked itself incumbent (anti-takeover).
                assert lan.peer_manager._local_server_ready is True
            finally:
                lan.stop()

    def test_endpoint_ready_advertises_reachable_tls_endpoint_without_incumbency(self):
        """A bound slave seeder advertises TLS material but never server_ready.

        The read-only seeder is a *runtime* fact carried by the authenticated
        handshake (ADR-0030); the rendezvous record must advertise no seeder flag,
        only the endpoint material a requester needs to reach it.
        """
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(
            cid="test123",
            bucket="photos",
            use_https=True,
            cert_fingerprint="ff00",
        )
        with (
            patch("hsync_client.p2p_rendezvous_mdns.Zeroconf") as mock_zc_cls,
            patch("hsync_client.p2p_rendezvous_mdns.ServiceBrowser"),
        ):
            lan.start(register_service=True)
            zc = mock_zc_cls.return_value
            lan.update_endpoint_ready(True)
            try:
                props = dict(zc.update_service.call_args.args[0].properties)
                assert props[b"https"] == b"true"
                assert props[b"cert_fp"] == b"ff00"
                assert b"server_ready" not in props
                assert b"seed" not in props
                assert lan.peer_manager._local_server_ready is False

                # Withdrawing the endpoint withdraws the endpoint material.
                lan.update_endpoint_ready(False)
                props = dict(zc.update_service.call_args.args[0].properties)
                assert b"https" not in props
                assert b"cert_fp" not in props

                lan.update_server_ready()
                props = dict(zc.update_service.call_args.args[0].properties)
                assert props[b"server_ready"] == b"true"
                assert props[b"https"] == b"true"
            finally:
                lan.stop()

    def test_update_server_not_ready_clears_incumbency(self):
        """Yielding re-registers without server_ready and clears the local flag.

        Regression (ADR-0027): a yielded master that left ``server_ready=true``
        would remain a "zombie incumbent" — ranked top of the election while no
        longer serving — prolonging a split-brain.
        """
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        with (
            patch("hsync_client.p2p_rendezvous_mdns.Zeroconf") as mock_zc_cls,
            patch("hsync_client.p2p_rendezvous_mdns.ServiceBrowser"),
        ):
            lan.start(register_service=True)
            zc = mock_zc_cls.return_value
            lan.update_server_ready()  # become incumbent first
            lan.update_server_not_ready()  # then yield
            try:
                new_info = zc.update_service.call_args.args[0]
                props = dict(new_info.properties)
                assert b"server_ready" not in props
                assert b"connected" not in props
                assert lan.peer_manager._local_server_ready is False
            finally:
                lan.stop()


class TestGetLocalIp:
    """Tests for the shared get_local_ip fallbacks (net_utils)."""

    def test_returns_an_ip_string(self):
        from hsync_client.net_utils import get_local_ip

        ip = get_local_ip()
        assert isinstance(ip, str)
        socket.inet_aton(ip)  # must be a valid dotted-quad

    def test_connect_failure_falls_back_to_loopback(self):
        with patch("hsync_client.net_utils.socket.socket") as mock_socket:
            probe = mock_socket.return_value.__enter__.return_value
            probe.connect.side_effect = OSError("no route")
            from hsync_client.net_utils import get_local_ip

            assert get_local_ip() == "127.0.0.1"

    def test_socket_failure_falls_back_to_loopback(self):
        with patch("hsync_client.net_utils.socket.socket", side_effect=OSError("no sockets")):
            from hsync_client.net_utils import get_local_ip

            assert get_local_ip() == "127.0.0.1"


class TestRecordPublicationSerialization:
    """The heartbeat thread, the debounced refresh timer and the main thread
    all publish the mDNS record: their ``update_service`` calls must never
    interleave, or the published TXT state diverges from the local state."""

    def test_concurrent_heartbeat_and_refresh_are_serialized(self):
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        lan = MdnsRendezvous(cid="test123", bucket="photos")
        try:
            with (
                patch("hsync_client.p2p_rendezvous_mdns.Zeroconf") as mock_zc_cls,
                patch("hsync_client.p2p_rendezvous_mdns.ServiceBrowser"),
            ):
                lan.start(register_service=True)
                zc = mock_zc_cls.return_value

                intervals: list[tuple[float, float]] = []
                intervals_lock = threading.Lock()

                def slow_update(info, *args, **kwargs):
                    start = time.monotonic()
                    time.sleep(0.02)
                    with intervals_lock:
                        intervals.append((start, time.monotonic()))

                zc.update_service.side_effect = slow_update

                errors: list[Exception] = []

                def hammer_heartbeat():
                    try:
                        for _ in range(10):
                            lan._heartbeat()
                    except Exception as e:  # pragma: no cover - surfaced below
                        errors.append(e)

                def hammer_refresh():
                    try:
                        for _ in range(10):
                            lan._refresh_service(server_ready=True)
                    except Exception as e:  # pragma: no cover - surfaced below
                        errors.append(e)

                threads = [
                    threading.Thread(target=hammer_heartbeat),
                    threading.Thread(target=hammer_refresh),
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                assert not errors, f"worker threads raised: {errors!r}"
                assert len(intervals) == 20
                intervals.sort()
                for (_, prev_end), (start, _) in zip(intervals, intervals[1:], strict=False):
                    assert start >= prev_end, (
                        "update_service calls overlapped: record publication is not serialized"
                    )

                # The published record reflects a coherent final state.
                final = dict(lan._service_info.properties)
                assert final[b"server_ready"] == b"true"
                assert 1 <= int(final[b"heartbeat"]) <= 10
        finally:
            lan.stop()


class _FakeNetworkMonitor:
    """Records the subscribers so a test can raise a change by hand."""

    def __init__(self) -> None:
        self.subscribers: list = []

    def subscribe(self, callback) -> None:
        self.subscribers.append(callback)

    def unsubscribe(self, callback) -> None:
        if callback in self.subscribers:
            self.subscribers.remove(callback)

    def fire(self, signature) -> None:
        for callback in list(self.subscribers):
            callback(signature)


class TestNetworkChangeRecovery:
    """The mDNS record must follow the host's address across a network change."""

    def _started(self, monitor, stack):
        """Start a plan with Zeroconf/browser/monitor patched for the whole test."""
        from hsync_client.p2p_rendezvous_mdns import MdnsRendezvous

        instances: list[MagicMock] = []

        def make_zeroconf() -> MagicMock:
            instance = MagicMock()
            instances.append(instance)
            return instance

        stack.enter_context(
            patch("hsync_client.p2p_rendezvous_mdns.Zeroconf", side_effect=make_zeroconf)
        )
        browser_cls = stack.enter_context(patch("hsync_client.p2p_rendezvous_mdns.ServiceBrowser"))
        stack.enter_context(
            patch("hsync_client.p2p_rendezvous_mdns.get_network_monitor", return_value=monitor)
        )
        lan = MdnsRendezvous(cid="test123", bucket="photos")
        lan.start(register_service=True)
        return lan, instances, browser_cls

    def test_address_change_rebuilds_the_stack_and_reannounces(self):
        """A DHCP reassignment replaces the instance and the advertised address."""
        from hsync_client.net_watch import NetworkSignature

        monitor = _FakeNetworkMonitor()
        with ExitStack() as stack:
            lan, instances, browser_cls = self._started(monitor, stack)
            try:
                assert len(instances) == 1
                assert browser_cls.call_count == 1

                monitor.fire(NetworkSignature(source_ip="10.9.8.7", online=True))

                # The stale instance is retired and a fresh one advertises the
                # address the host actually owns now.
                assert len(instances) == 2
                instances[0].unregister_service.assert_called_once()
                instances[0].close.assert_called_once()
                assert browser_cls.call_count == 2
                info = instances[1].register_service.call_args.args[0]
                assert info.addresses == [socket.inet_aton("10.9.8.7")]
                assert lan._local_ip == "10.9.8.7"
                assert lan.peer_manager is not None
                assert lan.peer_manager._local_address == "10.9.8.7"
            finally:
                lan.stop()

    def test_going_offline_keeps_the_last_record(self):
        """Nothing can be announced while the link is down: no rebuild happens."""
        from hsync_client.net_watch import NetworkSignature

        monitor = _FakeNetworkMonitor()
        with ExitStack() as stack:
            lan, instances, _browser = self._started(monitor, stack)
            previous_ip = lan._local_ip
            try:
                monitor.fire(NetworkSignature(source_ip="127.0.0.1", online=False))
                assert len(instances) == 1
                assert lan._local_ip == previous_ip
                assert lan._network_offline is True
            finally:
                lan.stop()

    def test_failed_heartbeat_reconciles_the_sockets(self):
        """A failed re-announce repairs the per-interface sockets."""
        monitor = _FakeNetworkMonitor()
        with ExitStack() as stack:
            lan, instances, _browser = self._started(monitor, stack)
            try:
                lan._recover_mdns()
                instances[0].update_interfaces.assert_called_once()
                assert len(instances) == 1
            finally:
                lan.stop()

    def test_a_failed_reconcile_escalates_to_a_rebuild(self):
        """An unrecoverable reconcile rebuilds instead of staying silent."""
        from hsync_client.net_watch import NetworkSignature

        monitor = _FakeNetworkMonitor()
        with ExitStack() as stack:
            lan, instances, _browser = self._started(monitor, stack)
            try:
                instances[0].update_interfaces.side_effect = OSError("no such device")
                stack.enter_context(
                    patch(
                        "hsync_client.p2p_rendezvous_mdns.network_signature",
                        return_value=NetworkSignature(source_ip="10.9.8.7", online=True),
                    )
                )
                lan._recover_mdns()
                assert len(instances) == 2
                assert lan._local_ip == "10.9.8.7"
            finally:
                lan.stop()

    def test_stop_unsubscribes_from_the_monitor(self):
        """A torn-down plan must not be woken by later network changes."""
        monitor = _FakeNetworkMonitor()
        with ExitStack() as stack:
            lan, instances, _browser = self._started(monitor, stack)
            assert len(monitor.subscribers) == 1
            lan.stop()
            assert monitor.subscribers == []
            assert len(instances) == 1
