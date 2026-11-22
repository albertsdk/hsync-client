# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The three peer-mesh capabilities: defaults, role presets, and their gates.

Each capability answers its own question and is advertised on its own surface, so
a device can stop serving without stopping fetching, and can stop publishing its
inventory without disappearing from the mesh's protocol support.
"""

from __future__ import annotations

import pytest
from hsync_common.protocol.handshake import Capability

from hsync_client import peer_roles
from hsync_client.config import CONFIG
from hsync_client.peer_roles import (
    PEER_ROLES,
    PeerRole,
    fetch_from_peers,
    peer_role_capabilities,
    publish_inventory,
    serve_blocks,
)


def test_every_capability_is_on_by_default(monkeypatch):
    """A node that configures nothing is a full peer."""
    for key in ("P2P_SERVE_BLOCKS", "P2P_FETCH_FROM_PEERS", "P2P_PUBLISH_INVENTORY"):
        monkeypatch.delitem(CONFIG, key, raising=False)

    assert serve_blocks() is True
    assert fetch_from_peers() is True
    assert publish_inventory() is True


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (PeerRole.full, (True, True, True)),
        (PeerRole.leech, (False, True, False)),
        (PeerRole.seed, (True, False, True)),
        (PeerRole.off, (False, False, False)),
    ],
)
def test_a_role_preset_names_the_three_keys(role, expected):
    """The preset is sugar over the atomic keys, never a fourth state."""
    assert peer_role_capabilities(role) == expected
    assert peer_role_capabilities(role.value) == expected


def test_no_role_means_the_full_role():
    assert peer_role_capabilities(None) == PEER_ROLES[PeerRole.full]
    assert peer_role_capabilities("") == PEER_ROLES[PeerRole.full]


def test_an_unknown_role_is_refused():
    with pytest.raises(ValueError):
        peer_role_capabilities("seeder")


def test_the_roles_cover_every_combination_they_claim():
    """A leech consumes without contributing; a seed contributes without pulling."""
    serve, fetch, publish = PEER_ROLES[PeerRole.leech]
    assert not serve and fetch and not publish
    serve, fetch, publish = PEER_ROLES[PeerRole.seed]
    assert serve and not fetch and publish


def test_each_gate_reads_only_its_own_key(monkeypatch):
    """One key cannot move another capability (the defect this surface fixes)."""
    monkeypatch.setitem(CONFIG, "P2P_SERVE_BLOCKS", False)
    assert serve_blocks() is False
    assert fetch_from_peers() is True
    assert publish_inventory() is True

    monkeypatch.setitem(CONFIG, "P2P_SERVE_BLOCKS", True)
    monkeypatch.setitem(CONFIG, "P2P_FETCH_FROM_PEERS", False)
    assert serve_blocks() is True
    assert fetch_from_peers() is False
    assert publish_inventory() is True


def test_the_local_capabilities_follow_the_gates(monkeypatch):
    """The advertised set is derived from the same three answers."""
    from hsync_client.capabilities import local_capabilities

    monkeypatch.setitem(CONFIG, "P2P_SERVE_BLOCKS", False)
    monkeypatch.setitem(CONFIG, "P2P_PUBLISH_INVENTORY", False)
    assert local_capabilities() & {Capability.peer_download, Capability.publishes_index} == set()

    monkeypatch.setitem(CONFIG, "P2P_SERVE_BLOCKS", True)
    monkeypatch.setitem(CONFIG, "P2P_PUBLISH_INVENTORY", True)
    caps = local_capabilities()
    assert Capability.peer_download in caps
    assert Capability.publishes_index in caps
    # The protocol statement never follows the participation switches.
    assert Capability.index in caps


def test_an_explicit_reader_sees_the_module_from_one_place():
    """``peer_roles`` is the only source of the three answers."""
    assert peer_roles.serve_blocks is serve_blocks
