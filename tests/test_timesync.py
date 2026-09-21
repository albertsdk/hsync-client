# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for the P2P startup clock-offset probe (ADR-0023)."""

import threading
from unittest.mock import patch

from hsync_common.clock import offset_store

from hsync_client import timesync


class TestStartNtpClockOffsetProbe:
    """Tests for start_ntp_clock_offset_probe (background SNTP probe)."""

    def test_probe_runs_on_background_daemon_thread(self):
        """The probe runs on a named daemon thread, not the caller's."""
        main_thread = threading.current_thread()
        started = threading.Event()

        def fake_measure(logger=None) -> tuple[float, str | None]:
            started.set()
            return 0.0, None

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

        def blocking_measure(logger=None) -> tuple[float, str | None]:
            entered.set()
            release.wait(5.0)
            return 0.0, None

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

        def fake_measure(logger=None) -> tuple[float, str | None]:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                first_release.wait(5.0)
                return 99.0, "stale"
            return 1.0, "fresh"

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
