# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for HsyncServiceListener (mDNS discovery) in the LAN module."""

from __future__ import annotations

from unittest.mock import MagicMock

from hsync_common.protocol.handshake import PROTOCOL_VERSION_LAN

from hsync_client.lan import HsyncServiceListener, PeerManager

_TYPE = "_hsync._tcp.local."


def _make_info(properties: dict[bytes, bytes], *, port: int | None = 9753) -> MagicMock:
    """Build a minimal ServiceInfo stand-in with the given TXT properties."""
    info = MagicMock()
    info.name = f"photos-{'deadbeef'}.{_TYPE}"
    info.properties = properties
    info.parsed_addresses.return_value = ["192.168.1.50"]
    info.port = port
    return info


def _full_props(overrides: dict[bytes, bytes] | None = None) -> dict[bytes, bytes]:
    props: dict[bytes, bytes] = {
        b"cid": b"peer-cid-1",
        b"bucket": b"photos",
        b"protocol": PROTOCOL_VERSION_LAN.encode(),
        b"version": b"1.2.3",
        b"group": b"team-a",
        b"https": b"true",
        b"cert_fp": b"ab" * 32,
        b"compression": b"true",
        b"delta": b"true",
        b"peer_download": b"true",
        b"seed": b"true",
        b"server_ready": b"true",
        b"force_master": b"true",
        b"never_master": b"false",
        b"startup_time": b"1234.5",
        b"sync_direction": b"upload-only",
    }
    if overrides:
        props.update(overrides)
    return props


def _listener_with_pm(
    goodbye_grace_s: float = 0.0, **pm_kwargs
) -> tuple[HsyncServiceListener, PeerManager]:
    pm = PeerManager(local_cid="local-cid", bucket="photos", **pm_kwargs)
    return HsyncServiceListener(pm, goodbye_grace_s=goodbye_grace_s), pm


class TestRegisterPeerFromInfo:
    def test_full_properties_parsed(self):
        listener, pm = _listener_with_pm(group="team-a")
        listener._register_peer_from_info(_make_info(_full_props()))
        peer = pm._peers.get("peer-cid-1")
        assert peer is not None
        assert peer.address == "192.168.1.50"
        assert peer.port == 9753
        assert peer.version == "1.2.3"
        assert peer.group == "team-a"
        assert peer.use_https is True
        assert peer.cert_fingerprint == "ab" * 32
        assert peer.compression_enabled is True
        assert peer.delta_enabled is True
        assert peer.peer_download_enabled is True
        assert peer.seed_enabled is True
        assert peer.server_ready is True
        assert peer.force_master is True
        assert peer.never_master is False
        assert peer.startup_time == 1234.5
        assert peer.sync_direction == "upload-only"

    def test_minimal_properties_use_defaults(self):
        listener, pm = _listener_with_pm()
        props = {
            b"cid": b"peer-cid-1",
            b"bucket": b"photos",
            b"protocol": PROTOCOL_VERSION_LAN.encode(),
        }
        listener._register_peer_from_info(_make_info(props))
        peer = pm._peers.get("peer-cid-1")
        assert peer is not None
        assert peer.version == "0.0.0"
        assert peer.group is None
        assert peer.use_https is False
        assert peer.cert_fingerprint is None
        assert peer.compression_enabled is False
        assert peer.delta_enabled is False
        assert peer.peer_download_enabled is False
        assert peer.seed_enabled is False
        assert peer.server_ready is False
        assert peer.force_master is False
        assert peer.never_master is False
        assert peer.startup_time == 0.0
        assert peer.sync_direction == "bidirectional"

    def test_missing_cid_is_ignored(self):
        listener, pm = _listener_with_pm(group="team-a")
        props = _full_props({b"cid": b""})
        listener._register_peer_from_info(_make_info(props))
        assert pm._peers == {}

    def test_missing_bucket_is_ignored(self):
        listener, pm = _listener_with_pm(group="team-a")
        props = _full_props({b"bucket": b""})
        listener._register_peer_from_info(_make_info(props))
        assert pm._peers == {}

    def test_wrong_protocol_is_ignored(self):
        listener, pm = _listener_with_pm(group="team-a")
        props = _full_props({b"protocol": b"hsync-lan/0"})
        listener._register_peer_from_info(_make_info(props))
        assert pm._peers == {}

    def test_missing_protocol_is_ignored(self):
        listener, pm = _listener_with_pm(group="team-a")
        props = _full_props()
        del props[b"protocol"]
        listener._register_peer_from_info(_make_info(props))
        assert pm._peers == {}

    def test_no_addresses_is_ignored(self):
        listener, pm = _listener_with_pm(group="team-a")
        info = _make_info(_full_props())
        info.parsed_addresses.return_value = []
        listener._register_peer_from_info(info)
        assert pm._peers == {}

    def test_no_port_is_ignored(self):
        listener, pm = _listener_with_pm(group="team-a")
        listener._register_peer_from_info(_make_info(_full_props(), port=None))
        assert pm._peers == {}

    def test_malformed_info_does_not_raise(self):
        """A broken ServiceInfo must be logged and swallowed, never crash."""
        listener, pm = _listener_with_pm()
        info = MagicMock()
        type(info).properties = property(lambda _s: (_ for _ in ()).throw(RuntimeError("boom")))
        listener._register_peer_from_info(info)  # must not raise
        assert pm._peers == {}


class TestServiceCallbacks:
    def test_add_service_registers_peer(self):
        listener, pm = _listener_with_pm(group="team-a")
        zc = MagicMock()
        zc.get_service_info.return_value = _make_info(_full_props())
        listener.add_service(zc, _TYPE, "photos-peer-cid-1." + _TYPE)
        assert "peer-cid-1" in pm._peers

    def test_add_service_without_info_is_noop(self):
        listener, pm = _listener_with_pm(group="team-a")
        zc = MagicMock()
        zc.get_service_info.return_value = None
        listener.add_service(zc, _TYPE, "photos-peer-cid-1." + _TYPE)
        assert pm._peers == {}

    def test_update_service_registers_peer(self):
        listener, pm = _listener_with_pm(group="team-a")
        zc = MagicMock()
        zc.get_service_info.return_value = _make_info(_full_props())
        listener.update_service(zc, _TYPE, "photos-peer-cid-1." + _TYPE)
        assert "peer-cid-1" in pm._peers

    def test_update_service_without_info_is_noop(self):
        listener, pm = _listener_with_pm(group="team-a")
        zc = MagicMock()
        zc.get_service_info.return_value = None
        listener.update_service(zc, _TYPE, "photos-peer-cid-1." + _TYPE)
        assert pm._peers == {}

    def test_remove_service_removes_peer(self):
        listener, pm = _listener_with_pm(group="team-a")
        # The removal parser takes the substring after the *last* dash of the
        # service name, so use a dash-less CID here.
        listener._register_peer_from_info(_make_info(_full_props({b"cid": b"peercid1"})))
        assert "peercid1" in pm._peers
        listener.remove_service(MagicMock(), _TYPE, "photos-peercid1." + _TYPE)
        assert "peercid1" not in pm._peers

    def test_remove_service_malformed_name_is_noop(self):
        listener, pm = _listener_with_pm(group="team-a")
        listener._register_peer_from_info(_make_info(_full_props()))
        # No dash in the service name: nothing to parse, nothing to remove.
        listener.remove_service(MagicMock(), _TYPE, "photos." + _TYPE)
        assert "peer-cid-1" in pm._peers

    def test_remove_service_error_is_swallowed(self):
        """Errors during peer removal must be logged, not propagated."""
        from unittest.mock import patch

        listener, _pm = _listener_with_pm(goodbye_grace_s=0)
        listener._peer_manager = MagicMock()
        listener._peer_manager.is_wss_connected.return_value = False
        listener._peer_manager.remove_peer.side_effect = RuntimeError("boom")

        with patch("hsync_client.lan.log") as mock_log:
            # Must not raise
            listener.remove_service(MagicMock(), _TYPE, "photos-peer-cid-1." + _TYPE)

        # The error must be logged at debug level (the only log call in the except handler)
        assert mock_log.debug.called
        # Verify remove_peer was attempted before the exception
        listener._peer_manager.remove_peer.assert_called_once()
