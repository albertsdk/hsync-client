"""
Tests targeting coverage gaps identified by pytest-cov for hsync-client.

Covers edge-case branches in:
- validators.py: range validators, bucket_path
- pinning.py: PinStore load/save/check_and_record edge cases
- sync_state.py: empty relpath, empty base_hash, retain, flush
- tombstones.py: empty relpath, merge_payload, get_payload
- utils.py: bandwidth parsing, path_lock, safe_rmdir, ensure_str_path, get_cid
- events.py: OSError on fdopen, serialization error
"""

import threading
from argparse import ArgumentTypeError
from unittest.mock import patch

import pytest
from hsync_common.path_validation import is_valid_filename

from hsync_client.config import CONFIG
from hsync_client.events import EventEmitter, emit_event, init_emitter
from hsync_client.pinning import PinStore, default_pin_store_path
from hsync_client.tombstones import (
    clear_deletion,
    get_payload,
    merge_payload,
    record_deletion,
)
from hsync_client.utils import (
    ensure_str_path,
    get_cid,
    get_throttle_delay,
    is_same_paths,
    is_valid_logfile,
    parse_bandwidth,
    path_lock,
    reset_bandwidth_throttle,
    safe_rmdir_if_empty,
    set_permissions,
)
from hsync_client.validators import (
    create_int_range_validator,
    valid_bucket,
    valid_bucket_path,
    valid_http_timeout,
    valid_max_wait_timeout,
    valid_reconnect_delay,
    valid_resync,
    valid_stability_delay,
)

# ---------------------------------------------------------------------------
# validators.py
# ---------------------------------------------------------------------------


class TestValidatorsCoverageGaps:
    """Cover range validators and bucket_path validation."""

    def test_create_int_range_validator_non_integer(self):
        validator = create_int_range_validator(0, 100, "test")
        with pytest.raises(ArgumentTypeError):
            validator("not-a-number")

    def test_create_int_range_validator_out_of_range(self):
        validator = create_int_range_validator(0, 100, "test")
        with pytest.raises(ArgumentTypeError):
            validator("0")  # <= min (exclusive)
        with pytest.raises(ArgumentTypeError):
            validator("100")  # >= max (exclusive)

    def test_create_int_range_validator_valid(self):
        validator = create_int_range_validator(0, 100, "test")
        assert validator("50") == 50

    def test_valid_reconnect_delay_valid(self):
        assert valid_reconnect_delay("5") == 5

    def test_valid_reconnect_delay_invalid(self):
        with pytest.raises(ArgumentTypeError):
            valid_reconnect_delay("not-a-number")

    def test_valid_resync_valid(self):
        assert valid_resync("300") == 300

    def test_valid_resync_invalid(self):
        with pytest.raises(ArgumentTypeError):
            valid_resync("not-a-number")

    def test_valid_bucket_valid(self):
        assert valid_bucket("my-bucket") == "my-bucket"

    def test_valid_bucket_invalid(self):
        with pytest.raises(ArgumentTypeError):
            valid_bucket(".invalid")

    def test_valid_stability_delay_valid(self):
        assert valid_stability_delay("5") == 5

    def test_valid_stability_delay_invalid(self):
        with pytest.raises(ArgumentTypeError):
            valid_stability_delay("not-a-number")

    def test_valid_http_timeout_valid(self):
        assert valid_http_timeout("30") == 30

    def test_valid_http_timeout_invalid(self):
        with pytest.raises(ArgumentTypeError):
            valid_http_timeout("not-a-number")

    def test_valid_max_wait_timeout_valid(self):
        assert valid_max_wait_timeout("60") == 60

    def test_valid_max_wait_timeout_invalid(self):
        with pytest.raises(ArgumentTypeError):
            valid_max_wait_timeout("not-a-number")

    def test_valid_bucket_path_valid(self, tmp_path):
        assert valid_bucket_path(str(tmp_path)) == str(tmp_path)

    def test_valid_bucket_path_traversal(self):
        with pytest.raises(ArgumentTypeError):
            valid_bucket_path("../../../etc/passwd")


# ---------------------------------------------------------------------------
# pinning.py
# ---------------------------------------------------------------------------


class TestPinningCoverageGaps:
    """Cover PinStore load/save/check_and_record edge cases."""

    def test_pin_store_load_corrupt_file(self, tmp_path):
        """A corrupt pin file is treated as empty."""
        pin_file = tmp_path / "pins.json"
        pin_file.write_text("not valid json")
        store = PinStore(pin_file)
        assert store.get("cid", "bucket") is None

    def test_pin_store_load_non_dict(self, tmp_path):
        """A non-dict JSON is treated as empty."""
        pin_file = tmp_path / "pins.json"
        pin_file.write_text("[1, 2, 3]")
        store = PinStore(pin_file)
        assert store.get("cid", "bucket") is None

    def test_pin_store_set_blank_fingerprint(self, tmp_path):
        """set with a blank fingerprint normalizes to None and is a no-op."""
        pin_file = tmp_path / "pins.json"
        store = PinStore(pin_file)
        # normalize_fingerprint() only rejects missing/blank input, so this is
        # the branch that makes PinStore.set() a no-op (nothing persisted).
        store.set("cid", "bucket", "   ")
        assert store.get("cid", "bucket") is None
        assert not pin_file.exists()

    def test_pin_store_set_normalizes_separators(self, tmp_path):
        """set strips ':'/space separators so equivalent fingerprints pin alike."""
        pin_file = tmp_path / "pins.json"
        store = PinStore(pin_file)
        store.set("cid", "bucket", "AA:BB:CC")
        assert store.get("cid", "bucket") == "aabbcc"

    def test_pin_store_set_and_get(self, tmp_path):
        """set + get round-trips a valid fingerprint."""
        pin_file = tmp_path / "pins.json"
        store = PinStore(pin_file)
        fp = "aa" * 32  # 64 hex chars
        store.set("cid", "bucket", fp)
        assert store.get("cid", "bucket") == fp

    def test_pin_store_check_and_record_trusted(self, tmp_path):
        """First sighting pins the fingerprint (trusted)."""
        pin_file = tmp_path / "pins.json"
        store = PinStore(pin_file)
        fp = "bb" * 32
        from hsync_common.protocol.pairing import PinDecision

        decision = store.check_and_record("cid", "bucket", fp)
        assert decision is PinDecision.trusted
        assert store.get("cid", "bucket") == fp

    def test_pin_store_check_and_record_matched(self, tmp_path):
        """A matching fingerprint returns matched and does not mutate."""
        pin_file = tmp_path / "pins.json"
        store = PinStore(pin_file)
        fp = "cc" * 32
        store.set("cid", "bucket", fp)
        from hsync_common.protocol.pairing import PinDecision

        decision = store.check_and_record("cid", "bucket", fp)
        assert decision is PinDecision.matched

    def test_pin_store_check_and_record_changed(self, tmp_path):
        """A mismatched fingerprint returns changed."""
        pin_file = tmp_path / "pins.json"
        store = PinStore(pin_file)
        fp1 = "dd" * 32
        fp2 = "ee" * 32
        store.set("cid", "bucket", fp1)
        from hsync_common.protocol.pairing import PinDecision

        decision = store.check_and_record("cid", "bucket", fp2)
        assert decision is PinDecision.changed

    def test_default_pin_store_path_env_override(self, tmp_path, monkeypatch):
        """HSYNC_LAN_PIN_FILE overrides the default path."""
        custom = tmp_path / "custom_pins.json"
        monkeypatch.setenv("HSYNC_LAN_PIN_FILE", str(custom))
        assert default_pin_store_path() == custom


# ---------------------------------------------------------------------------
# sync_state.py
# ---------------------------------------------------------------------------


class TestSyncStateCoverageGaps:
    """Cover sync_state empty-relpath and empty-base-hash branches."""

    def test_get_base_hash_empty_bucket(self):
        """get_base_hash returns None when LOCAL_BUCKET is empty."""
        CONFIG["LOCAL_BUCKET"] = ""
        from hsync_client.sync_state import get_base_hash

        assert get_base_hash("/some/path") is None

    def test_record_base_empty_hash(self, tmp_path):
        """record_base is a no-op when base_hash is empty."""
        CONFIG["LOCAL_BUCKET"] = str(tmp_path)
        from hsync_client.sync_state import record_base

        record_base(str(tmp_path / "file.txt"), "")
        # Must not raise

    def test_clear_base_empty_bucket(self):
        """clear_base is a no-op when LOCAL_BUCKET is empty."""
        CONFIG["LOCAL_BUCKET"] = ""
        from hsync_client.sync_state import clear_base

        clear_base("/some/path")
        # Must not raise

    def test_retain_drops_missing(self, tmp_path):
        """retain drops entries for paths not in the provided list."""
        CONFIG["LOCAL_BUCKET"] = str(tmp_path)
        from hsync_client.sync_state import (
            flush,
            get_sync_state_store,
            retain,
        )

        store = get_sync_state_store()
        store.record("keep.txt", "hash1")
        store.record("drop.txt", "hash2")
        retain([str(tmp_path / "keep.txt")])
        flush()
        assert store.base_hash("keep.txt") == "hash1"
        assert store.base_hash("drop.txt") is None


# ---------------------------------------------------------------------------
# tombstones.py
# ---------------------------------------------------------------------------


class TestTombstonesCoverageGaps:
    """Cover tombstone empty-relpath, merge, and payload branches."""

    def test_record_deletion_path_outside_bucket(self, tmp_path):
        """record_deletion is a no-op (empty relpath) when the path is the bucket root."""
        CONFIG["LOCAL_BUCKET"] = str(tmp_path)
        # abs_path == bucket_dir → relpath_in_bucket() yields "" → no marker,
        # no side-car write.
        record_deletion(str(tmp_path))
        assert len(get_payload()) == 0
        assert not (tmp_path / ".hsynctombstones").exists()

    def test_clear_deletion_path_outside_bucket(self, tmp_path):
        """clear_deletion is a no-op (empty relpath) when the path is the bucket root."""
        CONFIG["LOCAL_BUCKET"] = str(tmp_path)
        clear_deletion(str(tmp_path))
        assert len(get_payload()) == 0

    def test_record_and_clear(self, tmp_path):
        """record_deletion + clear_deletion round-trip."""
        CONFIG["LOCAL_BUCKET"] = str(tmp_path)
        f = tmp_path / "file.txt"
        f.write_text("content")
        record_deletion(str(f), sha256sum="abc")
        payload = get_payload()
        assert len(payload) > 0
        clear_deletion(str(f))
        assert len(get_payload()) == 0

    def test_merge_payload_no_changes(self, tmp_path):
        """merge_payload with None returns 0."""
        CONFIG["LOCAL_BUCKET"] = str(tmp_path)
        assert merge_payload(None) == 0

    def test_merge_payload_with_entries(self, tmp_path):
        """merge_payload merges entries and persists."""
        CONFIG["LOCAL_BUCKET"] = str(tmp_path)
        import time

        from hsync_common.models.tombstone import tombstone_key

        payload = {tombstone_key("merged.txt"): {"ts": int(time.time()), "sha256": "a" * 64}}
        changed = merge_payload(payload)
        assert changed == 1
        assert tombstone_key("merged.txt") in get_payload()


# ---------------------------------------------------------------------------
# utils.py
# ---------------------------------------------------------------------------


class TestUtilsCoverageGaps:
    """Cover bandwidth parsing, path_lock, safe_rmdir, ensure_str_path, get_cid."""

    def test_parse_bandwidth_none(self):
        assert parse_bandwidth(None) is None

    def test_parse_bandwidth_zero(self):
        assert parse_bandwidth(0) is None
        assert parse_bandwidth("0") is None

    def test_parse_bandwidth_int(self):
        assert parse_bandwidth(1024) == 1024

    def test_parse_bandwidth_negative_int(self):
        with pytest.raises(ValueError):
            parse_bandwidth(-1)

    def test_parse_bandwidth_str_kb(self):
        assert parse_bandwidth("1KB") == 1024

    def test_parse_bandwidth_str_mb(self):
        assert parse_bandwidth("1MB") == 1024**2

    def test_parse_bandwidth_str_gb(self):
        assert parse_bandwidth("1GB") == 1024**3

    def test_parse_bandwidth_str_kb_slash(self):
        assert parse_bandwidth("1KB/s") == 1024

    def test_parse_bandwidth_str_empty(self):
        assert parse_bandwidth("") is None
        assert parse_bandwidth("   ") is None

    def test_parse_bandwidth_str_plain_number(self):
        assert parse_bandwidth("12345") == 12345

    def test_parse_bandwidth_str_invalid(self):
        with pytest.raises(ValueError):
            parse_bandwidth("invalid")

    def test_parse_bandwidth_invalid_type(self):
        with pytest.raises(ValueError):
            parse_bandwidth(3.14)

    def test_get_throttle_delay_no_limit(self):
        reset_bandwidth_throttle()
        assert get_throttle_delay(100, None, "test") == 0.0

    def test_get_throttle_delay_zero_limit(self):
        reset_bandwidth_throttle()
        assert get_throttle_delay(100, 0, "test") == 0.0

    def test_get_throttle_delay_enough_tokens(self):
        reset_bandwidth_throttle()
        # First call initializes with full bucket
        delay = get_throttle_delay(100, 1000, "upload")
        assert delay == 0.0

    def test_get_throttle_delay_needs_delay(self):
        reset_bandwidth_throttle()
        # Consume all tokens first
        get_throttle_delay(1000, 1000, "download")
        # Now request more than available
        delay = get_throttle_delay(2000, 1000, "download")
        assert delay > 0.0

    def test_path_lock_serializes(self):
        """path_lock grants exclusive access: critical sections never overlap."""
        import time

        results = []
        overlaps = []
        in_section = 0
        state_guard = threading.Lock()

        def worker(name):
            nonlocal in_section
            with path_lock("/tmp/test_lock_path"):
                with state_guard:
                    in_section += 1
                    if in_section > 1:
                        overlaps.append(name)
                # Hold the lock briefly so a non-serializing impl would overlap.
                time.sleep(0.02)
                with state_guard:
                    in_section -= 1
                    results.append(name)

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 4
        assert overlaps == []  # no two threads were ever inside the lock at once

    def test_safe_rmdir_if_empty_nonexistent(self):
        assert safe_rmdir_if_empty("/nonexistent/path/12345") is False

    def test_safe_rmdir_if_empty_with_entries(self, tmp_path):
        (tmp_path / "file.txt").write_text("x")
        assert safe_rmdir_if_empty(str(tmp_path)) is False

    def test_safe_rmdir_if_empty_removes(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert safe_rmdir_if_empty(str(empty), bucket_root=str(tmp_path)) is True
        assert not empty.exists()

    def test_safe_rmdir_if_empty_protects_bucket_root(self, tmp_path):
        """The bucket root is never removed."""
        assert safe_rmdir_if_empty(str(tmp_path), bucket_root=str(tmp_path)) is False

    def test_ensure_str_path_none(self):
        assert ensure_str_path(None) == ""

    def test_ensure_str_path_bytes(self):
        assert ensure_str_path(b"/tmp/test") == "/tmp/test"

    def test_ensure_str_path_str(self):
        assert ensure_str_path("/tmp/test") == "/tmp/test"

    def test_get_cid_default_length(self):
        cid = get_cid()
        assert len(cid) == 21

    def test_get_cid_custom_length(self):
        cid = get_cid(10)
        assert len(cid) == 10

    def test_is_valid_filename_empty(self):
        assert not is_valid_filename("")

    def test_is_valid_filename_valid(self):
        assert is_valid_filename("normal.txt")

    def test_is_valid_filename_forbidden(self):
        assert not is_valid_filename("file\x00name")

    def test_is_valid_filename_reserved(self):
        assert not is_valid_filename("CON")

    def test_is_valid_filename_trailing_space(self):
        assert not is_valid_filename("file ")

    def test_is_valid_logfile_filename_only(self):
        assert is_valid_logfile("app.log")

    def test_is_valid_logfile_absolute(self):
        assert is_valid_logfile("/var/log/app.log")

    def test_is_valid_logfile_relative_with_dir(self):
        assert not is_valid_logfile("dir/app.log")

    def test_is_valid_logfile_forbidden(self):
        assert not is_valid_logfile("file\x00name")

    def test_is_same_paths_same(self):
        assert is_same_paths("/tmp/test", "/tmp/test")

    def test_is_same_paths_different(self):
        assert not is_same_paths("/tmp/test", "/tmp/other")

    def test_set_permissions(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        set_permissions(str(f), "644")
        # Setting same permissions is a no-op
        set_permissions(str(f), "644")


# ---------------------------------------------------------------------------
# events.py — OSError on fdopen
# ---------------------------------------------------------------------------


class TestEventsCoverageGaps:
    """Cover EventEmitter OSError on fdopen."""

    def teardown_method(self):
        init_emitter(None)

    def test_event_emitter_oserror_on_fdopen(self):
        """An OSError when opening the fd results in an inactive emitter."""
        with patch("os.fdopen", side_effect=OSError("bad fd")):
            emitter = EventEmitter(fd=999)
        assert not emitter.active

    def test_emit_event_no_emitter(self):
        """emit_event is a no-op when no emitter is installed."""
        init_emitter(None)
        emit_event("test", data="value")
        # Must not raise

    def test_event_emitter_close_idempotent(self):
        """Closing an already-closed emitter is safe."""
        emitter = EventEmitter(None)
        emitter.close()
        emitter.close()
        assert not emitter.active
