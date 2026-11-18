# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

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
    def test_p2p(self):
        assert classify_network(30) == NetClass.P2P

    def test_broadband(self):
        assert classify_network(80) == NetClass.BROADBAND

    def test_mobile(self):
        assert classify_network(200) == NetClass.MOBILE

    def test_unknown_on_none(self):
        assert classify_network(None) == NetClass.UNKNOWN

    def test_boundary_50(self):
        assert classify_network(50) == NetClass.P2P

    def test_boundary_150(self):
        assert classify_network(150) == NetClass.BROADBAND


# ── classify_bandwidth ───────────────────────────────────────────────────


class TestClassifyBandwidth:
    def test_unknown_all_none(self):
        assert classify_bandwidth(None) == BwClass.UNKNOWN

    def test_ten_g_by_mbps(self):
        assert classify_bandwidth(3000) == BwClass.TEN_G

    def test_gigabit(self):
        assert classify_bandwidth(900) == BwClass.GIGABIT

    def test_mid(self):
        assert classify_bandwidth(100) == BwClass.MID

    def test_low(self):
        assert classify_bandwidth(20) == BwClass.LOW

    def test_active_fallback(self):
        assert classify_bandwidth(None, active_mbps=2000) == BwClass.TEN_G

    def test_passive_preferred_over_active(self):
        assert classify_bandwidth(100, active_mbps=2000) == BwClass.MID


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

    def test_p2p_30s(self):
        assert derive_timeout(20, NetClass.P2P, 1, 300) == 30

    def test_broadband_45s(self):
        assert derive_timeout(80, NetClass.BROADBAND, 1, 300) == 45


# ── derive_retry ──────────────────────────────────────────────────────────


class TestDeriveRetry:
    def test_unknown_returns_safe(self):
        result = derive_retry(None, NetClass.UNKNOWN, 1, 60)
        assert result == (SAFE.retry_min_wait, SAFE.retry_max_wait, SAFE.reconnect_delay)

    def test_p2p_returns_low_values(self):
        min_w, max_w, rec = derive_retry(20, NetClass.P2P, 1, 60)
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
    def test_p2p_workstation(self):
        from hsync_client.autotune import ProbeResults

        results = ProbeResults(
            rtt_ms=5,
            net_class=NetClass.P2P,
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


class TestSeedBwEma:
    def test_seed_sets_ema_when_unset(self):
        import hsync_client.autotune as autotune

        autotune._bw_ema = None
        try:
            autotune.seed_bw_ema(500.0)
            assert autotune._bw_ema == 500.0
        finally:
            autotune._bw_ema = None

    def test_seed_never_overwrites_existing_ema(self):
        import hsync_client.autotune as autotune

        autotune._bw_ema = 100.0
        try:
            autotune.seed_bw_ema(500.0)
            assert autotune._bw_ema == 100.0
        finally:
            autotune._bw_ema = None

    def test_seed_ignores_nonpositive(self):
        import hsync_client.autotune as autotune

        autotune._bw_ema = None
        try:
            autotune.seed_bw_ema(0)
            assert autotune._bw_ema is None
        finally:
            autotune._bw_ema = None


# ── Thread-safety tests ──────────────────────────────────────────────────


class TestThreadSafety:
    def test_derive_retry_concurrent(self):
        import threading

        results = []

        def _call():
            for _ in range(100):
                r = derive_retry(50, NetClass.P2P, 1, 60)
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
    """Storage probe: platform parsing, walk-up, failure → UNKNOWN, never raises."""

    def test_never_raises_for_a_nonexistent_path(self):
        """The probe answers for the nearest existing ancestor, never raising."""
        assert detect_storage_type("/nonexistent/path/that/does/not/exist") in StorageType

    def test_returns_unknown_when_no_ancestor_resolves(self, monkeypatch, tmp_path):
        """A path with no block-backed ancestor (tmpfs/container overlay) is UNKNOWN."""
        mountinfo = tmp_path / "mountinfo"
        mountinfo.write_text("")
        monkeypatch.setattr("hsync_client.autotune._sysfs_block_device", lambda _st: None)
        monkeypatch.setattr("hsync_client.autotune._MOUNTINFO", mountinfo)
        assert detect_storage_type("/nonexistent/path/that/does/not/exist") == StorageType.UNKNOWN

    def test_unexpected_probe_error_returns_unknown(self, monkeypatch):
        """A probe failure degrades to UNKNOWN (HDD tier) instead of raising."""

        def _boom(_path):
            raise OSError("probe failed")

        monkeypatch.setattr("hsync_client.autotune.sys.platform", "linux")
        monkeypatch.setattr("hsync_client.autotune._detect_storage_linux", _boom)
        assert detect_storage_type("/tmp") == StorageType.UNKNOWN

    def test_unknown_platform_returns_unknown_without_probing(self, monkeypatch):
        """No probe runs on a platform without a detector."""
        monkeypatch.setattr("hsync_client.autotune.sys.platform", "aix")
        assert detect_storage_type("/tmp") == StorageType.UNKNOWN

    @pytest.mark.parametrize(
        ("name", "base"),
        [
            ("sda", "sda"),
            ("sda1", "sda"),
            ("nvme0n1", "nvme0n1"),
            ("nvme0n1p1", "nvme0n1"),
            ("mmcblk0p1", "mmcblk0"),
            ("md0p1", "md0"),
            ("dm-0", "dm-0"),
            ("loop0", "loop0"),
        ],
    )
    def test_strip_partition(self, name, base, monkeypatch):
        from hsync_client.autotune import _strip_partition

        assert _strip_partition(name) == base

    def test_mountinfo_source_uses_the_device_not_the_mount_point(self, monkeypatch):
        """Regression: mountinfo field 4 is the mount point; reading it turned
        ``/home/<user>`` into a device named after the user."""
        from hsync_client.autotune import _mountinfo_source_from_text

        # The parser is Linux-only (ioctl/mountinfo); macOS realpath follows the
        # system firmlinks, so pin it for the synthetic lines.
        monkeypatch.setattr("hsync_client.autotune.os.path.realpath", lambda p: p)
        line = "486 42 0:63 / /home/alice rw,nosuid - ecryptfs /home/alice/.Private rw"
        assert _mountinfo_source_from_text(line, "/home/alice/projects") == "/home/alice/.Private"

    def test_mountinfo_source_prefers_the_longest_mount_point(self):
        from hsync_client.autotune import _mountinfo_source_from_text

        text = "\n".join(
            [
                "1 0 8:1 / / rw - ext4 /dev/sda1 rw",
                "2 1 8:2 / /data rw - ext4 /dev/sdb1 rw",
            ]
        )
        assert _mountinfo_source_from_text(text, "/data/bucket") == "/dev/sdb1"

    def test_mountinfo_source_decodes_octal_escapes(self, monkeypatch):
        from hsync_client.autotune import _mountinfo_source_from_text

        monkeypatch.setattr("hsync_client.autotune.os.path.realpath", lambda p: p)
        line = "1 0 8:1 / /home/alice/my\\040files rw - ext4 /dev/sdc1 rw"
        assert _mountinfo_source_from_text(line, "/home/alice/my files/b") == "/dev/sdc1"

    def test_dm_resolves_to_its_first_slave(self, tmp_path, monkeypatch):
        from hsync_client.autotune import _dm_backing

        (tmp_path / "dm-0" / "slaves" / "sda1").mkdir(parents=True)
        monkeypatch.setattr("hsync_client.autotune._SYS_CLASS_BLOCK", tmp_path)
        assert _dm_backing("dm-0") == "sda"

    @pytest.mark.parametrize(
        ("device", "rotational", "expected"),
        [
            ("dm-0", "0", StorageType.SSD),
            ("sdb", "1", StorageType.HDD),
            ("nvme0n1", None, StorageType.NVME),
        ],
    )
    def test_storage_from_device(self, tmp_path, monkeypatch, device, rotational, expected):
        from hsync_client.autotune import _storage_from_device

        if rotational is not None:
            (tmp_path / device / "queue").mkdir(parents=True)
            (tmp_path / device / "queue" / "rotational").write_text(rotational + "\n")
        monkeypatch.setattr("hsync_client.autotune._SYS_CLASS_BLOCK", tmp_path)
        assert _storage_from_device(device) == expected

    def test_linux_base_device_walks_up_out_of_a_pseudo_filesystem(self, tmp_path, monkeypatch):
        """An ecryptfs home has a synthetic st_dev; the disk behind its mount
        point is the answer."""
        import os
        from pathlib import Path
        from types import SimpleNamespace

        from hsync_client.autotune import _linux_base_device

        (tmp_path / "home" / "alice" / "projects").mkdir(parents=True)
        target = tmp_path / "home" / "alice" / "projects"
        real_stat = os.stat
        synthetic = real_stat(tmp_path / "home").st_dev + 1

        def fake_stat(path, *args, **kwargs):
            if Path(path).name in ("alice", "projects"):
                return SimpleNamespace(st_dev=synthetic)
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", fake_stat)
        monkeypatch.setattr(
            "hsync_client.autotune._sysfs_block_device",
            lambda st: None if st == synthetic else "dm-0",
        )
        assert _linux_base_device(str(target)) == "dm-0"

    def test_linux_base_device_falls_back_to_the_mount_source(self, tmp_path, monkeypatch):
        """btrfs subvolumes have an anonymous st_dev; the mount source names the disk."""
        import os as _os

        from hsync_client.autotune import _linux_base_device

        target = tmp_path / "data" / "bucket"
        target.mkdir(parents=True)
        mountinfo = tmp_path / "mountinfo"
        mountinfo.write_text(f"1 0 0:44 / {_os.path.realpath(target)} rw - btrfs /dev/sda2 rw\n")
        monkeypatch.setattr("hsync_client.autotune._sysfs_block_device", lambda _st: None)
        monkeypatch.setattr("hsync_client.autotune._MOUNTINFO", mountinfo)
        assert _linux_base_device(str(target)) == "sda"

    def test_windows_class_mapping(self):
        from hsync_client.autotune import _windows_storage_class

        assert _windows_storage_class(17, False) == StorageType.NVME
        assert _windows_storage_class(11, True) == StorageType.HDD
        assert _windows_storage_class(11, False) == StorageType.SSD

    def test_windows_probe_returns_none_when_the_ioctl_layer_fails(self, monkeypatch):
        from hsync_client.autotune import _detect_storage_windows

        monkeypatch.setattr("hsync_client.autotune._windows_query_storage", lambda _p: None)
        assert _detect_storage_windows("C:\\") is None

    @pytest.mark.parametrize(
        ("info", "expected"),
        [
            ({"SolidState": True, "DeviceProtocol": "PCI-Express"}, StorageType.NVME),
            ({"SolidState": True, "DeviceProtocol": "SATA"}, StorageType.SSD),
            ({"SolidState": False}, StorageType.HDD),
            ({}, None),
        ],
    )
    def test_macos_class_mapping(self, info, expected):
        from hsync_client.autotune import _macos_storage_class

        assert _macos_storage_class(info) == expected

    def test_macos_probe_requires_a_device_node(self, monkeypatch):
        from hsync_client.autotune import _detect_storage_macos

        monkeypatch.setattr("hsync_client.autotune._macos_device_node", lambda _p: None)
        assert _detect_storage_macos("/") is None


class TestMeasureBandwidthActive:
    """Active bandwidth probe: failure → None."""

    def test_returns_none_on_request_failure(self, monkeypatch):
        from requests.exceptions import RequestException

        def _fail(*_args, **_kw):
            raise RequestException("network error")

        monkeypatch.setattr("hsync_client.utils.http_get", _fail)
        assert measure_bandwidth_active("http://localhost:9999") is None

    def test_measures_throughput_with_size_param(self, monkeypatch):
        """The probe requests ``/health?size=`` and derives Mbps from the payload."""
        captured = {}

        class _FakeResp:
            ok = True
            content = b"x" * 262144  # 256 KB

        def _fake_get(url, **kwargs):
            captured["url"] = url
            captured["params"] = kwargs.get("params")
            return _FakeResp()

        monkeypatch.setattr("hsync_client.utils.http_get", _fake_get)
        mbps = measure_bandwidth_active("http://localhost:9999")
        assert mbps is not None
        assert mbps > 0
        assert captured["url"] == "http://localhost:9999/health"
        assert captured["params"] == {"size": 262144}

    def test_returns_none_on_empty_payload(self, monkeypatch):
        """An empty payload (no size support) yields None, not a bogus 0 Mbps."""

        class _EmptyResp:
            ok = True
            content = b""

        monkeypatch.setattr("hsync_client.utils.http_get", lambda *a, **kw: _EmptyResp())
        assert measure_bandwidth_active("http://localhost:9999") is None


# ── Precedence tests (CLI > env > autotune > default) ─────────────────────


class TestPrecedence:
    """Verify the resolver chain: CLI > env > autotune > default."""

    def test_http_timeout_cli_beats_env_and_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_http_timeout_value

        monkeypatch.setenv("HSYNC_HTTP_TIMEOUT", "45")
        get_env_settings.cache_clear()
        assert _get_http_timeout_value(cli_value=10, autotune=60) == (10, "cli")

    def test_http_timeout_env_beats_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_http_timeout_value

        monkeypatch.setenv("HSYNC_HTTP_TIMEOUT", "45")
        get_env_settings.cache_clear()
        assert _get_http_timeout_value(cli_value=None, autotune=60) == (45, "env")

    def test_http_timeout_autotune_beats_default(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_http_timeout_value

        monkeypatch.delenv("HSYNC_HTTP_TIMEOUT", raising=False)
        get_env_settings.cache_clear()
        assert _get_http_timeout_value(cli_value=None, autotune=60) == (60, "autotune")

    def test_http_timeout_default_when_all_none(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_http_timeout_value

        monkeypatch.delenv("HSYNC_HTTP_TIMEOUT", raising=False)
        get_env_settings.cache_clear()
        from hsync_client.constants import HTTP_TIMEOUT_DEFAULT

        assert _get_http_timeout_value(cli_value=None, autotune=None) == (
            HTTP_TIMEOUT_DEFAULT,
            "default",
        )

    def test_reconnect_delay_cli_beats_env_and_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_reconnect_delay_value

        monkeypatch.setenv("HSYNC_RECONNECT_DELAY", "20")
        get_env_settings.cache_clear()
        assert _get_reconnect_delay_value(cli_value=3, autotune=10) == (3, "cli")

    def test_reconnect_delay_env_beats_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_reconnect_delay_value

        monkeypatch.setenv("HSYNC_RECONNECT_DELAY", "20")
        get_env_settings.cache_clear()
        assert _get_reconnect_delay_value(cli_value=None, autotune=10) == (20, "env")

    def test_reconnect_delay_autotune_beats_default(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_reconnect_delay_value

        monkeypatch.delenv("HSYNC_RECONNECT_DELAY", raising=False)
        get_env_settings.cache_clear()
        assert _get_reconnect_delay_value(cli_value=None, autotune=10) == (10, "autotune")

    def test_max_concurrent_transfers_cli_beats_env_and_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_max_concurrent_transfers_value

        monkeypatch.setenv("HSYNC_MAX_CONCURRENT_TRANSFERS", "8")
        get_env_settings.cache_clear()
        assert _get_max_concurrent_transfers_value(cli_value=2, autotune=15) == (2, "cli")

    def test_max_concurrent_transfers_env_beats_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_max_concurrent_transfers_value

        monkeypatch.setenv("HSYNC_MAX_CONCURRENT_TRANSFERS", "8")
        get_env_settings.cache_clear()
        assert _get_max_concurrent_transfers_value(cli_value=None, autotune=15) == (8, "env")

    def test_max_concurrent_transfers_autotune_beats_default(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_max_concurrent_transfers_value

        monkeypatch.delenv("HSYNC_MAX_CONCURRENT_TRANSFERS", raising=False)
        get_env_settings.cache_clear()
        assert _get_max_concurrent_transfers_value(cli_value=None, autotune=15) == (15, "autotune")

    def test_max_concurrent_transfers_default_when_no_overrides(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_max_concurrent_transfers_value
        from hsync_client.constants import MAX_CONCURRENT_TRANSFERS_DEFAULT

        monkeypatch.delenv("HSYNC_MAX_CONCURRENT_TRANSFERS", raising=False)
        get_env_settings.cache_clear()
        value, src = _get_max_concurrent_transfers_value(cli_value=None, autotune=None)
        assert src == "default"
        assert value == MAX_CONCURRENT_TRANSFERS_DEFAULT

    def test_transfer_retry_min_wait_env_beats_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_transfer_retry_min_wait_value

        monkeypatch.setenv("HSYNC_TRANSFER_RETRY_MIN_WAIT", "7.5")
        get_env_settings.cache_clear()
        assert _get_transfer_retry_min_wait_value(autotune=5) == (7.5, "env")

    def test_transfer_retry_min_wait_autotune_beats_default(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_transfer_retry_min_wait_value

        monkeypatch.delenv("HSYNC_TRANSFER_RETRY_MIN_WAIT", raising=False)
        get_env_settings.cache_clear()
        assert _get_transfer_retry_min_wait_value(autotune=5) == (5.0, "autotune")

    def test_transfer_retry_min_wait_default_when_all_none(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_transfer_retry_min_wait_value

        monkeypatch.delenv("HSYNC_TRANSFER_RETRY_MIN_WAIT", raising=False)
        get_env_settings.cache_clear()
        from hsync_client.constants import TRANSFER_RETRY_MIN_WAIT_DEFAULT

        assert _get_transfer_retry_min_wait_value(autotune=None) == (
            TRANSFER_RETRY_MIN_WAIT_DEFAULT,
            "default",
        )

    def test_transfer_retry_max_wait_env_beats_autotune(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_transfer_retry_max_wait_value

        monkeypatch.setenv("HSYNC_TRANSFER_RETRY_MAX_WAIT", "25.0")
        get_env_settings.cache_clear()
        assert _get_transfer_retry_max_wait_value(autotune=10) == (25.0, "env")

    def test_transfer_retry_max_wait_autotune_beats_default(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_transfer_retry_max_wait_value

        monkeypatch.delenv("HSYNC_TRANSFER_RETRY_MAX_WAIT", raising=False)
        get_env_settings.cache_clear()
        assert _get_transfer_retry_max_wait_value(autotune=30) == (30.0, "autotune")

    def test_transfer_retry_max_wait_default_when_all_none(self, monkeypatch):
        from hsync_client.config import get_env_settings
        from hsync_client.config_build import _get_transfer_retry_max_wait_value

        monkeypatch.delenv("HSYNC_TRANSFER_RETRY_MAX_WAIT", raising=False)
        get_env_settings.cache_clear()
        from hsync_client.constants import TRANSFER_RETRY_MAX_WAIT_DEFAULT

        assert _get_transfer_retry_max_wait_value(autotune=None) == (
            TRANSFER_RETRY_MAX_WAIT_DEFAULT,
            "default",
        )


# ── Integration tests ─────────────────────────────────────────────────────


class TestIntegration:
    """Integration: derive_profile produces correct storage_type and notes."""

    def test_storage_type_propagated_to_profile(self):
        """TuningProfile.storage_type must match ProbeResults.storage_type."""
        from hsync_client.autotune import ProbeResults

        for st in (StorageType.HDD, StorageType.SSD, StorageType.NVME, StorageType.UNKNOWN):
            results = ProbeResults(
                rtt_ms=10,
                net_class=NetClass.P2P,
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
            net_class=NetClass.P2P,
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


class TestRefreshConcurrencyFromQueue:
    """Per-cycle concurrency refresh must honor explicit CLI/env overrides."""

    @pytest.fixture(autouse=True)
    def _restore_config(self):
        """Restore CONFIG and the autotune EMA after each test."""
        import hsync_client.autotune as autotune_mod
        from hsync_client.config import CONFIG

        snapshot = dict(CONFIG)
        prev_ema = autotune_mod._bw_ema
        try:
            yield
        finally:
            CONFIG.clear()
            CONFIG.update(snapshot)
            autotune_mod._bw_ema = prev_ema

    @staticmethod
    def _file(tmp_path):
        from hsync_common.models.file import File

        f = tmp_path / "f.bin"
        f.write_bytes(b"x" * 1024)
        return File(filepath=str(f), sha256sum="a" * 64, size=1024)

    def test_explicit_override_is_preserved(self, tmp_path):
        """MAX_CONCURRENT_TRANSFERS absent from managed keys -> no rewrite."""
        from hsync_client.config import CONFIG
        from hsync_client.sync import _refresh_concurrency_from_queue

        CONFIG.clear()
        CONFIG.update(
            {
                "AUTOTUNE_ENABLED": True,
                "AUTOTUNE_MANAGED_KEYS": [],  # nothing managed -> explicit override
                "MAX_CONCURRENT_TRANSFERS": 20,
                "AUTOTUNE_STORAGE_TYPE": "ssd",
            }
        )
        before = CONFIG["MAX_CONCURRENT_TRANSFERS"]

        _refresh_concurrency_from_queue([self._file(tmp_path)], [])

        assert CONFIG["MAX_CONCURRENT_TRANSFERS"] == before == 20

    def test_managed_key_is_rewritten(self, tmp_path):
        """When managed (autotune/default provenance), the refresh may rewrite."""
        import hsync_client.autotune as autotune_mod
        from hsync_client.config import CONFIG
        from hsync_client.sync import _refresh_concurrency_from_queue

        autotune_mod._bw_ema = None  # deterministic: bandwidth UNKNOWN
        CONFIG.clear()
        CONFIG.update(
            {
                "AUTOTUNE_ENABLED": True,
                "AUTOTUNE_MANAGED_KEYS": ["MAX_CONCURRENT_TRANSFERS"],
                "MAX_CONCURRENT_TRANSFERS": 99,  # deliberately not a derived value
                "AUTOTUNE_STORAGE_TYPE": "nvme",
            }
        )

        _refresh_concurrency_from_queue([self._file(tmp_path)], [])

        # With NVME + MIXED queue the derived base is 15; the refresh must have
        # moved CONFIG away from the seeded 99.
        assert CONFIG["MAX_CONCURRENT_TRANSFERS"] != 99

    def test_autotune_disabled_is_noop(self, tmp_path):
        """--no-autotune (AUTOTUNE_ENABLED False): concurrency is never touched."""
        from hsync_client.config import CONFIG
        from hsync_client.sync import _refresh_concurrency_from_queue

        CONFIG.clear()
        CONFIG.update(
            {
                "AUTOTUNE_ENABLED": False,
                "AUTOTUNE_MANAGED_KEYS": ["MAX_CONCURRENT_TRANSFERS"],
                "MAX_CONCURRENT_TRANSFERS": 20,
                "AUTOTUNE_STORAGE_TYPE": "",
            }
        )

        _refresh_concurrency_from_queue([self._file(tmp_path)], [])

        assert CONFIG["MAX_CONCURRENT_TRANSFERS"] == 20
