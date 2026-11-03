# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Unit tests for the shared network helpers (multi-homed address discovery)."""

from __future__ import annotations

import hsync_client.net_utils as net_utils


def test_the_default_route_address_comes_first(monkeypatch):
    monkeypatch.setattr(net_utils, "get_local_ip", lambda: "10.145.201.139")
    monkeypatch.setattr(
        net_utils, "_enumerate_local_ipv4", lambda: ["192.168.1.5", "10.145.201.139"]
    )
    assert net_utils.get_local_ips() == ["10.145.201.139", "192.168.1.5"]


def test_loopback_and_link_local_are_dropped(monkeypatch):
    monkeypatch.setattr(net_utils, "get_local_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(
        net_utils,
        "_enumerate_local_ipv4",
        lambda: ["127.0.0.1", "169.254.10.1", "192.168.1.5"],
    )
    assert net_utils.get_local_ips() == ["192.168.1.5"]


def test_falls_back_to_the_primary_when_nothing_enumerates(monkeypatch):
    monkeypatch.setattr(net_utils, "get_local_ip", lambda: "192.168.1.5")
    monkeypatch.setattr(net_utils, "_enumerate_local_ipv4", lambda: [])
    assert net_utils.get_local_ips() == ["192.168.1.5"]


def test_offline_keeps_the_loopback_fallback(monkeypatch):
    """With no interface at all the loopback placeholder is still advertised."""
    monkeypatch.setattr(net_utils, "get_local_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(net_utils, "_enumerate_local_ipv4", lambda: [])
    assert net_utils.get_local_ips() == ["127.0.0.1"]
