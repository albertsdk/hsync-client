"""
Tests for utility functions in hsync_client.utils module.
"""

import os
import stat
import tempfile
from unittest.mock import patch

from hsync_client.config import CONFIG, set_config
from hsync_client.utils import (
    _suppress_insecure_warning,
    bytes_to_human_readable,
    compute_md5,
    ensure_str_path,
    get_chunk_size,
    get_cid,
    get_permissions,
    get_ssl_verify,
    is_same_paths,
    is_valid_filename,
    is_valid_logfile,
    set_permissions,
    verify_master_fingerprint,
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

    def test_url_safe_characters(self):
        """Test CID contains only URL-safe characters (a-zA-Z0-9-_)."""
        cid = get_cid(length=100)
        valid_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert all(c in valid_chars for c in cid)

    def test_unique_cids(self):
        """Test CIDs are unique (cryptographically secure)."""
        cids = [get_cid() for _ in range(100)]
        assert len(set(cids)) == 100

    def test_contains_special_chars(self):
        """Test CID can contain - and _ characters."""
        # Generate many CIDs to statistically ensure we get special chars
        cids = "".join(get_cid() for _ in range(100))
        # With 64-char alphabet and 2100 chars, probability of no - or _ is negligible
        assert "-" in cids or "_" in cids


class TestBytesToHumanReadable:
    """Tests for bytes_to_human_readable function."""

    def test_bytes(self):
        """Test bytes formatting."""
        assert bytes_to_human_readable(0) == "0.00B"
        assert bytes_to_human_readable(512) == "512.00B"
        assert bytes_to_human_readable(1023) == "1023.00B"

    def test_kilobytes(self):
        """Test kilobytes formatting."""
        assert bytes_to_human_readable(1024) == "1.00KB"
        assert bytes_to_human_readable(1536) == "1.50KB"

    def test_megabytes(self):
        """Test megabytes formatting."""
        assert bytes_to_human_readable(1024 * 1024) == "1.00MB"
        assert bytes_to_human_readable(1.5 * 1024 * 1024) == "1.50MB"

    def test_gigabytes(self):
        """Test gigabytes formatting."""
        assert bytes_to_human_readable(1024**3) == "1.00GB"

    def test_terabytes(self):
        """Test terabytes formatting."""
        assert bytes_to_human_readable(1024**4) == "1.00TB"


class TestComputeMd5:
    """Tests for compute_md5 function."""

    def test_compute_md5_empty_file(self):
        """Test MD5 of empty file."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"")
            temp_path = f.name

        try:
            # MD5 of empty string
            assert compute_md5(temp_path) == "d41d8cd98f00b204e9800998ecf8427e"
        finally:
            os.unlink(temp_path)

    def test_compute_md5_known_content(self):
        """Test MD5 of known content."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"hello world")
            temp_path = f.name

        try:
            # MD5 of "hello world"
            assert compute_md5(temp_path) == "5eb63bbbe01eeed093cb22bb8f5acdc3"
        finally:
            os.unlink(temp_path)


class TestGetChunkSize:
    """Tests for get_chunk_size function."""

    def test_none_returns_default(self):
        """Test None file size returns default 64KB."""
        assert get_chunk_size(None) == 64 * 1024

    def test_small_file_under_1mb(self):
        """Test small file returns 8KB chunk."""
        assert get_chunk_size(500_000) == 8 * 1024
        assert get_chunk_size(999_999) == 8 * 1024

    def test_medium_file_under_100mb(self):
        """Test medium file returns 64KB chunk."""
        assert get_chunk_size(1_000_000) == 64 * 1024
        assert get_chunk_size(50_000_000) == 64 * 1024
        assert get_chunk_size(99_999_999) == 64 * 1024

    def test_large_file_over_100mb(self):
        """Test large file returns 256KB chunk."""
        assert get_chunk_size(100_000_000) == 256 * 1024
        assert get_chunk_size(1_000_000_000) == 256 * 1024


class TestGetPermissions:
    """Tests for get_permissions function."""

    def test_get_file_permissions(self):
        """Test getting file permissions."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name
            os.chmod(temp_path, 0o644)

        try:
            perms = get_permissions(temp_path)
            assert perms == "-rw-r--r--"
        finally:
            os.unlink(temp_path)

    def test_get_directory_permissions(self):
        """Test getting directory permissions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chmod(temp_dir, 0o755)
            perms = get_permissions(temp_dir)
            assert perms == "drwxr-xr-x"

    def test_get_executable_permissions(self):
        """Test getting executable file permissions."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name
            os.chmod(temp_path, 0o755)

        try:
            perms = get_permissions(temp_path)
            assert perms == "-rwxr-xr-x"
        finally:
            os.unlink(temp_path)


class TestSetPermissions:
    """Tests for set_permissions function."""

    def test_set_read_write_permissions(self):
        """Test setting read-write permissions."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name

        try:
            set_permissions(temp_path, "-rw-r--r--")
            mode = stat.S_IMODE(os.stat(temp_path).st_mode)
            assert mode == 0o644
        finally:
            os.unlink(temp_path)

    def test_set_executable_permissions(self):
        """Test setting executable permissions."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name

        try:
            set_permissions(temp_path, "-rwxr-xr-x")
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
            set_permissions(temp_path, "-rw-r--r--")
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


class TestVerifyMasterFingerprint:
    """Tests for verify_master_fingerprint function."""

    def test_no_fingerprint_returns_true(self):
        """Returns True when no MASTER_CERT_FINGERPRINT is configured."""
        original = CONFIG.copy()
        try:
            CONFIG.pop("MASTER_CERT_FINGERPRINT", None)
            assert verify_master_fingerprint("https://192.168.1.100:8443") is True
        finally:
            set_config(original)

    def test_http_url_returns_true(self):
        """Returns True for HTTP (non-HTTPS) URL even with fingerprint set."""
        original = CONFIG.copy()
        try:
            CONFIG["MASTER_CERT_FINGERPRINT"] = "abc123"
            assert verify_master_fingerprint("http://192.168.1.100:8080") is True
        finally:
            set_config(original)

    def test_fingerprint_match_returns_true(self):
        """Returns True when fingerprint matches."""
        original = CONFIG.copy()
        try:
            CONFIG["MASTER_CERT_FINGERPRINT"] = "deadbeef" * 8
            with patch(
                "hsync_client.ssl_utils.get_server_cert_fingerprint",
                return_value="deadbeef" * 8,
            ):
                assert verify_master_fingerprint("https://192.168.1.100:8443") is True
        finally:
            set_config(original)

    def test_fingerprint_mismatch_returns_false(self):
        """Returns False when fingerprint does not match."""
        original = CONFIG.copy()
        try:
            CONFIG["MASTER_CERT_FINGERPRINT"] = "deadbeef" * 8
            with patch(
                "hsync_client.ssl_utils.get_server_cert_fingerprint",
                return_value="cafebabe" * 8,
            ):
                assert verify_master_fingerprint("https://192.168.1.100:8443") is False
        finally:
            set_config(original)

    def test_server_unreachable_returns_false(self):
        """Returns False when server certificate cannot be retrieved."""
        original = CONFIG.copy()
        try:
            CONFIG["MASTER_CERT_FINGERPRINT"] = "abc123"
            with patch(
                "hsync_client.ssl_utils.get_server_cert_fingerprint",
                return_value=None,
            ):
                assert verify_master_fingerprint("https://192.168.1.100:8443") is False
        finally:
            set_config(original)


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
