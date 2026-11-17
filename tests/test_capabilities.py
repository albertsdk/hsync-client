# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Capability advertisement: metadata is independent from block serving."""

from __future__ import annotations

from hsync_common.protocol.handshake import Capability


def test_inventory_is_advertised_even_without_block_serving(monkeypatch):
    from hsync_client import capabilities
    from hsync_client.config import CONFIG

    monkeypatch.setitem(CONFIG, "P2P_PEER_DOWNLOAD", False)
    caps = capabilities.local_capabilities()

    assert Capability.index in caps
    assert Capability.peer_download not in caps


def test_block_serving_adds_peer_download(monkeypatch):
    from hsync_client import capabilities
    from hsync_client.config import CONFIG

    monkeypatch.setitem(CONFIG, "P2P_PEER_DOWNLOAD", True)
    caps = capabilities.local_capabilities()

    assert Capability.index in caps
    assert Capability.peer_download in caps


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
