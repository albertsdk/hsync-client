# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Bandwidth limiter: the configured rate is what the stream actually gets."""

from __future__ import annotations

import pytest

from hsync_client.utils import throttle


@pytest.fixture
def clock(monkeypatch):
    """Drive the limiter from a simulated monotonic clock."""
    state = {"now": 1000.0}
    monkeypatch.setattr(throttle.time, "monotonic", lambda: state["now"])
    throttle.reset_bandwidth_throttle()
    yield state
    throttle.reset_bandwidth_throttle()


def _stream(clock, limit, chunk, count, key="upload"):
    """Hand out *count* chunks, sleeping as the limiter asks; return arrivals."""
    arrivals = []
    for _ in range(count):
        delay = throttle.get_throttle_delay(chunk, limit, key)
        arrivals.append(clock["now"])
        clock["now"] += delay
    return arrivals


def _bytes_in_window(arrivals, chunk, start, end):
    return chunk * sum(1 for t in arrivals if start <= t < end)


class TestPacer:
    def test_no_limit_is_a_noop(self, clock):
        assert throttle.get_throttle_delay(4096, None, "upload") == 0.0
        assert throttle.get_throttle_delay(4096, 0, "upload") == 0.0

    def test_the_rate_applies_from_the_first_chunk(self, clock):
        """No free opening burst: 256 KiB at 1 MiB/s waits exactly 0.25 s."""
        assert throttle.get_throttle_delay(256 * 1024, 1024 * 1024, "upload") == pytest.approx(0.25)

    def test_a_second_of_traffic_never_exceeds_the_limit(self, clock):
        limit = 1024 * 1024
        chunk = 256 * 1024
        arrivals = _stream(clock, limit, chunk, 40)

        for window_start in range(0, 8):
            moved = _bytes_in_window(
                arrivals, chunk, arrivals[0] + window_start, arrivals[0] + window_start + 1
            )
            assert moved <= limit

    def test_the_sustained_rate_matches_the_limit(self, clock):
        limit = 512 * 1024
        chunk = 128 * 1024
        arrivals = _stream(clock, limit, chunk, 33)

        elapsed = arrivals[-1] - arrivals[0]
        assert elapsed == pytest.approx(32 * chunk / limit)

    def test_idle_time_grants_no_credit(self, clock):
        """The old bucket banked a 2 s burst while idle; the pacer does not."""
        limit = 1024 * 1024
        chunk = 256 * 1024
        _stream(clock, limit, chunk, 4)

        clock["now"] += 600.0  # long pause between files
        resumed = _stream(clock, limit, chunk, 4)

        gaps = [b - a for a, b in zip(resumed, resumed[1:], strict=False)]
        assert all(gap == pytest.approx(0.25) for gap in gaps)

    def test_directions_do_not_share_the_budget(self, clock):
        limit = 1024 * 1024
        chunk = 256 * 1024

        assert throttle.get_throttle_delay(chunk, limit, "upload") == pytest.approx(0.25)
        assert throttle.get_throttle_delay(chunk, limit, "download") == pytest.approx(0.25)

    def test_a_chunk_bigger_than_a_second_waits_in_proportion(self, clock):
        assert throttle.get_throttle_delay(2048, 1024, "download") == pytest.approx(2.0)

    def test_reset_forgets_the_schedule(self, clock):
        throttle.get_throttle_delay(1024 * 1024, 1024 * 1024, "upload")
        throttle.reset_bandwidth_throttle()

        assert throttle.get_throttle_delay(1024, 1024 * 1024, "upload") == pytest.approx(1 / 1024)
