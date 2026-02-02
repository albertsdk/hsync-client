# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Tests for parallel worker optimization in sync.py.
"""

import os
from unittest.mock import patch

from hsync_client.constants import WORKER_MEMORY_BUDGET_BYTES
from hsync_client.sync import _get_optimal_workers


class TestGetOptimalWorkers:
    """Tests for _get_optimal_workers function."""

    def test_small_file_count_uses_2_workers(self):
        """For small file counts (<10), should use at most 2 workers."""
        # Test with various small counts
        assert _get_optimal_workers(1) == 1
        assert _get_optimal_workers(2) == 2
        assert _get_optimal_workers(5) == 2
        assert _get_optimal_workers(9) == 2

    def test_medium_file_count_uses_up_to_4_workers(self):
        """For medium file counts (10-99), should use up to 4 workers."""
        # Pin CPU count so the file-count tier is what's under test, not the
        # core count of the (possibly low-core) CI runner.
        with patch("hsync_client.sync.cpu_count", return_value=8):
            assert _get_optimal_workers(10) == 4
            assert _get_optimal_workers(50) == 4
            assert _get_optimal_workers(99) == 4

    def test_large_file_count_uses_up_to_8_workers(self):
        """For large file counts (>=100), should use up to 8 workers."""
        # Pin CPU count so the file-count tier is what's under test, not the
        # core count of the (possibly low-core) CI runner.
        with patch("hsync_client.sync.cpu_count", return_value=8):
            assert _get_optimal_workers(100) == 8
            assert _get_optimal_workers(500) == 8
            assert _get_optimal_workers(1000) == 8

    def test_respects_cpu_count(self):
        """Should not exceed available CPU cores."""
        with patch("hsync_client.sync.cpu_count", return_value=2):
            # Even with many files, should cap at 2 workers
            assert _get_optimal_workers(1000) == 2

    def test_respects_file_count(self):
        """Should not exceed the number of files to process."""
        # With only 3 files, can only use 3 workers max
        assert _get_optimal_workers(3) == 2  # capped by small file logic
        # With only 5 files, env var is not set so it uses small file logic = 2
        assert _get_optimal_workers(5) == 2

    def test_environment_variable_override(self):
        """Should respect HSYNC_MAX_FILE_WORKERS environment variable."""
        with (
            patch("hsync_client.sync.cpu_count", return_value=8),
            patch.dict(os.environ, {"HSYNC_MAX_FILE_WORKERS": "6"}),
        ):
            # Should use the env value, capped by CPU and file count
            assert _get_optimal_workers(100) == 6

    def test_environment_variable_invalid(self):
        """Should fall back to defaults for invalid env values."""
        with patch.dict(os.environ, {"HSYNC_MAX_FILE_WORKERS": "invalid"}):
            # Should fall back to default calculation
            result = _get_optimal_workers(100)
            assert 1 <= result <= 8

    def test_environment_variable_exceeds_cpu(self):
        """Should cap env value at CPU count."""
        with patch("hsync_client.sync.cpu_count", return_value=4):
            with patch.dict(os.environ, {"HSYNC_MAX_FILE_WORKERS": "16"}):
                # Should cap at 4 (CPU count)
                assert _get_optimal_workers(1000) == 4

    def test_environment_variable_exceeds_files(self):
        """Should cap env value at file count."""
        with (
            patch("hsync_client.sync.cpu_count", return_value=8),
            patch.dict(os.environ, {"HSYNC_MAX_FILE_WORKERS": "10"}),
        ):
            # With only 5 files, should use 5 (env var takes precedence)
            assert _get_optimal_workers(5) == 5

    def test_cpu_count_returns_none(self):
        """Should handle cpu_count returning None."""
        with patch("hsync_client.sync.cpu_count", return_value=None):
            # Should default to 4 and work normally
            result = _get_optimal_workers(100)
            assert 1 <= result <= 8

    def test_cpu_count_raises_exception(self):
        """Should handle cpu_count raising an exception."""
        with patch("hsync_client.sync.cpu_count", side_effect=Exception("test")):
            # Should default to 4 and work normally
            result = _get_optimal_workers(100)
            assert 1 <= result <= 8


class TestMemoryAwareWorkers:
    """Tests for the cgroup memory-limit cap in ``_get_optimal_workers``."""

    # ``_get_optimal_workers`` imports both probes locally from
    # ``hsync_common.runtime``, so patching the source-module attributes is what
    # reaches the function (the local ``from ... import`` re-reads them per call).

    def test_memory_none_falls_back_to_cpu(self):
        """Bare metal (no cgroup memory limit => None) keeps the CPU-only path."""
        with (
            patch("hsync_common.runtime.get_cpu_quota_cores", return_value=8.0),
            patch("hsync_common.runtime.get_memory_limit_bytes", return_value=None),
        ):
            assert _get_optimal_workers(1000) == 8

    def test_memory_tiny_limit_caps_to_one_worker(self):
        """A memory limit below the per-worker budget collapses to 1 worker."""
        budget = WORKER_MEMORY_BUDGET_BYTES
        with (
            patch("hsync_common.runtime.get_cpu_quota_cores", return_value=8.0),
            patch(
                "hsync_common.runtime.get_memory_limit_bytes",
                return_value=budget // 2,
            ),
        ):
            assert _get_optimal_workers(1000) == 1

    def test_memory_does_not_reduce_below_cpu(self):
        """A large memory limit never reduces parallelism below the CPU cap."""
        with (
            patch("hsync_common.runtime.get_cpu_quota_cores", return_value=4.0),
            patch(
                "hsync_common.runtime.get_memory_limit_bytes",
                return_value=8 * 1024 * 1024 * 1024,  # 8 GiB
            ),
        ):
            assert _get_optimal_workers(1000) == 4

    def test_memory_medium_tier(self):
        """The memory cap applies to the medium (10-99 files) tier too."""
        budget = WORKER_MEMORY_BUDGET_BYTES
        with (
            patch("hsync_common.runtime.get_cpu_quota_cores", return_value=8.0),
            patch(
                "hsync_common.runtime.get_memory_limit_bytes",
                return_value=budget * 2,  # => 2 workers
            ),
        ):
            assert _get_optimal_workers(50) == 2

    def test_memory_small_tier_unaffected(self):
        """The <10-files 2-worker floor intentionally ignores the memory cap."""
        budget = WORKER_MEMORY_BUDGET_BYTES
        with (
            patch("hsync_common.runtime.get_cpu_quota_cores", return_value=8.0),
            patch(
                "hsync_common.runtime.get_memory_limit_bytes",
                return_value=budget // 2,  # => 1 worker
            ),
        ):
            # memory is not applied to the trivial floor (mirrors CPU today)
            assert _get_optimal_workers(5) == 2

    def test_memory_env_override_still_bounded(self):
        """HSYNC_MAX_FILE_WORKERS is bounded by the memory cap (symmetric with CPU)."""
        budget = WORKER_MEMORY_BUDGET_BYTES
        with (
            patch("hsync_common.runtime.get_cpu_quota_cores", return_value=8.0),
            patch(
                "hsync_common.runtime.get_memory_limit_bytes",
                return_value=budget // 2,  # => 1 worker
            ),
            patch.dict(os.environ, {"HSYNC_MAX_FILE_WORKERS": "6"}),
        ):
            assert _get_optimal_workers(100) == 1

    def test_memory_probe_raises_falls_back_to_cpu(self):
        """If the memory probe raises, degrade gracefully to the CPU-only path."""
        with (
            patch("hsync_common.runtime.get_cpu_quota_cores", return_value=8.0),
            patch(
                "hsync_common.runtime.get_memory_limit_bytes",
                side_effect=Exception("probe failed"),
            ),
        ):
            result = _get_optimal_workers(1000)
            assert 1 <= result <= 8
