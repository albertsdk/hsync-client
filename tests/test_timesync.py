# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the P2P startup clock-offset probe (ADR-0023)."""

import threading
import time
from unittest.mock import patch

import pytest
from hsync_common.clock import offset_store

from hsync_client import timesync


class TestRendezvousClockOffset:
    """The ack echo is a last-resort time reference (ADR-0055)."""

    @staticmethod
    def _now_z(offset_seconds: float = 0.0) -> str:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(time.time() + offset_seconds, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def test_establishes_trust_for_a_degraded_host(self):
        from datetime import UTC, datetime

        offset_store.clear()
        now = datetime.now(UTC).replace(microsecond=0)
        recorded = timesync.record_rendezvous_clock_offset(
            now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            sent_at=now.timestamp(),
            received_at=now.timestamp() + 0.2,
        )
        assert recorded is True
        assert offset_store.is_trusted() is True
        assert abs(offset_store.get()) < 2.0
        # Half the round-trip plus the one-second quantization of the echo.
        assert offset_store.uncertainty() == pytest.approx(0.6, abs=0.05)

    def test_never_refines_a_better_source(self):
        offset_store.clear()
        offset_store.record(0.25, rtt=0.02)
        assert (
            timesync.record_rendezvous_clock_offset(
                self._now_z(), sent_at=time.time(), received_at=time.time()
            )
            is False
        )
        assert offset_store.get() == 0.25

    def test_implausible_node_clock_is_refused(self):
        offset_store.clear()
        assert (
            timesync.record_rendezvous_clock_offset(
                "2000-01-01T00:00:00Z", sent_at=time.time(), received_at=time.time()
            )
            is False
        )
        assert offset_store.is_trusted() is False

    @pytest.mark.parametrize("value", [None, 12.0, "2026-01-01 00:00:00", "garbage"])
    def test_unusable_timestamp_is_ignored(self, value):
        offset_store.clear()
        assert (
            timesync.record_rendezvous_clock_offset(
                value, sent_at=time.time(), received_at=time.time()
            )
            is False
        )
        assert offset_store.is_trusted() is False


class TestStartNtpClockOffsetProbe:
    """Tests for start_ntp_clock_offset_probe (background SNTP probe)."""

    def test_probe_runs_on_background_daemon_thread(self):
        """The probe runs on a named daemon thread, not the caller's."""
        main_thread = threading.current_thread()
        started = threading.Event()

        def fake_measure(logger=None) -> tuple[float, float, str | None]:
            started.set()
            return 0.0, 0.0, None

        with patch.object(timesync, "_measure_ntp_offset", side_effect=fake_measure):
            thread = timesync.start_ntp_clock_offset_probe()
            try:
                assert thread.daemon is True
                assert thread.name == "hsync-ntp-probe"
                assert thread is not main_thread
                assert started.wait(2.0), "probe thread never started"
            finally:
                thread.join(2.0)
                assert not thread.is_alive()

    def test_start_returns_immediately_even_if_probe_blocks(self):
        """Startup is never blocked by a slow/unreachable NTP probe."""
        release = threading.Event()
        entered = threading.Event()

        def blocking_measure(logger=None) -> tuple[float, float, str | None]:
            entered.set()
            release.wait(5.0)
            return 0.0, 0.0, None

        with patch.object(timesync, "_measure_ntp_offset", side_effect=blocking_measure):
            thread = timesync.start_ntp_clock_offset_probe()
            try:
                assert entered.wait(2.0), "probe thread never entered the blocking measure"
                assert thread.is_alive()
            finally:
                release.set()
                thread.join(2.0)
                assert not thread.is_alive()

    def test_stale_probe_does_not_overwrite_newer_result(self):
        """A slower older worker must not replace a newer probe's offset."""
        first_entered = threading.Event()
        first_release = threading.Event()
        calls = 0

        def fake_measure(logger=None) -> tuple[float, float, str | None]:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                first_release.wait(5.0)
                return 99.0, 0.0, "stale"
            return 1.0, 0.0, "fresh"

        with patch.object(timesync, "_measure_ntp_offset", side_effect=fake_measure):
            stale = timesync.start_ntp_clock_offset_probe()
            assert first_entered.wait(2.0)
            fresh = timesync.start_ntp_clock_offset_probe()
            try:
                fresh.join(2.0)
                assert not fresh.is_alive()
                assert offset_store.get() == 1.0
            finally:
                first_release.set()
                stale.join(2.0)
                assert not stale.is_alive()
            assert offset_store.get() == 1.0
