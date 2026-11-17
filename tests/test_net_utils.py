# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Unit tests for the shared network helpers (adapter enumeration, IPv6)."""

from __future__ import annotations

import hsync_client.net_utils as net_utils


class _IP:
    def __init__(self, ip: object) -> None:
        self.ip = ip
        self.nice_name = ""


class _Adapter:
    def __init__(self, name: str, ips: list, nice_name: str = "") -> None:
        self.name = name
        self.nice_name = nice_name
        self.ips = [_IP(ip) for ip in ips]


def _patch_adapters(monkeypatch, adapters) -> None:
    monkeypatch.setattr(net_utils, "_get_adapters", lambda: adapters)


def test_the_default_route_address_comes_first(monkeypatch):
    monkeypatch.setattr(net_utils, "get_local_ip", lambda: "10.145.201.139")
    _patch_adapters(monkeypatch, [_Adapter("eth0", ["192.168.1.5", "10.145.201.139"])])
    assert net_utils.get_local_addresses().ipv4 == ("10.145.201.139", "192.168.1.5")


def test_loopback_and_link_local_are_dropped(monkeypatch):
    monkeypatch.setattr(net_utils, "get_local_ip", lambda: "127.0.0.1")
    _patch_adapters(
        monkeypatch,
        [
            _Adapter("lo", ["127.0.0.1"]),
            _Adapter("eth0", ["169.254.10.1", "192.168.1.5"]),
        ],
    )
    assert net_utils.get_local_addresses().ipv4 == ("192.168.1.5",)


def test_falls_back_to_the_primary_when_nothing_enumerates(monkeypatch):
    monkeypatch.setattr(net_utils, "get_local_ip", lambda: "192.168.1.5")
    _patch_adapters(monkeypatch, [])
    assert net_utils.get_local_addresses().ipv4 == ("192.168.1.5",)


def test_offline_keeps_the_loopback_fallback(monkeypatch):
    """With no interface at all the loopback placeholder is still advertised."""
    monkeypatch.setattr(net_utils, "get_local_ip", lambda: "127.0.0.1")
    _patch_adapters(monkeypatch, [])
    assert net_utils.get_local_addresses().ipv4 == ("127.0.0.1",)


def test_global_addresses_come_after_lan_ones(monkeypatch):
    monkeypatch.setattr(net_utils, "get_local_ip", lambda: "10.0.0.1")
    _patch_adapters(monkeypatch, [_Adapter("eth0", ["8.8.8.8", "192.168.1.5", "10.0.0.1"])])
    assert net_utils.get_local_addresses().ipv4 == ("10.0.0.1", "192.168.1.5", "8.8.8.8")


def test_ipv6_keeps_ula_and_global_and_drops_unusable(monkeypatch):
    monkeypatch.setattr(net_utils, "get_local_ip", lambda: "192.168.1.5")
    _patch_adapters(
        monkeypatch,
        [
            _Adapter(
                "eth0",
                [
                    ("fd00::1", 0, 0),
                    ("2001:db8::5", 0, 0),
                    ("fe80::1", 0, 2),
                    ("::1", 0, 0),
                    ("ff02::1", 0, 2),
                ],
            )
        ],
    )
    assert net_utils.get_local_addresses().ipv6 == ("fd00::1", "2001:db8::5")


def test_adapter_enumeration_failure_falls_back_to_the_hostname(monkeypatch):
    monkeypatch.setattr(net_utils, "get_local_ip", lambda: "192.168.1.5")

    def _explode():
        raise OSError("no adapters")

    monkeypatch.setattr(net_utils, "_get_adapters", _explode)
    monkeypatch.setattr(net_utils, "_hostname_ipv4", lambda: ["192.168.1.6"])
    assert net_utils.get_local_addresses().ipv4 == ("192.168.1.5", "192.168.1.6")


def test_windows_virtual_adapters_are_skipped(monkeypatch):
    monkeypatch.setattr(net_utils.sys, "platform", "win32")
    virtual = _Adapter("guid", [], nice_name="Hyper-V Virtual Ethernet Adapter")
    real = _Adapter("guid", [], nice_name="Intel(R) Wi-Fi 6 AX201")
    assert net_utils._is_advertisable_adapter(virtual) is False
    assert net_utils._is_advertisable_adapter(real) is True


def test_normalize_peer_ip_maps_ipv4_mapped_and_keeps_the_rest():
    assert net_utils.normalize_peer_ip("::ffff:192.168.1.5") == "192.168.1.5"
    assert net_utils.normalize_peer_ip("192.168.1.5") == "192.168.1.5"
    assert net_utils.normalize_peer_ip("fd00::1") == "fd00::1"
    assert net_utils.normalize_peer_ip("host.example") == "host.example"
    assert net_utils.normalize_peer_ip("") == ""


def test_virtual_interfaces_are_skipped():
    """Container/bridge interfaces are not advertised (their IPs are host-local)."""
    for name in ("docker0", "br-1a2b", "veth9f", "virbr0", "vboxnet0", "vmnet8", "lo"):
        assert net_utils._is_advertisable_interface(name) is False
    for name in ("eth0", "wlan0", "wlp3s0", "enp0s3", "wg0", "tun0"):
        assert net_utils._is_advertisable_interface(name) is True
