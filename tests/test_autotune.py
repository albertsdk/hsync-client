# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for the autotune module."""

import pytest

from hsync_client.autotune import (
    SAFE,
    BwClass,
    NetClass,
    QueueProfile,
    StorageType,
    classify_bandwidth,
    classify_network,
    derive_concurrency,
    derive_profile,
    derive_retry,
    derive_timeout,
    detect_storage_type,
    measure_bandwidth_active,
    measure_rtt,
    profile_transfer_queue,
)

# ── Test helpers ─────────────────────────────────────────────────────────


class _FakeFile:
    def __init__(self, size: int):
        self.size = size


# ── classify_network ─────────────────────────────────────────────────────


class TestClassifyNetwork:
    def test_lan(self):
        assert classify_network(30) == NetClass.LAN

    def test_broadband(self):
        assert classify_network(80) == NetClass.BROADBAND

    def test_mobile(self):
        assert classify_network(200) == NetClass.MOBILE

    def test_unknown_on_none(self):
        assert classify_network(None) == NetClass.UNKNOWN

    def test_mobile_override(self):
        assert classify_network(30, is_mobile=True) == NetClass.MOBILE

    def test_boundary_50(self):
        assert classify_network(50) == NetClass.LAN

    def test_boundary_150(self):
        assert classify_network(150) == NetClass.BROADBAND


# ── classify_bandwidth ───────────────────────────────────────────────────


class TestClassifyBandwidth:
    def test_unknown_all_none(self):
        assert classify_bandwidth(None, None) == BwClass.UNKNOWN

    def test_ten_g_by_mbps(self):
        assert classify_bandwidth(3000, None) == BwClass.TEN_G

    def test_ten_g_by_nic(self):
        assert classify_bandwidth(None, 10000) == BwClass.TEN_G

    def test_gigabit(self):
        assert classify_bandwidth(900, None) == BwClass.GIGABIT

    def test_mid(self):
        assert classify_bandwidth(100, None) == BwClass.MID

    def test_low(self):
        assert classify_bandwidth(20, None) == BwClass.LOW

    def test_active_fallback(self):
        assert classify_bandwidth(None, None, active_mbps=2000) == BwClass.TEN_G

    def test_passive_preferred_over_active(self):
        assert classify_bandwidth(100, None, active_mbps=2000) == BwClass.MID


# ── profile_transfer_queue ────────────────────────────────────────────────


class TestProfileTransferQueue:
    def test_mixed_on_empty(self):
        assert profile_transfer_queue([]) == QueueProfile.MIXED

    def test_many_small(self):
        files = [_FakeFile(500_000) for _ in range(100)]
        assert profile_transfer_queue(files) == QueueProfile.MANY_SMALL

    def test_few_large(self):
        files = [_FakeFile(300_000_000) for _ in range(10)]
        assert profile_transfer_queue(files) == QueueProfile.FEW_LARGE

    def test_mixed(self):
        files = [_FakeFile(500_000) for _ in range(30)] + [
            _FakeFile(500_000_000) for _ in range(10)
        ]
        assert profile_transfer_queue(files) == QueueProfile.MIXED


# ── derive_timeout ────────────────────────────────────────────────────────


class TestDeriveTimeout:
    def test_unknown(self):
        assert derive_timeout(None, NetClass.UNKNOWN, 1, 300) == SAFE.http_timeout

    def test_mobile_60s_regardless_of_rtt(self):
        for rtt in (None, 30, 80, 200):
            assert derive_timeout(rtt, NetClass.MOBILE, 1, 300) == 60

    def test_lan_30s(self):
        assert derive_timeout(20, NetClass.LAN, 1, 300) == 30

    def test_broadband_45s(self):
        assert derive_timeout(80, NetClass.BROADBAND, 1, 300) == 45


# ── derive_retry ──────────────────────────────────────────────────────────


class TestDeriveRetry:
    def test_unknown_returns_safe(self):
        result = derive_retry(None, NetClass.UNKNOWN, 1, 60)
        assert result == (SAFE.retry_min_wait, SAFE.retry_max_wait, SAFE.reconnect_delay)

    def test_lan_returns_low_values(self):
        min_w, max_w, rec = derive_retry(20, NetClass.LAN, 1, 60)
        assert min_w == 6  # round(5 * 1.2) = 6
        assert max_w == 12  # round(6 * 2) = 12
        assert rec == 6

    def test_high_rtt_scales_up(self):
        min_w, max_w, rec = derive_retry(200, NetClass.MOBILE, 1, 60)
        assert min_w == 15  # round(5 * 3) = 15
        assert max_w == 30  # round(15 * 2) = 30
        assert rec == 15

    def test_capped_at_factor_3(self):
        min_w, max_w, rec = derive_retry(500, NetClass.MOBILE, 1, 60)
        assert min_w == 15
        assert max_w == 30

    def test_mobile_no_rtt(self):
        result = derive_retry(None, NetClass.MOBILE, 1, 60)
        assert result == (10, 20, 10)


# ── derive_concurrency ───────────────────────────────────────────────────


class TestDeriveConcurrency:
    def test_hdd_default(self):
        assert derive_concurrency(StorageType.HDD, BwClass.MID, QueueProfile.MIXED, 1, 20) == 5

    def test_ssd_base(self):
        assert derive_concurrency(StorageType.SSD, BwClass.MID, QueueProfile.MIXED, 1, 20) == 10

    def test_nvme_base(self):
        assert derive_concurrency(StorageType.NVME, BwClass.MID, QueueProfile.MIXED, 1, 20) == 15

    def test_nvme_ten_g(self):
        assert derive_concurrency(StorageType.NVME, BwClass.TEN_G, QueueProfile.MIXED, 1, 20) == 20

    def test_nvme_ten_g_many_small(self):
        r = derive_concurrency(StorageType.NVME, BwClass.TEN_G, QueueProfile.MANY_SMALL, 1, 20)
        assert r == 20  # 15 + 5 + 3 = 23 → clamped to 20

    def test_low_bandwidth_caps_concurrency(self):
        assert derive_concurrency(StorageType.SSD, BwClass.LOW, QueueProfile.MIXED, 1, 20) == 4

    def test_hdd_few_large(self):
        assert derive_concurrency(StorageType.HDD, BwClass.MID, QueueProfile.FEW_LARGE, 1, 20) == 3


# ── derive_profile ───────────────────────────────────────────────────────


class TestDeriveProfile:
    def test_lan_workstation(self):
        from hsync_client.autotune import ProbeResults

        results = ProbeResults(
            rtt_ms=5,
            net_class=NetClass.LAN,
            bw_class=BwClass.TEN_G,
            storage_type=StorageType.NVME,
            queue_profile=QueueProfile.MANY_SMALL,
        )
        profile = derive_profile(results)
        assert profile.http_timeout == 30
        assert profile.retry_min_wait == 5
        assert profile.retry_max_wait == 10
        assert profile.reconnect_delay == 5
        assert profile.concurrency == 20  # 15 + 5 + 3 = 23 → 20
        assert "(auto)" in profile.notes

    def test_remote_high_latency(self):
        from hsync_client.autotune import ProbeResults

        results = ProbeResults(
            rtt_ms=300,
            net_class=NetClass.MOBILE,
            bw_class=BwClass.MID,
            storage_type=StorageType.SSD,
            queue_profile=QueueProfile.MIXED,
        )
        profile = derive_profile(results)
        assert profile.http_timeout == 60
        assert profile.retry_min_wait == 15
        assert profile.retry_max_wait == 30
        assert profile.concurrency == 10

    def test_all_probes_failed(self):
        from hsync_client.autotune import ProbeResults

        results = ProbeResults(
            rtt_ms=None,
            net_class=NetClass.UNKNOWN,
            bw_class=BwClass.UNKNOWN,
            storage_type=StorageType.UNKNOWN,
            queue_profile=QueueProfile.MIXED,
        )
        profile = derive_profile(results)
        assert profile.http_timeout == SAFE.http_timeout
        assert profile.retry_min_wait == SAFE.retry_min_wait
        assert profile.retry_max_wait == SAFE.retry_max_wait
        assert profile.reconnect_delay == SAFE.reconnect_delay
        assert profile.concurrency == SAFE.concurrency


# ── update_bw_ema ─────────────────────────────────────────────────────────


class TestUpdateBwEma:
    def test_first_sample_sets_ema(self):
        import hsync_client.autotune as autotune

        autotune._bw_ema = None
        autotune.update_bw_ema(10_000_000, 1.0)  # 10 MB in 1s = 80 Mbps
        assert autotune._bw_ema == pytest.approx(80.0, rel=0.01)

    def test_second_sample_updates_ema(self):
        import hsync_client.autotune as autotune

        autotune._bw_ema = None
        autotune.update_bw_ema(10_000_000, 1.0)  # 80 Mbps
        autotune.update_bw_ema(20_000_000, 1.0)  # 160 Mbps
        expected = 0.3 * 160 + 0.7 * 80  # 104
        assert autotune._bw_ema == pytest.approx(expected, rel=0.01)

    def test_zero_elapsed_ignored(self):
        import hsync_client.autotune as autotune

        autotune._bw_ema = 100.0
        autotune.update_bw_ema(10_000_000, 0)
        assert autotune._bw_ema == 100.0


# ── Thread-safety tests ──────────────────────────────────────────────────


class TestThreadSafety:
    def test_derive_retry_concurrent(self):
        import threading

        results = []

        def _call():
            for _ in range(100):
                r = derive_retry(50, NetClass.LAN, 1, 60)
                results.append(r)

        threads = [threading.Thread(target=_call) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for r in results:
            assert r[2] >= 1


# ── Probe tests with fakes ────────────────────────────────────────────────


class TestMeasureRtt:
    """RTT probe: socket stubs, failure → None."""

    def test_returns_none_on_connection_failure(self, monkeypatch):
        def _fail(*_args, **_kw):
            raise OSError("connection refused")

        monkeypatch.setattr("hsync_client.autotune.create_connection", _fail)
        assert measure_rtt("localhost", 9999) is None

    def test_returns_median_of_samples(self, monkeypatch):
        """Stub create_connection to return instantly; RTT should be ~0."""
        from contextlib import contextmanager

        @contextmanager
        def _instant(*_args, **_kw):
            yield

        monkeypatch.setattr("hsync_client.autotune.create_connection", _instant)
        rtt = measure_rtt("localhost", 80, samples=3)
        assert rtt is not None
        assert rtt >= 0


class TestDetectStorageType:
    """Storage probe: failure → UNKNOWN, never raises."""

    def test_returns_unknown_on_nonexistent_path(self):
        result = detect_storage_type("/nonexistent/path/that/does/not/exist")
        assert result == StorageType.UNKNOWN

    def test_returns_unknown_on_permission_error(self, monkeypatch, tmp_path):
        """Simulate a permission error reading sysfs."""
        from pathlib import Path

        def _raise_oserror(*_args, **_kw):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", _raise_oserror)
        result = detect_storage_type(str(tmp_path))
        assert result == StorageType.UNKNOWN


class TestMeasureBandwidthActive:
    """Active bandwidth probe: failure → None."""

    def test_returns_none_on_request_failure(self, monkeypatch):
        def _fail(*_args, **_kw):
            raise Exception("network error")

        monkeypatch.setattr("requests.get", _fail)
        assert measure_bandwidth_active("http://localhost:9999") is None


# ── Precedence tests (CLI > env > autotune > default) ─────────────────────


class TestPrecedence:
    """Verify the resolver chain: CLI > env > autotune > default."""

    def test_http_timeout_cli_beats_env_and_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_http_timeout_value

        monkeypatch.setenv("HSYNC_HTTP_TIMEOUT", "45")
        get_env_settings.cache_clear()
        assert _get_http_timeout_value(cli_value=10, autotune=60) == 10

    def test_http_timeout_env_beats_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_http_timeout_value

        monkeypatch.setenv("HSYNC_HTTP_TIMEOUT", "45")
        get_env_settings.cache_clear()
        assert _get_http_timeout_value(cli_value=None, autotune=60) == 45

    def test_http_timeout_autotune_beats_default(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_http_timeout_value

        monkeypatch.delenv("HSYNC_HTTP_TIMEOUT", raising=False)
        get_env_settings.cache_clear()
        assert _get_http_timeout_value(cli_value=None, autotune=60) == 60

    def test_http_timeout_default_when_all_none(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_http_timeout_value

        monkeypatch.delenv("HSYNC_HTTP_TIMEOUT", raising=False)
        get_env_settings.cache_clear()
        from hsync_client.constants import HTTP_TIMEOUT_DEFAULT

        assert _get_http_timeout_value(cli_value=None, autotune=None) == HTTP_TIMEOUT_DEFAULT

    def test_reconnect_delay_cli_beats_env_and_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_reconnect_delay_value

        monkeypatch.setenv("HSYNC_RECONNECT_DELAY", "20")
        get_env_settings.cache_clear()
        assert _get_reconnect_delay_value(cli_value=3, autotune=10) == 3

    def test_reconnect_delay_env_beats_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_reconnect_delay_value

        monkeypatch.setenv("HSYNC_RECONNECT_DELAY", "20")
        get_env_settings.cache_clear()
        assert _get_reconnect_delay_value(cli_value=None, autotune=10) == 20

    def test_reconnect_delay_autotune_beats_default(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_reconnect_delay_value

        monkeypatch.delenv("HSYNC_RECONNECT_DELAY", raising=False)
        get_env_settings.cache_clear()
        assert _get_reconnect_delay_value(cli_value=None, autotune=10) == 10

    def test_max_concurrent_transfers_cli_beats_env_and_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_max_concurrent_transfers_value

        monkeypatch.setenv("HSYNC_MAX_CONCURRENT_TRANSFERS", "8")
        get_env_settings.cache_clear()
        assert _get_max_concurrent_transfers_value(cli_value=2, autotune=15) == 2

    def test_max_concurrent_transfers_env_beats_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_max_concurrent_transfers_value

        monkeypatch.setenv("HSYNC_MAX_CONCURRENT_TRANSFERS", "8")
        get_env_settings.cache_clear()
        assert _get_max_concurrent_transfers_value(cli_value=None, autotune=15) == 8

    def test_max_concurrent_transfers_autotune_beats_default(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_max_concurrent_transfers_value

        monkeypatch.delenv("HSYNC_MAX_CONCURRENT_TRANSFERS", raising=False)
        get_env_settings.cache_clear()
        assert _get_max_concurrent_transfers_value(cli_value=None, autotune=15) == 15

    def test_transfer_retry_min_wait_env_beats_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_transfer_retry_min_wait_value

        monkeypatch.setenv("HSYNC_TRANSFER_RETRY_MIN_WAIT", "7.5")
        get_env_settings.cache_clear()
        assert _get_transfer_retry_min_wait_value(autotune=5) == 7.5

    def test_transfer_retry_min_wait_autotune_beats_default(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_transfer_retry_min_wait_value

        monkeypatch.delenv("HSYNC_TRANSFER_RETRY_MIN_WAIT", raising=False)
        get_env_settings.cache_clear()
        assert _get_transfer_retry_min_wait_value(autotune=5) == 5.0

    def test_transfer_retry_min_wait_default_when_all_none(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_transfer_retry_min_wait_value

        monkeypatch.delenv("HSYNC_TRANSFER_RETRY_MIN_WAIT", raising=False)
        get_env_settings.cache_clear()
        from hsync_client.constants import TRANSFER_RETRY_MIN_WAIT_DEFAULT

        assert _get_transfer_retry_min_wait_value(autotune=None) == TRANSFER_RETRY_MIN_WAIT_DEFAULT

    def test_transfer_retry_max_wait_env_beats_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_transfer_retry_max_wait_value

        monkeypatch.setenv("HSYNC_TRANSFER_RETRY_MAX_WAIT", "25.0")
        get_env_settings.cache_clear()
        assert _get_transfer_retry_max_wait_value(autotune=10) == 25.0

    def test_transfer_retry_max_wait_autotune_beats_default(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_transfer_retry_max_wait_value

        monkeypatch.delenv("HSYNC_TRANSFER_RETRY_MAX_WAIT", raising=False)
        get_env_settings.cache_clear()
        assert _get_transfer_retry_max_wait_value(autotune=30) == 30.0

    def test_transfer_retry_max_wait_default_when_all_none(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.main_logic import _get_transfer_retry_max_wait_value

        monkeypatch.delenv("HSYNC_TRANSFER_RETRY_MAX_WAIT", raising=False)
        get_env_settings.cache_clear()
        from hsync_client.constants import TRANSFER_RETRY_MAX_WAIT_DEFAULT

        assert _get_transfer_retry_max_wait_value(autotune=None) == TRANSFER_RETRY_MAX_WAIT_DEFAULT


# ── Integration tests ─────────────────────────────────────────────────────


class TestIntegration:
    """Integration: derive_profile produces correct storage_type and notes."""

    def test_storage_type_propagated_to_profile(self):
        """TuningProfile.storage_type must match ProbeResults.storage_type."""
        from hsync_client.autotune import ProbeResults

        for st in (StorageType.HDD, StorageType.SSD, StorageType.NVME, StorageType.UNKNOWN):
            results = ProbeResults(
                rtt_ms=10,
                net_class=NetClass.LAN,
                bw_class=BwClass.MID,
                storage_type=st,
                queue_profile=QueueProfile.MIXED,
            )
            profile = derive_profile(results)
            assert profile.storage_type == st

    def test_notes_contain_derived_values(self):
        """The startup log line must contain the derived values."""
        from hsync_client.autotune import ProbeResults

        results = ProbeResults(
            rtt_ms=5,
            net_class=NetClass.LAN,
            bw_class=BwClass.TEN_G,
            storage_type=StorageType.NVME,
            queue_profile=QueueProfile.MANY_SMALL,
        )
        profile = derive_profile(results)
        assert "rtt=5ms" in profile.notes
        assert "bw=ten_g" in profile.notes
        assert "storage=nvme" in profile.notes
        assert "timeout=30s" in profile.notes
        assert "(auto)" in profile.notes

    def test_safe_profile_has_unknown_storage(self):
        """SAFE fallback must have StorageType.UNKNOWN."""
        assert SAFE.storage_type == StorageType.UNKNOWN

    def test_bw_ema_live_read(self):
        """Reading _bw_ema via module attribute reflects updates (Bug 4 regression)."""
        import hsync_client.autotune as autotune_mod

        autotune_mod._bw_ema = None
        autotune_mod.update_bw_ema(10_000_000, 1.0)  # 80 Mbps
        # Reading via module attribute must see the updated value
        assert autotune_mod._bw_ema is not None
        assert autotune_mod._bw_ema == pytest.approx(80.0, rel=0.01)
        # Cleanup
        autotune_mod._bw_ema = None
