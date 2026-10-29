# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the sync-server trust store, probe, policy and messages (ADR-0036)."""

from __future__ import annotations

import pytest
from hsync_common.protocol.tls_trust import TrustPolicy

from hsync_client import server_trust
from hsync_client.server_trust import (
    SERVER_TRUST_FILE_ENV,
    ServerTrustStore,
    default_server_trust_path,
    fetch_server_fingerprint,
    resolve_server_trust,
)
from hsync_client.trust_messages import server_mismatch_guidance, server_pairing_needed_guidance

_FP_A = "a" * 64
_FP_B = "b" * 64


class TestServerTrustStore:
    def test_set_get_entries_roundtrip(self, tmp_path):
        store = ServerTrustStore(tmp_path / "servers.json")
        store.set("server.example.com", 443, _FP_A, source="cli")
        store.set("server.example.com", 9753, _FP_B, source="tofu")

        assert store.get("server.example.com", 443) == _FP_A
        assert store.entries() == [
            {
                "host": "server.example.com",
                "port": 443,
                "fingerprint": _FP_A,
                "updated_at": store.entries()[0]["updated_at"],
                "first_seen": store.entries()[0]["first_seen"],
                "source": "cli",
            },
            {
                "host": "server.example.com",
                "port": 9753,
                "fingerprint": _FP_B,
                "updated_at": store.entries()[1]["updated_at"],
                "first_seen": store.entries()[1]["first_seen"],
                "source": "tofu",
            },
        ]

    def test_repin_preserves_first_seen(self, tmp_path):
        store = ServerTrustStore(tmp_path / "servers.json")
        store.set("h", 443, _FP_A, source="tofu")
        first = store.entries()[0]["first_seen"]
        store.set("h", 443, _FP_B, source="cli")
        entry = store.entries()[0]
        assert entry["fingerprint"] == _FP_B
        assert entry["source"] == "cli"
        assert entry["first_seen"] == first

    def test_remove_reports_whether_an_entry_existed(self, tmp_path):
        store = ServerTrustStore(tmp_path / "servers.json")
        store.set("h", 443, _FP_A, source="cli")
        assert store.remove("h", 443) is True
        assert store.remove("h", 443) is False

    def test_rejects_invalid_fingerprint_and_source(self, tmp_path):
        store = ServerTrustStore(tmp_path / "servers.json")
        with pytest.raises(ValueError):
            store.set("h", 443, "not-a-fingerprint", source="cli")
        with pytest.raises(ValueError):
            store.set("h", 443, _FP_A, source="bogus")

    def test_default_path_uses_env_override(self, tmp_path, monkeypatch):
        target = tmp_path / "custom.json"
        monkeypatch.setenv(SERVER_TRUST_FILE_ENV, str(target))
        assert default_server_trust_path() == target


class TestResolveServerTrust:
    @pytest.mark.parametrize(
        ("policy", "fingerprints", "tofu", "verify_ssl", "expected"),
        [
            ("chain", (), False, True, TrustPolicy.chain),
            (None, (), False, True, TrustPolicy.chain),
            (None, (), False, False, TrustPolicy.none),
            (None, (_FP_A,), False, True, TrustPolicy.strict),
            (None, (), True, True, TrustPolicy.accept_new),
            ("ask", (), False, True, TrustPolicy.ask),
        ],
    )
    def test_precedence(self, policy, fingerprints, tofu, verify_ssl, expected):
        assert (
            resolve_server_trust(policy, fingerprints, tofu, verify_ssl, env_default=None)
            is expected
        )

    def test_env_default_is_used_when_no_flag(self):
        assert (
            resolve_server_trust(None, (), False, True, env_default="accept-new")
            is TrustPolicy.accept_new
        )


class TestTrustPolicyResolutionParity:
    """Both pinned transports must share one flag/env precedence.

    Regression: the bootnode and the sync server used to carry two hand-copied
    implementations of the same precedence, so a new rule (or a typo in one)
    silently changed one transport only. Only the fallback may differ.
    """

    @pytest.mark.parametrize(
        ("policy", "fingerprints", "tofu", "verify_ssl", "env_default"),
        [
            (None, (), False, True, None),
            (None, (), False, True, "accept-new"),
            (None, (), False, True, "strict"),
            ("ask", (), False, True, "accept-new"),
            (None, (), True, True, "strict"),
            ("ask", (), True, True, None),
            (None, (_FP_A,), False, True, "ask"),
            ("strict", (_FP_A,), True, True, None),
            ("chain", (), False, False, "ask"),
        ],
    )
    def test_transports_agree(
        self, monkeypatch, policy, fingerprints, tofu, verify_ssl, env_default
    ):
        from hsync_client import main_logic

        monkeypatch.setattr(main_logic, "get_discovery_server_trust", lambda: env_default)
        monkeypatch.setattr(main_logic, "get_server_trust", lambda: env_default)

        bootnode = main_logic._resolve_discovery_server_trust(
            policy, fingerprints, tofu, verify_ssl
        )
        server = main_logic._resolve_server_trust(policy, fingerprints, tofu, verify_ssl)

        if env_default is None and policy is None and not fingerprints and not tofu:
            # No signal at all: only the documented default differs.
            assert verify_ssl is True
            assert bootnode is TrustPolicy.ask
            assert server is TrustPolicy.chain
            return
        assert bootnode is server


class _FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeTLS:
    def __init__(self, der: bytes):
        self._der = der

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getpeercert(self, binary_form: bool = False):
        return self._der if binary_form else {}


class _FakeContext:
    check_hostname = True
    verify_mode = None

    def __init__(self, *args, **kwargs):
        pass

    def wrap_socket(self, sock, server_hostname=None):
        return _FakeTLS(b"\x30\x82\x01\x00fake-der")


class TestFetchServerFingerprint:
    def test_returns_none_when_unreachable(self, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(server_trust.socket, "create_connection", boom)
        assert fetch_server_fingerprint("h", 443) is None

    def test_reads_the_presented_certificate(self, monkeypatch):
        monkeypatch.setattr(server_trust.ssl, "SSLContext", _FakeContext)
        monkeypatch.setattr(server_trust.socket, "create_connection", lambda *a, **k: _FakeSocket())
        called: list[bytes] = []

        def fake_compute(der: bytes) -> str:
            called.append(der)
            return _FP_A

        monkeypatch.setattr("hsync_common.ssl_utils.fingerprint_from_der", fake_compute)
        assert fetch_server_fingerprint("h", 443) == _FP_A
        assert called == [b"\x30\x82\x01\x00fake-der"]

    def test_returns_none_when_no_certificate(self, monkeypatch):
        class _NoCertContext(_FakeContext):
            def wrap_socket(self, sock, server_hostname=None):
                return _FakeTLS(b"")

        monkeypatch.setattr(server_trust.ssl, "SSLContext", _NoCertContext)
        monkeypatch.setattr(server_trust.socket, "create_connection", lambda *a, **k: _FakeSocket())
        assert fetch_server_fingerprint("h", 443) is None


class TestServerTrustMessages:
    def test_pairing_needed_names_the_pin_command(self):
        block = server_pairing_needed_guidance("server.example.com", 443, _FP_A)
        assert "server 'server.example.com:443'" in block
        assert f"hsync trust server add server.example.com:443 sha256:{_FP_A}" in block

    def test_changed_pin_names_store_and_commands(self):
        block = server_mismatch_guidance(
            "server.example.com",
            443,
            _FP_B,
            stored=_FP_A,
            store_path="/state/server_known_hosts.json",
            explicit_pins=False,
        )
        assert "/state/server_known_hosts.json" in block
        assert f"hsync trust server add server.example.com:443 sha256:{_FP_B}" in block
        assert "hsync trust server remove server.example.com:443" in block

    def test_explicit_mismatch_points_at_the_flag(self):
        block = server_mismatch_guidance(
            "h", 443, _FP_B, stored=None, store_path="/p.json", explicit_pins=True
        )
        assert "--server-fingerprint" in block
        assert "hsync trust server fingerprint h:443" in block
