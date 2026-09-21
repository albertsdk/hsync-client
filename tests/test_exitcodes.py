# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Tests for process exit codes and one-shot cycle statistics.

Covers:
- ``hsync_client.exitcodes``: the code table and its priority order.
- ``hsync_client.sync._execute_parallel_transfers``: failed-transfer counting.
- ``run_sync`` mapping from a cycle verdict to a process exit code
  (automation contract documented in ``docs/scheduling.md``).
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hsync_client.exitcodes import ExitCode, exit_code_for_result
from hsync_client.sync import SyncCycleResult


class TestExitCodes:
    """The code table and the priority order of cycle verdicts."""

    def test_clean_cycle_is_ok(self):
        assert (
            exit_code_for_result(failed_transfers=0, guard_tripped=False, conflicts=0)
            == ExitCode.OK
        )

    def test_transfer_errors_outrank_guard_and_conflicts(self):
        assert (
            exit_code_for_result(failed_transfers=2, guard_tripped=True, conflicts=3)
            == ExitCode.TRANSFER_ERRORS
        )

    def test_guard_outranks_conflicts(self):
        assert (
            exit_code_for_result(failed_transfers=0, guard_tripped=True, conflicts=1)
            == ExitCode.DELETION_GUARD
        )

    def test_conflicts_reported_when_alone(self):
        assert (
            exit_code_for_result(failed_transfers=0, guard_tripped=False, conflicts=1)
            == ExitCode.CONFLICTS
        )

    def test_table_is_stable(self):
        """The numeric values are a public contract for scripts."""
        assert int(ExitCode.OK) == 0
        assert int(ExitCode.FATAL) == 1
        assert int(ExitCode.TRANSFER_ERRORS) == 2
        assert int(ExitCode.DELETION_GUARD) == 3
        assert int(ExitCode.CONFLICTS) == 4
        assert int(ExitCode.P2P_NO_PEER) == 5


class TestParallelTransferFailureCounting:
    """_execute_parallel_transfers records failures in the shared counters."""

    def _file(self, name: str) -> MagicMock:
        f = MagicMock()
        f.filepath = name
        f.size = 0
        return f

    def test_clean_transfers_leave_zero_failures(self):
        counters: dict[str, int] = {"failed": 0}

        async def _run() -> None:
            with (
                patch("hsync_client.transfer.download_file"),
                patch("hsync_client.transfer.upload_file"),
            ):
                from hsync_client.sync import _execute_parallel_transfers

                await _execute_parallel_transfers(
                    [self._file("a.txt")], [self._file("b.txt")], counters
                )

        asyncio.run(_run())
        assert counters["failed"] == 0

    def test_failed_downloads_are_counted(self):
        counters: dict[str, int] = {"failed": 0}

        def _boom(file) -> None:
            raise OSError("simulated transfer failure")

        async def _run() -> None:
            with (
                patch("hsync_client.transfer.download_file", side_effect=_boom),
                patch("hsync_client.transfer.upload_file"),
            ):
                from hsync_client.sync import _execute_parallel_transfers

                await _execute_parallel_transfers(
                    [self._file("a.txt"), self._file("c.txt")], [], counters
                )

        asyncio.run(_run())
        assert counters["failed"] == 2

    def test_counters_omitted_when_not_collecting(self):
        """Callers that do not need stats can omit the counters mapping."""

        async def _run() -> None:
            with (
                patch("hsync_client.transfer.download_file"),
                patch("hsync_client.transfer.upload_file"),
            ):
                from hsync_client.sync import _execute_parallel_transfers

                await _execute_parallel_transfers([self._file("a.txt")], [])

        asyncio.run(_run())  # must not raise


class TestOneShotExitCodeMapping:
    """run_sync maps the one-shot cycle verdict onto documented exit codes."""

    def _kwargs(self, tmp_path):
        from tests.test_main_logic_sync import _default_run_sync_kwargs

        return _default_run_sync_kwargs(tmp_path, server_url="https://localhost:8000")

    def _run_oneshot(self, tmp_path, result: SyncCycleResult) -> int:
        from hsync_client.main_logic import run_sync

        kwargs = self._kwargs(tmp_path)
        with (
            patch("hsync_client.main_logic.setup_logging", return_value=MagicMock()),
            patch("hsync_client.main_logic.get_api_key", return_value="test-key"),
            patch("hsync_client.main_logic._check_server_connectivity", return_value=True),
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.sync", new=MagicMock()),
            patch("hsync_client.main_logic.run", return_value=result),
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            return int(exc_info.value.code)

    def test_clean_cycle_exits_zero(self, tmp_path):
        result = SyncCycleResult(
            completed=True, transfers=1, failed=0, conflicts=0, guard_tripped=False
        )
        assert self._run_oneshot(tmp_path, result) == 0

    def test_transfer_errors_exit_two(self, tmp_path):
        result = SyncCycleResult(
            completed=True, transfers=3, failed=1, conflicts=0, guard_tripped=False
        )
        assert self._run_oneshot(tmp_path, result) == 2

    def test_guard_tripped_exits_three(self, tmp_path):
        result = SyncCycleResult(
            completed=True, transfers=0, failed=0, conflicts=0, guard_tripped=True
        )
        assert self._run_oneshot(tmp_path, result) == 3

    def test_conflicts_exit_four(self, tmp_path):
        result = SyncCycleResult(
            completed=True, transfers=2, failed=0, conflicts=1, guard_tripped=False
        )
        assert self._run_oneshot(tmp_path, result) == 4


class TestSyncFatalMapsToExitOne:
    """A fatal sync failure is reported as exit code 1 without a traceback."""

    def test_network_error_exits_one(self, tmp_path):
        from hsync_client.exceptions import NetworkError
        from hsync_client.main_logic import run_sync
        from tests.test_main_logic_sync import _default_run_sync_kwargs

        kwargs = _default_run_sync_kwargs(tmp_path, server_url="https://localhost:8000")
        with (
            patch("hsync_client.main_logic.setup_logging", return_value=MagicMock()),
            patch("hsync_client.main_logic.get_api_key", return_value="test-key"),
            patch("hsync_client.main_logic._check_server_connectivity", return_value=True),
            patch("hsync_client.main_logic.set_config"),
            patch("hsync_client.main_logic.set_debug"),
            patch("hsync_client.main_logic.sync", new=MagicMock()),
            patch("hsync_client.main_logic.run", side_effect=NetworkError(message="down")),
        ):
            with pytest.raises(SystemExit) as exc_info:
                run_sync(**kwargs)
            assert int(exc_info.value.code) == 1
