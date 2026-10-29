# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Unit tests for the durable rendezvous trust store (``rendezvous_server_known_hosts.json``).

The store is the client's known hosts (ADR-0035): it persists the accepted
fingerprint per ``host:port`` with ``first_seen`` and the ``source`` that trusted
it, and fails closed on any other shape.
"""

from __future__ import annotations

import json

import pytest

from hsync_client.rendezvous_trust import (
    RENDEZVOUS_TRUST_FILE_ENV,
    RendezvousTrustStore,
    RendezvousTrustStoreError,
    default_rendezvous_trust_path,
)

HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"


def test_default_path_override(tmp_path, monkeypatch):
    target = tmp_path / "trust.json"
    monkeypatch.setenv(RENDEZVOUS_TRUST_FILE_ENV, str(target))
    assert default_rendezvous_trust_path() == target


def test_set_and_get_roundtrip(tmp_path):
    store = RendezvousTrustStore(tmp_path / "trust.json")
    store.set("boot.example.net", 443, f"sha256:{HEX}", source="cli")
    assert store.get("boot.example.net", 443) == HEX
    assert store.get("boot.example.net", 444) is None


def test_set_repins_current_and_keeps_first_seen(tmp_path):
    store = RendezvousTrustStore(tmp_path / "trust.json")
    store.set("boot.example.net", 443, HEX, source="tofu")
    first_seen = store.entries()[0]["first_seen"]
    store.set("boot.example.net", 443, OTHER, source="cli")
    assert store.get("boot.example.net", 443) == OTHER
    entry = store.entries()[0]
    assert entry["source"] == "cli"
    assert entry["first_seen"] == first_seen


def test_missing_file_is_empty(tmp_path):
    store = RendezvousTrustStore(tmp_path / "trust.json")
    assert store.get("boot.example.net", 443) is None
    assert store.entries() == []


def test_entries_are_sorted_and_typed(tmp_path):
    store = RendezvousTrustStore(tmp_path / "trust.json")
    store.set("b.example.net", 443, OTHER, source="tofu")
    store.set("a.example.net", 1443, HEX, source="prompt")
    entries = store.entries()
    assert [(e["host"], e["port"]) for e in entries] == [
        ("a.example.net", 1443),
        ("b.example.net", 443),
    ]
    assert entries[0]["source"] == "prompt"


def test_remove_reports_whether_an_entry_existed(tmp_path):
    store = RendezvousTrustStore(tmp_path / "trust.json")
    store.set("boot.example.net", 443, HEX, source="cli")
    assert store.remove("boot.example.net", 443) is True
    assert store.remove("boot.example.net", 443) is False
    assert store.entries() == []


def test_set_rejects_invalid_fingerprint_and_source(tmp_path):
    store = RendezvousTrustStore(tmp_path / "trust.json")
    with pytest.raises(ValueError):
        store.set("boot.example.net", 443, "not-a-fingerprint", source="cli")
    with pytest.raises(ValueError):
        store.set("boot.example.net", 443, HEX, source="bogus")


def test_corrupt_file_fails_closed(tmp_path):
    path = tmp_path / "trust.json"
    path.write_text(json.dumps({"version": 1, "rendezvouss": {"h:1": "garbage"}}))
    with pytest.raises(RendezvousTrustStoreError):
        RendezvousTrustStore(path).get("h", 1)


def test_record_without_the_full_shape_fails_closed(tmp_path):
    path = tmp_path / "trust.json"
    path.write_text(
        json.dumps({"version": 1, "rendezvouss": {"h:1": {"current": HEX, "updated_at": "x"}}})
    )
    with pytest.raises(RendezvousTrustStoreError):
        RendezvousTrustStore(path).entries()


def test_unsupported_version_fails_closed(tmp_path):
    path = tmp_path / "trust.json"
    path.write_text(json.dumps({"version": 99, "rendezvouss": {}}))
    with pytest.raises(RendezvousTrustStoreError):
        RendezvousTrustStore(path).get("h", 1)


def test_file_permissions_are_owner_only(tmp_path):
    path = tmp_path / "state" / "trust.json"
    store = RendezvousTrustStore(path)
    store.set("boot.example.net", 443, HEX, source="cli")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
