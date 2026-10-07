# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the benchmark comparison tool's regression-decision logic.

The L1 benchmark suite is dev tooling (never collected by pytest, never run in
CI), but :func:`bench.compare.compare` is a small pure function whose verdicts
— threshold, noise floor and missing-op handling — are the easiest to get wrong
and the most worth pinning. These tests import that pure function only; they
never import ``hsync_client`` and never time anything.
"""

from bench.compare import compare


def _result(name: str, mean_ms: float, p50_ms: float) -> dict:
    return {
        "name": name,
        "count": 2000,
        "total_s": 0.0,
        "ops_per_s": 0.0,
        "mean_ms": mean_ms,
        "p50_ms": p50_ms,
        "p99_ms": p50_ms,
        "min_ms": p50_ms,
        "max_ms": p50_ms,
        "stdev_ms": 0.0,
    }


def test_identical_runs_have_no_regression() -> None:
    before = [_result("op", mean_ms=1.0, p50_ms=1.0)]
    entries = compare(before, before)
    assert len(entries) == 1
    assert entries[0].regressed is False
    assert entries[0].p50_ratio == 1.0


def test_slowdown_above_threshold_is_flagged() -> None:
    before = [_result("op", mean_ms=1.0, p50_ms=1.0)]
    after = [_result("op", mean_ms=1.5, p50_ms=1.5)]
    entries = compare(before, after, threshold=1.10)
    assert entries[0].regressed is True
    assert entries[0].p50_ratio == 1.5


def test_slowdown_below_threshold_is_not_flagged() -> None:
    before = [_result("op", mean_ms=1.0, p50_ms=1.0)]
    after = [_result("op", mean_ms=1.05, p50_ms=1.05)]
    entries = compare(before, after, threshold=1.10)
    assert entries[0].regressed is False


def test_exactly_at_threshold_is_not_flagged() -> None:
    before = [_result("op", mean_ms=1.0, p50_ms=1.0)]
    after = [_result("op", mean_ms=1.10, p50_ms=1.10)]
    entries = compare(before, after, threshold=1.10)
    assert entries[0].regressed is False


def test_sub_noise_floor_slowdown_is_ignored() -> None:
    before = [_result("op", mean_ms=0.0004, p50_ms=0.0004)]
    after = [_result("op", mean_ms=0.0012, p50_ms=0.0012)]  # 3x slower
    entries = compare(before, after, threshold=1.10, min_mean_ms=0.001)
    assert entries[0].regressed is False


def test_missing_operation_is_flagged() -> None:
    before = [_result("op", mean_ms=1.0, p50_ms=1.0)]
    entries = compare(before, [])
    assert entries[0].missing is True
    assert entries[0].regressed is True
    assert entries[0].p50_ratio is None


def test_new_operation_in_after_is_ignored() -> None:
    before = [_result("op", mean_ms=1.0, p50_ms=1.0)]
    after = [
        _result("op", mean_ms=1.0, p50_ms=1.0),
        _result("new-op", mean_ms=2.0, p50_ms=2.0),
    ]
    entries = compare(before, after)
    assert [entry.name for entry in entries] == ["op"]
    assert entries[0].regressed is False
