"""
Tests for hsync_client.pinning (LAN Trust-On-First-Use certificate store).
"""

import os
import stat

import pytest
from hsync_common.protocol.pairing import PinDecision

from hsync_client.pinning import (
    PIN_FILE_ENV,
    PinStore,
    default_pin_store,
    default_pin_store_path,
)


class TestDefaultPinStorePath:
    """Resolution of the pin-store location."""

    def test_env_override_takes_precedence(self, tmp_path, monkeypatch):
        target = tmp_path / "custom" / "pins.json"
        monkeypatch.setenv(PIN_FILE_ENV, str(target))
        assert default_pin_store_path() == target
        assert default_pin_store().path == target

    def test_xdg_state_home_is_used_when_no_override(self, tmp_path, monkeypatch):
        monkeypatch.delenv(PIN_FILE_ENV, raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        path = default_pin_store_path()
        assert path == tmp_path / "hsync" / "lan_known_peers.json"


class TestPinStore:
    """Persistence and TOFU classification."""

    def test_get_missing_is_none(self, tmp_path):
        store = PinStore(tmp_path / "pins.json")
        assert store.get("cid-1", "bucket") is None

    def test_set_then_get_roundtrips_normalized(self, tmp_path):
        store = PinStore(tmp_path / "pins.json")
        store.set("cid-1", "bucket", "AA:BB:CC")
        assert store.get("cid-1", "bucket") == "aabbcc"

    def test_set_persists_across_instances(self, tmp_path):
        path = tmp_path / "pins.json"
        PinStore(path).set("cid-1", "bucket", "deadbeef")
        assert PinStore(path).get("cid-1", "bucket") == "deadbeef"

    def test_pin_file_is_owner_only(self, tmp_path):
        path = tmp_path / "pins.json"
        PinStore(path).set("cid-1", "bucket", "deadbeef")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_corrupt_file_is_treated_as_empty(self, tmp_path):
        path = tmp_path / "pins.json"
        path.write_text("not json", encoding="utf-8")
        assert PinStore(path).get("cid-1", "bucket") is None

    def test_check_and_record_first_use_is_trusted_and_pins(self, tmp_path):
        path = tmp_path / "pins.json"
        store = PinStore(path)
        assert store.check_and_record("cid-1", "bucket", "abc123") is PinDecision.trusted
        assert store.get("cid-1", "bucket") == "abc123"

    def test_check_and_record_same_is_matched(self, tmp_path):
        store = PinStore(tmp_path / "pins.json")
        store.check_and_record("cid-1", "bucket", "abc123")
        assert store.check_and_record("cid-1", "bucket", "ABC123") is PinDecision.matched

    def test_check_and_record_changed_does_not_repin(self, tmp_path):
        store = PinStore(tmp_path / "pins.json")
        store.check_and_record("cid-1", "bucket", "abc123")
        assert store.check_and_record("cid-1", "bucket", "def456") is PinDecision.changed
        # the original pin is preserved (a change must not silently overwrite trust)
        assert store.get("cid-1", "bucket") == "abc123"

    def test_check_and_record_missing_fingerprint_raises(self, tmp_path):
        """A missing fingerprint violates the HTTPS-only LAN contract."""
        store = PinStore(tmp_path / "pins.json")
        with pytest.raises(ValueError, match="fingerprint is mandatory"):
            store.check_and_record("cid-1", "bucket", None)
        assert store.get("cid-1", "bucket") is None
