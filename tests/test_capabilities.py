# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Capability advertisement: one token per participation statement."""

from __future__ import annotations

from hsync_common.protocol.handshake import Capability


def test_the_inventory_protocol_is_advertised_without_publishing(monkeypatch):
    """Speaking the protocol and publishing an inventory are two statements."""
    from hsync_client import capabilities
    from hsync_client.config import CONFIG

    monkeypatch.setitem(CONFIG, "P2P_PUBLISH_INVENTORY", False)
    monkeypatch.setitem(CONFIG, "P2P_SERVE_BLOCKS", False)
    caps = capabilities.local_capabilities()

    assert Capability.index in caps
    assert Capability.publishes_index not in caps
    assert Capability.peer_download not in caps


def test_each_participation_role_advertises_itself(monkeypatch):
    from hsync_client import capabilities
    from hsync_client.config import CONFIG

    monkeypatch.setitem(CONFIG, "P2P_PUBLISH_INVENTORY", True)
    monkeypatch.setitem(CONFIG, "P2P_SERVE_BLOCKS", False)
    caps = capabilities.local_capabilities()

    assert Capability.publishes_index in caps
    assert Capability.peer_download not in caps

    monkeypatch.setitem(CONFIG, "P2P_SERVE_BLOCKS", True)
    monkeypatch.setitem(CONFIG, "P2P_PUBLISH_INVENTORY", False)
    caps = capabilities.local_capabilities()

    assert Capability.peer_download in caps
    assert Capability.publishes_index not in caps


def test_readonly_permissions_is_advertised_by_a_lossy_host(monkeypatch):
    from hsync_client import capabilities

    monkeypatch.setattr(capabilities, "local_permissions_are_lossy", lambda: True)
    assert Capability.readonly_permissions in capabilities.local_capabilities()


def test_readonly_permissions_is_not_advertised_by_a_posix_host(monkeypatch):
    from hsync_client import capabilities

    monkeypatch.setattr(capabilities, "local_permissions_are_lossy", lambda: False)
    assert Capability.readonly_permissions not in capabilities.local_capabilities()


def test_adopt_remote_capabilities_records_the_permission_model():
    from hsync_client import capabilities
    from hsync_client.config import CONFIG

    capabilities.adopt_remote_capabilities({Capability.readonly_permissions})
    assert CONFIG["REMOTE_READONLY_PERMISSIONS"] is True

    capabilities.adopt_remote_capabilities(set())
    assert CONFIG["REMOTE_READONLY_PERMISSIONS"] is False
