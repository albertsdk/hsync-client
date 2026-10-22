"""
Tests for hsync_client.main_logic module - P2P mode functionality.

Contains TestRunLanMode class.
"""

from typing import cast
from unittest.mock import MagicMock, patch

from hsync_client.config import HsyncConfig
from hsync_client.events import UiEventName
from hsync_client.identity import DeviceIdentity
from hsync_client.p2p_handler import run_p2p_mode


def _make_p2p_config(tmp_path: object, **overrides: object) -> HsyncConfig:
    """Return a minimal config dict suitable for run_p2p_mode."""
    defaults: dict[str, object] = {
        "CID": "test-cid",
        "REMOTE_BUCKET": "test-bucket",
        "P2P_LISTEN_PORT": 9000,
        "P2P_GROUP": None,
        "P2P_PREFER_MASTER": False,
        "P2P_NEVER_MASTER": False,
        "SYNC_DIRECTION": "bidirectional",
        "LOCAL_BUCKET": str(tmp_path),
        "P2P_KEY": None,
        "DISCOVERY_TIMEOUT": 1,
        "DRYRUN": False,
        "ONE_SHOT": True,
        "REQUEST_HEADERS": {"X-Client-Id": "test-cid"},
        "API": {"buckets": "", "events": ""},
        "SERVER_COMPRESSION_ENABLED": False,
        "HTTP_TIMEOUT": 30,
        "MASTER_CERT_FINGERPRINT": None,
    }
    defaults.update(overrides)
    return cast(HsyncConfig, defaults)


class TestRunLanMode:
    """Tests for run_p2p_mode function."""

    def test_no_peers_one_shot_exits(self, tmp_path):
        """One-shot mode with no peers exits with warning."""
        config = _make_p2p_config(tmp_path)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = []
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = None

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000

        with patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync):
            run_p2p_mode(config, mock_log)

        assert any("alone" in str(c) for c in mock_log.warning.call_args_list)
        assert any("Discovery: mDNS" in str(c) for c in mock_log.info.call_args_list)
        mock_p2p_sync.stop.assert_called_once()

    def test_discovery_event_reports_the_active_plan(self, tmp_path):
        """The plan is surfaced on the structured-events channel at startup."""
        config = _make_p2p_config(tmp_path, BOOTNODE="boot.example.net", BOOTNODE_PORT=443)
        mock_log = MagicMock()
        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = []
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = None
        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000
        mock_p2p_sync.endpoint_published.return_value = True

        with (
            patch(
                "hsync_client.p2p_discovery_bootnode.BootnodeDiscovery",
                return_value=mock_p2p_sync,
            ),
            patch(
                "hsync_client.p2p_discovery_hybrid.HybridDiscovery",
                return_value=mock_p2p_sync,
            ),
            patch("hsync_client.p2p_handler.emit_event") as emit,
        ):
            run_p2p_mode(config, mock_log)

        discovery_calls = [
            call
            for call in emit.call_args_list
            if call.args and call.args[0] == UiEventName.discovery
        ]
        assert len(discovery_calls) == 1
        assert discovery_calls[0].kwargs == {
            "plan": "mdns+bootnode",
            "bootnode": "boot.example.net",
        }

    def test_bootnode_banner_and_plan_selection(self, tmp_path):
        """A configured bootnode selects the combined mDNS + bootnode plan."""
        config = _make_p2p_config(tmp_path, BOOTNODE="boot.example.net", BOOTNODE_PORT=443)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = []
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = None

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000
        mock_p2p_sync.endpoint_published.return_value = True

        with (
            patch(
                "hsync_client.p2p_discovery_bootnode.BootnodeDiscovery",
                return_value=mock_p2p_sync,
            ) as mock_plan,
            patch(
                "hsync_client.p2p_discovery_hybrid.HybridDiscovery",
                return_value=mock_p2p_sync,
            ),
        ):
            run_p2p_mode(config, mock_log)

        mock_plan.assert_called_once()
        kwargs = mock_plan.call_args.kwargs
        assert kwargs["bootnode_host"] == "boot.example.net"
        assert kwargs["bootnode_port"] == 443
        assert any(
            "Discovery: mDNS on the local network + bootnode boot.example.net:443" in str(c)
            for c in mock_log.info.call_args_list
        )

    def test_master_starts_servers_non_persistent(self, tmp_path):
        """Master starts HTTP and WebSocket servers."""
        config = _make_p2p_config(tmp_path)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [MagicMock()]
        # First call: is_master -> True, then sleep raises to break loop
        mock_peer_mgr.is_master.return_value = True

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000

        mock_p2p_server = MagicMock()
        mock_ws_server = MagicMock()
        # The unified design gates the watcher start on the control plane not
        # already running; a bare MagicMock would look "running" and skip it.
        mock_ws_server.running = False
        mock_ws_server.connected_clients = 0

        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync),
            patch("hsync_client.p2p_handler.P2PServer", return_value=mock_p2p_server),
            patch("hsync_client.p2p_handler.P2PWebSocketServer", return_value=mock_ws_server),
            patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            run_p2p_mode(config, mock_log)

        mock_p2p_server.start.assert_called_once()
        mock_ws_server.start.assert_called_once_with(watch_path=str(tmp_path))
        mock_p2p_sync.update_server_ready.assert_called_once()

    def test_slave_non_persistent_sync(self, tmp_path):
        """Client connects to master and runs sync in non-persistent mode."""
        config = _make_p2p_config(tmp_path)
        mock_log = MagicMock()

        master_peer = MagicMock()
        master_peer.cid = "master-cid"
        master_peer.address = "192.168.1.1"
        master_peer.port = 9000
        master_peer.use_https = True
        master_peer.cert_fingerprint = "abcdef1234567890" * 4
        master_peer.compression_enabled = True
        master_peer.server_ready = True
        master_peer.connected = 0  # presence arithmetic reads an int
        master_peer.get_server_url.return_value = "https://192.168.1.1:9000"

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [master_peer]
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = master_peer

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000

        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync),
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("asyncio.run") as mock_run,
        ):
            run_p2p_mode(config, mock_log)

        mock_run.assert_called_once()
        assert any("master" in str(c).lower() for c in mock_log.info.call_args_list)

    def test_slave_insecure_master_refuses(self, tmp_path):
        """Client refuses a master that does not serve HTTPS with a fingerprint."""
        # Secure-only P2P: a plain-HTTP master is rejected, no sync.
        config = _make_p2p_config(tmp_path)
        mock_log = MagicMock()

        master_peer = MagicMock()
        master_peer.cid = "master-cid"
        master_peer.address = "192.168.1.1"
        master_peer.port = 9000
        master_peer.use_https = False
        master_peer.cert_fingerprint = None
        master_peer.server_ready = True

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [master_peer]
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = master_peer

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000

        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync),
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("asyncio.run") as mock_run,
        ):
            run_p2p_mode(config, mock_log)

        # No sync attempted and an insecure-master warning was emitted.
        mock_run.assert_not_called()
        assert any("INSECURE MASTER" in str(c) for c in mock_log.warning.call_args_list)

    def test_slave_dryrun_shows_preview(self, tmp_path):
        """Client in dryrun mode shows preview and exits."""
        config = _make_p2p_config(tmp_path, DRYRUN=True)
        mock_log = MagicMock()

        master_peer = MagicMock()
        master_peer.cid = "master-cid"
        master_peer.server_ready = True
        master_peer.use_https = True
        master_peer.cert_fingerprint = "abcdef1234567890" * 4
        master_peer.compression_enabled = False
        master_peer.get_server_url.return_value = "https://192.168.1.1:9000"

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [master_peer]
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = master_peer

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000

        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync),
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler._master_endpoint_reachable", return_value=True),
            patch("hsync_client.p2p_handler._run_preview") as mock_preview,
        ):
            run_p2p_mode(config, mock_log)

        mock_preview.assert_called_once_with(mock_log)

    def test_slave_dryrun_skips_the_preview_for_an_unreachable_master(self, tmp_path):
        """A dead master's stale record must not produce a doomed preview."""
        config = _make_p2p_config(tmp_path, DRYRUN=True)
        mock_log = MagicMock()

        master_peer = MagicMock()
        master_peer.cid = "master-cid"
        master_peer.server_ready = True
        master_peer.use_https = True
        master_peer.cert_fingerprint = "abcdef1234567890" * 4
        master_peer.compression_enabled = False
        master_peer.get_server_url.return_value = "https://192.168.1.1:9000"

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [master_peer]
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = master_peer

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000

        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync),
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler._master_endpoint_reachable", return_value=False),
            patch("hsync_client.p2p_handler._run_preview") as mock_preview,
        ):
            run_p2p_mode(config, mock_log)

        mock_preview.assert_not_called()
        mock_peer_mgr.remove_peer.assert_called_once_with("master-cid")

    def test_keyboard_interrupt_cleans_up(self, tmp_path):
        """KeyboardInterrupt stops all servers and p2p_sync."""
        config = _make_p2p_config(tmp_path)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = []
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = None

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000
        # Make wait_for_discovery raise to simulate Ctrl+C
        mock_p2p_sync.wait_for_discovery.side_effect = KeyboardInterrupt

        with patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync):
            run_p2p_mode(config, mock_log)

        mock_p2p_sync.stop.assert_called_once()
        assert any("Shutting down" in str(c) for c in mock_log.info.call_args_list)

    def test_loads_persistent_master_certificate(self, tmp_path):
        """P2P mode always loads the persistent master certificate."""
        config = _make_p2p_config(tmp_path)
        mock_log = MagicMock()

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = []
        mock_peer_mgr.is_master.return_value = False
        mock_peer_mgr.get_master.return_value = None

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000

        mock_cert_info = MagicMock()
        mock_cert_info.fingerprint = "abcdef1234567890" * 4

        listener_server = MagicMock()
        listener_server.port = 9000
        listener_ws = MagicMock()
        listener_ws.running = False
        listener_server.websocket_server = listener_ws

        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync),
            patch("hsync_client.p2p_handler.P2PServer", return_value=listener_server),
            patch("hsync_client.p2p_handler.P2PWebSocketServer", return_value=listener_ws),
            patch(
                "hsync_client.identity.load_or_create_device_identity",
                return_value=DeviceIdentity(
                    device_id="TestDevice1234567890ab", cert_info=mock_cert_info
                ),
            ) as mock_load,
        ):
            run_p2p_mode(config, mock_log)

        mock_load.assert_called_once()

    def test_slave_waits_for_master_server_ready(self, tmp_path):
        """Client re-enters discovery loop when master not ready."""
        config = _make_p2p_config(tmp_path)
        mock_log = MagicMock()

        master_not_ready = MagicMock()
        master_not_ready.cid = "master-cid"
        master_not_ready.server_ready = False

        master_ready = MagicMock()
        master_ready.cid = "master-cid"
        master_ready.server_ready = True
        master_ready.use_https = True
        master_ready.cert_fingerprint = "abcdef1234567890" * 4
        master_ready.compression_enabled = False
        master_ready.connected = 0  # presence arithmetic reads an int
        master_ready.get_server_url.return_value = "https://192.168.1.1:9000"

        mock_peer_mgr = MagicMock()
        mock_peer_mgr.get_alive_peers.return_value = [MagicMock()]
        mock_peer_mgr.is_master.return_value = False
        # First call returns not-ready master, then the ready master; the
        # presence refresh during the initial sync asks for it again.
        mock_peer_mgr.get_master.side_effect = [
            master_not_ready,
            master_ready,
            master_ready,
        ]

        mock_p2p_sync = MagicMock()
        mock_p2p_sync.start.return_value = mock_peer_mgr
        mock_p2p_sync.port = 9000

        with (
            patch("hsync_client.p2p_discovery_mdns.MdnsDiscovery", return_value=mock_p2p_sync),
            patch("hsync_client.p2p_handler.set_config"),
            patch("hsync_client.p2p_handler._evaluate_master_pin", return_value=True),
            patch("hsync_client.p2p_handler.sync", new=MagicMock()),
            patch("asyncio.run"),
        ):
            run_p2p_mode(config, mock_log)

        # wait_for_discovery should be called twice (first loop: not ready, second: ready)
        assert mock_p2p_sync.wait_for_discovery.call_count == 2
