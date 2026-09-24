# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Unit tests for the bootnode TOFU trust store (ADR-0034)."""

from __future__ import annotations

import json

import pytest

from hsync_client.bootnode_trust import (
    BOOTNODE_TRUST_FILE_ENV,
    BootnodeTrustStore,
    BootnodeTrustStoreError,
    default_bootnode_trust_path,
)

HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"


def test_default_path_override(tmp_path, monkeypatch):
    target = tmp_path / "trust.json"
    monkeypatch.setenv(BOOTNODE_TRUST_FILE_ENV, str(target))
    assert default_bootnode_trust_path() == target


def test_set_and_get_roundtrip(tmp_path, monkeypatch):
    store = BootnodeTrustStore(tmp_path / "trust.json")
    store.set("boot.example.net", 443, f"sha256:{HEX}")
    assert store.get("boot.example.net", 443) == HEX
    assert store.get("boot.example.net", 444) is None


def test_set_repins_current(tmp_path, monkeypatch):
    store = BootnodeTrustStore(tmp_path / "trust.json")
    store.set("boot.example.net", 443, HEX)
    store.set("boot.example.net", 443, OTHER)
    assert store.get("boot.example.net", 443) == OTHER


def test_missing_file_is_empty(tmp_path):
    store = BootnodeTrustStore(tmp_path / "trust.json")
    assert store.get("boot.example.net", 443) is None


def test_set_rejects_invalid_fingerprint(tmp_path):
    store = BootnodeTrustStore(tmp_path / "trust.json")
    with pytest.raises(ValueError):
        store.set("boot.example.net", 443, "not-a-fingerprint")


def test_corrupt_file_fails_closed(tmp_path):
    path = tmp_path / "trust.json"
    path.write_text(json.dumps({"version": 1, "bootnodes": {"h:1": "garbage"}}))
    store = BootnodeTrustStore(path)
    with pytest.raises(BootnodeTrustStoreError):
        store.get("h", 1)


def test_unsupported_version_fails_closed(tmp_path):
    path = tmp_path / "trust.json"
    path.write_text(json.dumps({"version": 99, "bootnodes": {}}))
    store = BootnodeTrustStore(path)
    with pytest.raises(BootnodeTrustStoreError):
        store.get("h", 1)


def test_file_permissions_are_owner_only(tmp_path):
    path = tmp_path / "state" / "trust.json"
    store = BootnodeTrustStore(path)
    store.set("boot.example.net", 443, HEX)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
