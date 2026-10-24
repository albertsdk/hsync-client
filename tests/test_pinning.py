"""
Tests for hsync_client.pinning (P2P Trust-On-First-Use certificate store).
"""

import json
import multiprocessing
import os
import stat
import threading
from pathlib import Path

import pytest
from hsync_common.protocol.pairing import PinDecision

from hsync_client.pinning import (
    PIN_FILE_ENV,
    PinStore,
    PinStoreError,
    default_pin_store,
    default_pin_store_path,
)


def _race_first_pin(path: str, fingerprint: str, start, results) -> None:
    """Child-process target for transactional first-use pinning."""
    start.wait()
    try:
        decision = PinStore(Path(path)).check_and_record("cid-race", "bucket", fingerprint)
        results.put(decision.value)
    except BaseException as exc:  # pragma: no cover - asserted in parent
        results.put(f"error:{type(exc).__name__}:{exc}")


class TestDefaultPinStorePath:
    """Resolution of the pin-store location."""

    def test_env_override_takes_precedence(self, tmp_path, monkeypatch):
        target = tmp_path / "custom" / "pins.json"
        monkeypatch.setenv(PIN_FILE_ENV, str(target))
        assert default_pin_store_path() == target
        assert default_pin_store().path == target

    def test_xdg_state_home_is_used_when_no_override(self, tmp_path, monkeypatch):
        monkeypatch.delenv(PIN_FILE_ENV, raising=False)
        monkeypatch.delenv("HSYNC_STATE_DIR", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        path = default_pin_store_path()
        assert path == tmp_path / "hsync" / "p2p_known_peers.json"

    def test_state_dir_is_the_default_parent(self, tmp_path, monkeypatch):
        """HSYNC_STATE_DIR relocates the pins together with the rest of the state."""
        monkeypatch.delenv(PIN_FILE_ENV, raising=False)
        monkeypatch.setenv("HSYNC_STATE_DIR", str(tmp_path / "state"))
        assert default_pin_store_path() == tmp_path / "state" / "p2p_known_peers.json"


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

    def test_entries_are_sorted_by_cid_then_bucket(self, tmp_path):
        store = PinStore(tmp_path / "pins.json")
        store.set("cid-b", "bucket", "b" * 64)
        store.set("cid-a", "bucket", "a" * 64)
        store.set("cid-a", "alpha", "c" * 64)
        entries = store.entries()
        assert [(e["cid"], e["bucket"], e["fingerprint"]) for e in entries] == [
            ("cid-a", "alpha", "c" * 64),
            ("cid-a", "bucket", "a" * 64),
            ("cid-b", "bucket", "b" * 64),
        ]
        assert all(e["first_seen"] for e in entries)

    def test_first_seen_is_recorded_and_preserved_on_repin(self, tmp_path):
        store = PinStore(tmp_path / "pins.json")
        store.set("cid-1", "bucket", "a" * 64)
        first = store.entries()[0]["first_seen"]
        assert first
        store.set("cid-1", "bucket", "b" * 64)  # a documented repin
        assert store.entries()[0]["first_seen"] == first
        assert store.get("cid-1", "bucket") == "b" * 64

    def test_legacy_flat_entry_fails_closed(self, tmp_path):
        """A bare fingerprint value (no record) is not a valid pin entry."""
        path = tmp_path / "pins.json"
        path.write_text(json.dumps({"cid-1|bucket": "a" * 64}), encoding="utf-8")
        with pytest.raises(PinStoreError, match="corrupt"):
            PinStore(path).get("cid-1", "bucket")

    def test_entries_empty(self, tmp_path):
        assert PinStore(tmp_path / "pins.json").entries() == []

    def test_remove_reports_whether_an_entry_existed(self, tmp_path):
        store = PinStore(tmp_path / "pins.json")
        store.set("cid-1", "bucket", "a" * 64)
        assert store.remove("cid-1", "bucket") is True
        assert store.remove("cid-1", "bucket") is False
        assert store.entries() == []

    def test_pin_file_is_owner_only(self, tmp_path):
        path = tmp_path / "pins.json"
        PinStore(path).set("cid-1", "bucket", "deadbeef")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_corrupt_file_fails_closed(self, tmp_path):
        path = tmp_path / "pins.json"
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(PinStoreError, match="corrupt"):
            PinStore(path).get("cid-1", "bucket")
        with pytest.raises(PinStoreError, match="corrupt"):
            PinStore(path).check_and_record("cid-1", "bucket", "abc123")

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
        """A missing fingerprint violates the HTTPS-only P2P contract."""
        store = PinStore(tmp_path / "pins.json")
        with pytest.raises(ValueError, match="fingerprint is mandatory"):
            store.check_and_record("cid-1", "bucket", None)
        assert store.get("cid-1", "bucket") is None

    def test_concurrent_writes_across_instances_do_not_crash(self, tmp_path):
        """Concurrent writers sharing one pin file never race on a temp name.

        Regression: several hsync processes (peers on one host, or multiple
        buckets) share the default pin store. When two of them pinned a new
        master at the same time after a failover, the fixed ``.tmp`` name made
        one writer's ``os.replace`` move the temp file away while the other was
        still writing it, crashing the latter with ``FileNotFoundError``. Each
        write must use its own temp file.
        """
        path = tmp_path / "pins.json"
        barrier = threading.Barrier(4)
        errors: list[BaseException] = []

        def writer(i: int) -> None:
            store = PinStore(path)
            barrier.wait()
            try:
                for _ in range(50):
                    store.set(f"cid-{i}", "bucket", f"f{i}")
            except BaseException as e:  # pragma: no cover - only on regression
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent writes raised: {errors}"
        # The file survives as valid JSON (last writer wins per key).
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert {k.split("|", 1)[0] for k in data} == {f"cid-{i}" for i in range(4)}
        # No temp file left behind.
        assert not list(path.parent.glob("*.tmp"))

    def test_first_use_is_atomic_across_processes(self, tmp_path):
        """Two processes racing first use cannot both establish different trust."""
        ctx = multiprocessing.get_context("spawn")
        path = tmp_path / "pins.json"
        start = ctx.Barrier(2)
        results = ctx.Queue()
        processes = [
            ctx.Process(target=_race_first_pin, args=(str(path), fp, start, results))
            for fp in ("a" * 64, "b" * 64)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0

        decisions = sorted(results.get(timeout=2) for _ in processes)
        assert decisions == ["changed", "trusted"]
        assert PinStore(path).get("cid-race", "bucket") in {"a" * 64, "b" * 64}

    def test_first_use_persistence_failure_never_returns_trusted(self, tmp_path, monkeypatch):
        """TOFU is not granted when the durable write cannot complete."""
        store = PinStore(tmp_path / "pins.json")

        def fail_save(_data):
            raise OSError("disk unavailable")

        monkeypatch.setattr(store, "_save_unlocked", fail_save)
        with pytest.raises(PinStoreError, match="persist"):
            store.check_and_record("cid-1", "bucket", "a" * 64)
