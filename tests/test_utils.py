"""
Tests for utility functions in hsync_client.utils module.
"""

import os
import stat
import tempfile
import threading
import time
from unittest.mock import patch

from hsync_common.path_validation import is_valid_filename

from hsync_client.config import CONFIG, set_config
from hsync_client.utils import (
    _suppress_insecure_warning,
    ensure_str_path,
    get_cid,
    get_http_session,
    get_permissions,
    get_ssl_verify,
    is_same_paths,
    is_valid_logfile,
    path_lock,
    path_present,
    set_permissions,
    warn_if_inotify_watch_limit_low,
)


class TestIsValidLogfile:
    """Tests for is_valid_logfile function."""

    def test_simple_filename(self):
        """Test simple filename is valid."""
        assert is_valid_logfile("hsync.log") is True

    def test_filename_with_extension(self):
        """Test filename with extension is valid."""
        assert is_valid_logfile("app.log.txt") is True

    def test_forbidden_chars_in_filename(self):
        """Test forbidden characters are rejected."""
        assert is_valid_logfile("file<name>.log") is False
        assert is_valid_logfile("file:name.log") is False
        assert is_valid_logfile("file|name.log") is False
        assert is_valid_logfile("file?name.log") is False
        assert is_valid_logfile("file*name.log") is False

    def test_reserved_names_windows(self):
        """Test Windows reserved names are rejected."""
        assert is_valid_logfile("CON") is False
        assert is_valid_logfile("PRN") is False
        assert is_valid_logfile("AUX") is False
        assert is_valid_logfile("NUL") is False
        assert is_valid_logfile("COM1") is False
        assert is_valid_logfile("LPT1") is False

    def test_reserved_names_with_extension(self):
        """Test Windows reserved names with extension are rejected."""
        # Reserved names are also reserved when followed by an extension
        assert is_valid_logfile("NUL.txt") is False
        assert is_valid_logfile("CON.log") is False
        assert is_valid_logfile("COM1.log") is False
        assert is_valid_logfile("LPT1.txt") is False
        assert is_valid_logfile("AUX.dat") is False

    def test_reserved_names_all_ports(self):
        """Test all COM and LPT port numbers are rejected."""
        for i in range(1, 10):
            assert is_valid_logfile(f"COM{i}") is False
            assert is_valid_logfile(f"LPT{i}") is False

    def test_reserved_names_superscript(self):
        """Test superscript digit variants of COM and LPT are rejected."""
        # Windows recognizes ¹²³ as valid digits in device names
        assert is_valid_logfile("COM\u00b9") is False  # COM¹
        assert is_valid_logfile("COM\u00b2") is False  # COM²
        assert is_valid_logfile("COM\u00b3") is False  # COM³
        assert is_valid_logfile("LPT\u00b9") is False  # LPT¹
        assert is_valid_logfile("LPT\u00b2") is False  # LPT²
        assert is_valid_logfile("LPT\u00b3") is False  # LPT³

    def test_control_chars_rejected(self):
        """Test control characters (ASCII 1-31) are rejected."""
        assert is_valid_logfile("file\x01name.log") is False
        assert is_valid_logfile("file\x1fname.log") is False
        assert is_valid_logfile("file\tname.log") is False  # Tab
        assert is_valid_logfile("file\nname.log") is False  # Newline

    def test_nul_char_rejected(self):
        """Test NUL character is rejected."""
        assert is_valid_logfile("file\x00name.log") is False

    def test_trailing_space_rejected(self):
        """Test trailing space is rejected (Windows ignores them)."""
        assert is_valid_logfile("file.log ") is False
        assert is_valid_logfile("file ") is False

    def test_trailing_period_rejected(self):
        """Test trailing period is rejected (Windows ignores them)."""
        assert is_valid_logfile("file.") is False
        assert is_valid_logfile("filename.") is False

    def test_absolute_path_valid(self):
        """Test absolute path is valid."""
        assert is_valid_logfile("/var/log/hsync.log") is True
        assert is_valid_logfile("/tmp/app.log") is True

    def test_relative_path_with_dir_invalid(self):
        """Test relative path with directory is invalid."""
        assert is_valid_logfile("logs/hsync.log") is False
        assert is_valid_logfile("subdir/logs/hsync.log") is False


class TestIsValidFilename:
    """Tests for is_valid_filename function."""

    def test_valid_filenames(self):
        """Test valid filenames are accepted."""
        assert is_valid_filename("file.txt") is True
        assert is_valid_filename("document.pdf") is True
        assert is_valid_filename("image-2024.png") is True
        assert is_valid_filename("data_file.csv") is True

    def test_empty_filename_rejected(self):
        """Test empty filename is rejected."""
        assert is_valid_filename("") is False

    def test_forbidden_chars_rejected(self):
        """Test filenames with forbidden characters are rejected."""
        assert is_valid_filename("file<name>.txt") is False
        assert is_valid_filename("file:name.txt") is False
        assert is_valid_filename('file"name.txt') is False
        assert is_valid_filename("file|name.txt") is False
        assert is_valid_filename("file?name.txt") is False
        assert is_valid_filename("file*name.txt") is False

    def test_control_chars_rejected(self):
        """Test filenames with control characters are rejected."""
        assert is_valid_filename("file\x01name.txt") is False
        assert is_valid_filename("file\x1fname.txt") is False

    def test_nul_char_rejected(self):
        """Test filenames with NUL character are rejected."""
        assert is_valid_filename("file\x00name.txt") is False

    def test_reserved_names_rejected(self):
        """Test Windows reserved names are rejected."""
        assert is_valid_filename("CON") is False
        assert is_valid_filename("NUL") is False
        assert is_valid_filename("COM1") is False
        assert is_valid_filename("LPT1") is False

    def test_reserved_names_with_extension_rejected(self):
        """Test reserved names with extensions are rejected."""
        assert is_valid_filename("NUL.txt") is False
        assert is_valid_filename("COM1.log") is False

    def test_trailing_space_rejected(self):
        """Test filenames with trailing space are rejected."""
        assert is_valid_filename("file.txt ") is False

    def test_trailing_period_rejected(self):
        """Test filenames with trailing period are rejected."""
        assert is_valid_filename("file.") is False


class TestIsSamePaths:
    """Tests for is_same_paths function."""

    def test_same_path(self):
        """Test identical paths."""
        assert is_same_paths("/tmp/file.txt", "/tmp/file.txt") is True

    def test_normalized_paths(self):
        """Test paths that normalize to same location."""
        assert is_same_paths("/tmp/../tmp/file.txt", "/tmp/file.txt") is True
        assert is_same_paths("/tmp/./file.txt", "/tmp/file.txt") is True

    def test_different_paths(self):
        """Test different paths."""
        assert is_same_paths("/tmp/file1.txt", "/tmp/file2.txt") is False


class TestGetCid:
    """Tests for get_cid function (NanoID-style)."""

    def test_default_length(self):
        """Test default CID length is 21 (NanoID standard)."""
        cid = get_cid()
        assert len(cid) == 21

    def test_custom_length(self):
        """Test custom CID length."""
        cid = get_cid(length=32)
        assert len(cid) == 32

    def test_alphanumeric_characters(self):
        """Test CID contains only strictly alphanumeric characters (A-Za-z0-9)."""
        cid = get_cid(length=100)
        valid_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
        assert all(c in valid_chars for c in cid)

    def test_unique_cids(self):
        """Test CIDs are unique (cryptographically secure)."""
        cids = [get_cid() for _ in range(100)]
        assert len(set(cids)) == 100

    def test_no_special_chars(self):
        """Test CID never contains - or _ (always starts alphanumeric)."""
        # Generate many CIDs; none may contain the old URL-safe specials.
        cids = "".join(get_cid() for _ in range(100))
        assert "-" not in cids and "_" not in cids

    def test_first_char_always_alphanumeric(self):
        """Every generated CID starts with a letter or digit, never -/_."""
        for _ in range(200):
            assert get_cid()[0].isalnum()


class TestGetPermissions:
    """Tests for get_permissions function."""

    def test_get_file_permissions(self):
        """Test getting file permissions."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name
            os.chmod(temp_path, 0o644)

        try:
            perms = get_permissions(temp_path)
            assert perms == "644"
        finally:
            os.unlink(temp_path)

    def test_get_directory_permissions(self):
        """Test getting directory permissions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chmod(temp_dir, 0o755)
            perms = get_permissions(temp_dir)
            assert perms == "755"

    def test_get_executable_permissions(self):
        """Test getting executable file permissions."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name
            os.chmod(temp_path, 0o755)

        try:
            perms = get_permissions(temp_path)
            assert perms == "755"
        finally:
            os.unlink(temp_path)


class TestSetPermissions:
    """Tests for set_permissions function."""

    def test_set_read_write_permissions(self):
        """Test setting read-write permissions."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name

        try:
            set_permissions(temp_path, "644")
            mode = stat.S_IMODE(os.stat(temp_path).st_mode)
            assert mode == 0o644
        finally:
            os.unlink(temp_path)

    def test_set_executable_permissions(self):
        """Test setting executable permissions."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name

        try:
            set_permissions(temp_path, "755")
            mode = stat.S_IMODE(os.stat(temp_path).st_mode)
            assert mode == 0o755
        finally:
            os.unlink(temp_path)

    def test_set_permissions_no_change(self):
        """Test setting same permissions does nothing."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name
            os.chmod(temp_path, 0o644)

        try:
            # Should not raise, just return
            set_permissions(temp_path, "644")
            mode = stat.S_IMODE(os.stat(temp_path).st_mode)
            assert mode == 0o644
        finally:
            os.unlink(temp_path)


class TestGetSslVerify:
    """Tests for get_ssl_verify function."""

    def test_returns_false_when_insecure(self):
        """Test returns False when NO_VERIFY_SSL is True."""
        original = CONFIG.copy()
        try:
            CONFIG["NO_VERIFY_SSL"] = True
            CONFIG["CA_CERT"] = None
            result = get_ssl_verify()
            assert result is False
        finally:
            set_config(original)

    def test_returns_ca_cert_path(self):
        """Test returns CA cert path when provided."""
        original = CONFIG.copy()
        try:
            CONFIG["NO_VERIFY_SSL"] = False
            CONFIG["CA_CERT"] = "/path/to/ca.crt"
            result = get_ssl_verify()
            assert result == "/path/to/ca.crt"
        finally:
            set_config(original)

    def test_returns_true_by_default(self):
        """Test returns True for default SSL verification."""
        original = CONFIG.copy()
        try:
            CONFIG["NO_VERIFY_SSL"] = False
            CONFIG["CA_CERT"] = None
            result = get_ssl_verify()
            assert result is True
        finally:
            set_config(original)

    def test_insecure_takes_priority(self):
        """Test NO_VERIFY_SSL takes priority over CA_CERT."""
        original = CONFIG.copy()
        try:
            CONFIG["NO_VERIFY_SSL"] = True
            CONFIG["CA_CERT"] = "/path/to/ca.crt"
            result = get_ssl_verify()
            assert result is False
        finally:
            set_config(original)


class TestEnsureStrPath:
    """Tests for ensure_str_path function."""

    def test_string_passthrough(self):
        """String path is returned as-is."""
        assert ensure_str_path("/tmp/file.txt") == "/tmp/file.txt"

    def test_bytes_decoded(self):
        """Bytes path is decoded to string."""
        assert ensure_str_path(b"/tmp/file.txt") == "/tmp/file.txt"

    def test_none_returns_empty(self):
        """None returns empty string."""
        assert ensure_str_path(None) == ""

    def test_empty_string(self):
        """Empty string is returned as-is."""
        assert ensure_str_path("") == ""

    def test_unicode_path(self):
        """Unicode path is handled correctly."""
        assert ensure_str_path("/tmp/résumé.txt") == "/tmp/résumé.txt"

    def test_bytes_unicode(self):
        """Bytes with unicode are decoded correctly."""
        assert ensure_str_path("/tmp/résumé.txt".encode()) == "/tmp/résumé.txt"


class TestSuppressInsecureWarning:
    """Tests for _suppress_insecure_warning function."""

    def test_suppresses_warning(self):
        """Calling _suppress_insecure_warning does not raise."""
        import hsync_client.utils as utils_mod

        old_flag = utils_mod._insecure_warning_suppressed
        try:
            utils_mod._insecure_warning_suppressed = False
            _suppress_insecure_warning()
            assert utils_mod._insecure_warning_suppressed is True
        finally:
            utils_mod._insecure_warning_suppressed = old_flag

    def test_idempotent(self):
        """Calling twice does not raise."""
        import hsync_client.utils as utils_mod

        old_flag = utils_mod._insecure_warning_suppressed
        try:
            utils_mod._insecure_warning_suppressed = False
            _suppress_insecure_warning()
            _suppress_insecure_warning()
            assert utils_mod._insecure_warning_suppressed is True
        finally:
            utils_mod._insecure_warning_suppressed = old_flag


class TestGetHttpSession:
    """Tests for get_http_session and connection-level fingerprint pinning."""

    def test_plain_session_without_fingerprint(self):
        """No MASTER_CERT_FINGERPRINT → no fingerprint adapter is mounted."""
        from hsync_client.ssl_utils import FingerprintHTTPAdapter

        original = CONFIG.copy()
        try:
            CONFIG.pop("MASTER_CERT_FINGERPRINT", None)
            session = get_http_session()
            try:
                adapter = session.get_adapter("https://192.168.1.100:8443")
                assert not isinstance(adapter, FingerprintHTTPAdapter)
            finally:
                session.close()
        finally:
            set_config(original)

    def test_mounts_fingerprint_adapter(self):
        """MASTER_CERT_FINGERPRINT → an HTTPS fingerprint adapter is mounted."""
        from hsync_client.ssl_utils import FingerprintHTTPAdapter

        original = CONFIG.copy()
        try:
            CONFIG["MASTER_CERT_FINGERPRINT"] = "deadbeef" * 8
            session = get_http_session()
            try:
                adapter = session.get_adapter("https://192.168.1.100:8443")
                assert isinstance(adapter, FingerprintHTTPAdapter)
                assert adapter._assert_fingerprint == "deadbeef" * 8
            finally:
                session.close()
        finally:
            set_config(original)

    def test_fingerprint_session_ignores_environment_proxies(self):
        """A pinned P2P session must not consult the environment for proxies.

        The pinned fingerprint *is* the P2P trust anchor (TOFU): an environment
        proxy or CA bundle would add a second, conflicting one, and a
        peer-to-peer connection routed through an environment proxy would make
        the certificate pinning meaningless.

        It is also a per-file cost on the hot path: ``requests`` re-resolves the
        environment proxy configuration on every request
        (``Session.merge_environment_settings`` → ``get_environ_proxies`` →
        ``getproxies``/``proxy_bypass_environment``, each walking ``os.environ``),
        measured at 5 calls and ~3.8% of the P2P apply-path profile.
        """
        original = CONFIG.copy()
        calls: list[int] = []
        try:
            with patch("requests.utils.getproxies", lambda: calls.append(1) or {}):
                CONFIG["MASTER_CERT_FINGERPRINT"] = "deadbeef" * 8
                session = get_http_session()
                try:
                    assert session.trust_env is False
                    session.merge_environment_settings(
                        "https://peer.invalid/x", {}, None, None, None
                    )
                finally:
                    session.close()
        finally:
            set_config(original)
        assert calls == [], "a pinned P2P session consulted the environment proxies"

    def test_plain_session_still_consults_environment_proxies(self):
        """Remote mode keeps honouring the environment proxy configuration."""
        original = CONFIG.copy()
        calls: list[int] = []
        try:
            with patch("requests.utils.getproxies", lambda: calls.append(1) or {}):
                CONFIG.pop("MASTER_CERT_FINGERPRINT", None)
                session = get_http_session()
                try:
                    assert session.trust_env is True
                    session.merge_environment_settings(
                        "https://server.invalid/x", {}, None, None, None
                    )
                finally:
                    session.close()
        finally:
            set_config(original)
        assert calls, "remote mode stopped honouring the environment proxies"


class TestGetSslVerifyWithFingerprint:
    """Tests for get_ssl_verify with fingerprint verification."""

    def test_returns_false_with_fingerprint(self):
        """Returns False when MASTER_CERT_FINGERPRINT is set."""
        import hsync_client.utils as utils_mod

        original = CONFIG.copy()
        old_flag = utils_mod._insecure_warning_suppressed
        try:
            CONFIG["NO_VERIFY_SSL"] = False
            CONFIG["CA_CERT"] = None
            CONFIG["MASTER_CERT_FINGERPRINT"] = "abc123"
            result = get_ssl_verify()
            assert result is False
        finally:
            set_config(original)
            utils_mod._insecure_warning_suppressed = old_flag


# =========================================================================
# Bandwidth Utility Tests
# =========================================================================

from hsync_client.utils import (
    get_throttle_delay,
    parse_bandwidth,
    reset_bandwidth_throttle,
)


class TestParseBandwidth:
    """Tests for parse_bandwidth function."""

    def test_none_returns_none(self):
        assert parse_bandwidth(None) is None

    def test_zero_int_returns_none(self):
        assert parse_bandwidth(0) is None

    def test_zero_str_returns_none(self):
        assert parse_bandwidth("0") is None

    def test_empty_str_returns_none(self):
        assert parse_bandwidth("") is None

    def test_positive_int_passthrough(self):
        assert parse_bandwidth(1024) == 1024

    def test_negative_int_raises(self):
        import pytest

        with pytest.raises(ValueError, match="positive"):
            parse_bandwidth(-1)

    def test_kb_suffix(self):
        assert parse_bandwidth("1KB") == 1024

    def test_mb_suffix(self):
        assert parse_bandwidth("1MB") == 1024**2

    def test_gb_suffix(self):
        assert parse_bandwidth("1GB") == 1024**3

    def test_kb_per_s_suffix(self):
        assert parse_bandwidth("500KB/s") == 500 * 1024

    def test_mb_per_s_suffix(self):
        assert parse_bandwidth("2MB/s") == 2 * 1024**2

    def test_b_suffix(self):
        assert parse_bandwidth("8192B") == 8192

    def test_fractional_value(self):
        assert parse_bandwidth("1.5MB") == int(1.5 * 1024**2)

    def test_plain_number_string(self):
        assert parse_bandwidth("65536") == 65536

    def test_case_insensitive(self):
        assert parse_bandwidth("1mb") == 1024**2

    def test_whitespace_trimmed(self):
        assert parse_bandwidth("  1KB  ") == 1024

    def test_invalid_suffix_raises(self):
        import pytest

        with pytest.raises(ValueError, match="Invalid bandwidth"):
            parse_bandwidth("1XB")

    def test_invalid_type_raises(self):
        import pytest

        with pytest.raises(ValueError, match="Invalid bandwidth"):
            parse_bandwidth(3.14)  # type: ignore[arg-type]


class TestGetThrottleDelay:
    """Tests for get_throttle_delay function."""

    def setup_method(self):
        reset_bandwidth_throttle()

    def teardown_method(self):
        reset_bandwidth_throttle()

    def test_unlimited_returns_zero(self):
        assert get_throttle_delay(1024, None, "test") == 0.0

    def test_zero_limit_returns_zero(self):
        assert get_throttle_delay(1024, 0, "test") == 0.0

    def test_negative_limit_returns_zero(self):
        assert get_throttle_delay(1024, -1, "test") == 0.0

    def test_first_call_no_delay_within_budget(self):
        """First call initialises bucket with bandwidth_limit tokens."""
        # 1024 bytes with 2048 B/s limit → bucket starts at 2048, no delay
        delay = get_throttle_delay(1024, 2048, "test")
        assert delay == 0.0

    def test_first_call_delay_when_exceeding_budget(self):
        """First call should delay when bytes exceed initial tokens."""
        # 4096 bytes with 1024 B/s limit → bucket starts at 1024, needs 3072 more
        delay = get_throttle_delay(4096, 1024, "test")
        assert delay > 0.0
        # Expected: (4096 - 1024) / 1024 = 3.0 seconds
        assert abs(delay - 3.0) < 0.1

    def test_separate_keys_independent(self):
        """Different transfer keys have independent token buckets."""
        delay_a = get_throttle_delay(512, 1024, "key_a")
        delay_b = get_throttle_delay(512, 1024, "key_b")
        assert delay_a == 0.0
        assert delay_b == 0.0

    def test_token_bucket_caps_at_2x(self):
        """Tokens are capped at 2x bandwidth_limit even after long idle."""
        limit = 1024
        _now = [100.0]

        def _monotonic():
            return _now[0]

        with patch("time.monotonic", side_effect=_monotonic):
            # First call: consume a small amount (bucket starts at 1024)
            get_throttle_delay(100, limit, "cap_test")
            # Advance time by 10s → would add 10240 tokens
            # But cap is 2*1024 = 2048 so it should be capped
            _now[0] += 10.0
            # Sending exactly 2*limit should succeed (capped at 2048)
            delay = get_throttle_delay(limit * 2, limit, "cap_test")
            assert delay == 0.0
            # Immediately sending more should require a delay
            delay2 = get_throttle_delay(100, limit, "cap_test")
            assert delay2 > 0.0


class TestResetBandwidthThrottle:
    """Tests for reset_bandwidth_throttle function."""

    def test_reset_clears_state(self):
        """After reset, the next call initialises fresh."""
        # Prime the bucket
        get_throttle_delay(512, 1024, "reset_test")
        reset_bandwidth_throttle()
        # After reset, bucket starts fresh with full tokens
        delay = get_throttle_delay(512, 1024, "reset_test")
        assert delay == 0.0


class TestWarnIfInotifyWatchLimitLow:
    """Tests for warn_if_inotify_watch_limit_low function."""

    def test_no_warning_when_under_limit(self, tmp_path):
        """A small tree below the limit emits no warning."""
        with (
            patch("hsync_client.utils.Path.exists", return_value=True),
            patch("hsync_client.utils.Path.read_text", return_value="8192\n"),
            patch("hsync_client.utils.log.warning") as mock_warn,
        ):
            warn_if_inotify_watch_limit_low(str(tmp_path))
        mock_warn.assert_not_called()

    def test_warns_when_tree_near_limit(self, tmp_path):
        """A tree exceeding the warn threshold emits a warning."""
        # Create a few sub-directories so os.walk yields several entries.
        for i in range(5):
            (tmp_path / f"sub{i}").mkdir()

        with (
            patch("hsync_client.utils.Path.exists", return_value=True),
            # Tiny limit so the threshold (ratio * limit) is crossed quickly.
            patch("hsync_client.utils.Path.read_text", return_value="2\n"),
            patch("hsync_client.utils.log.warning") as mock_warn,
        ):
            warn_if_inotify_watch_limit_low(str(tmp_path))
        mock_warn.assert_called_once()
        assert "max_user_watches" in mock_warn.call_args[0][0]

    def test_no_op_when_limit_file_absent(self, tmp_path):
        """When the limit file is absent (non-Linux) nothing happens."""
        with (
            patch("hsync_client.utils.Path.exists", return_value=False),
            patch("hsync_client.utils.log.warning") as mock_warn,
        ):
            warn_if_inotify_watch_limit_low(str(tmp_path))
        mock_warn.assert_not_called()

    def test_never_raises_on_error(self, tmp_path):
        """A malformed limit file must not raise."""
        with (
            patch("hsync_client.utils.Path.exists", return_value=True),
            patch("hsync_client.utils.Path.read_text", return_value="not-a-number"),
        ):
            warn_if_inotify_watch_limit_low(str(tmp_path))  # must not raise


class TestPathLock:
    """Tests for the per-path serialization primitive."""

    def test_serializes_same_path(self, tmp_path):
        """Two threads on the same path never run their critical sections at once."""
        target = str(tmp_path / "same")
        guard = threading.Lock()
        start = threading.Barrier(2)
        active = 0
        max_active = 0

        def worker():
            nonlocal active, max_active
            start.wait()
            with path_lock(target):
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with guard:
                    active -= 1

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert max_active == 1

    def test_distinct_paths_run_concurrently(self, tmp_path):
        """Locks on different paths do not block one another."""
        both_in = threading.Barrier(2, timeout=2)
        reached: list[str] = []

        def worker(name: str):
            with path_lock(str(tmp_path / name)):
                # If distinct paths were serialized this barrier would time out.
                both_in.wait()
                reached.append(name)

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert sorted(reached) == ["a", "b"]

    def test_registry_is_cleaned_up_after_release(self, tmp_path):
        """The per-path lock entry is dropped once no waiter holds it."""
        from hsync_client.utils import _path_locks

        target = str(tmp_path / "cleanup")
        key = os.path.normpath(os.path.abspath(target))
        with path_lock(target):
            assert key in _path_locks
        assert key not in _path_locks


class TestPathPresent:
    """Tests for path_present (symlink-aware existence check)."""

    def test_existing_file(self, tmp_path):
        """A regular file counts as present."""
        target = tmp_path / "file.txt"
        target.write_bytes(b"x")
        assert path_present(str(target)) is True

    def test_existing_directory(self, tmp_path):
        """A directory counts as present."""
        assert path_present(str(tmp_path)) is True

    def test_missing_path(self, tmp_path):
        """A path with no directory entry counts as absent."""
        assert path_present(str(tmp_path / "ghost.txt")) is False

    def test_dangling_symlink_counts_as_present(self, tmp_path):
        """A dangling symlink is a real directory entry, so it is present.

        ``Path.exists()`` follows the link and reports False; callers that act
        on the entry (inbound delete/rename, the stale-delete guard) must see
        it as present or they would drop a genuine entry from their decisions.
        """
        link = tmp_path / "dangling"
        link.symlink_to(tmp_path / "does-not-exist")
        assert path_present(str(link)) is True
