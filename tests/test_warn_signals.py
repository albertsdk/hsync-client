# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The engine warnings a user must see (the structured ``UiEventName.warning``).

A non-fatal condition the sync keeps running through stayed in the log tail
before: a stalled safety net, a shared folder that is gone, free space below the
reserve, unreadable files, dropped real-time events, a file that never
stabilises. Each one explains a behaviour the user cannot otherwise understand,
so each emits a warning with a stable ``source`` (and ``reason="transient"`` when
it heals itself).
"""

from unittest.mock import patch

import pytest
from hsync_common.disk import FreeSpaceCheck
from hsync_common.models.file import File

from hsync_client import sync as sync_mod
from hsync_client.event_queue import EventQueue


def _space(ok: bool, free: int = 1024, required: int = 4096) -> FreeSpaceCheck:
    return FreeSpaceCheck(ok=ok, free_bytes=free, required_bytes=required, error=None)


class TestDiskReserveWarning:
    """Free space below the reserve must reach the user before files are skipped."""

    def test_below_the_reserve_emits_a_persistent_warning(self, tmp_path):
        with (
            patch.object(sync_mod, "CONFIG", {"MIN_FREE_DISK_SPACE": 1024}),
            patch.object(sync_mod, "check_free_space", return_value=_space(False)),
            patch.object(sync_mod, "emit_warning") as warn,
        ):
            sync_mod._warn_disk_reserve(str(tmp_path))

        warn.assert_called_once()
        assert warn.call_args.kwargs["source"] == "storage"
        assert warn.call_args.kwargs["reason"] == "disk_reserve"

    def test_enough_space_or_a_disabled_guard_is_silent(self, tmp_path):
        with (
            patch.object(sync_mod, "CONFIG", {"MIN_FREE_DISK_SPACE": 1024}),
            patch.object(sync_mod, "check_free_space", return_value=_space(True)),
            patch.object(sync_mod, "emit_warning") as warn,
        ):
            sync_mod._warn_disk_reserve(str(tmp_path))

        with (
            patch.object(sync_mod, "CONFIG", {"MIN_FREE_DISK_SPACE": 0}),
            patch.object(sync_mod, "check_free_space") as probe,
            patch.object(sync_mod, "emit_warning") as warn_disabled,
        ):
            sync_mod._warn_disk_reserve(str(tmp_path))

        warn.assert_not_called()
        warn_disabled.assert_not_called()
        probe.assert_not_called()


class TestMissingBucketWarning:
    def test_a_vanished_shared_folder_is_reported(self, tmp_path):
        with patch.object(sync_mod, "emit_warning") as warn:
            sync_mod._warn_missing_bucket(str(tmp_path))

        warn.assert_called_once()
        assert warn.call_args.kwargs["source"] == "path"
        assert warn.call_args.kwargs["reason"] == "missing"
        assert str(tmp_path) in warn.call_args.args[0]


class TestUnreadableFilesWarning:
    def test_a_batch_with_unreadable_files_reports_once(self):
        with patch.object(sync_mod, "emit_warning") as warn:
            sync_mod._warn_unreadable_files(3)

        warn.assert_called_once()
        assert warn.call_args.kwargs["source"] == "scan"
        assert warn.call_args.kwargs["reason"] == "transient"

    def test_a_clean_batch_is_silent(self):
        with patch.object(sync_mod, "emit_warning") as warn:
            sync_mod._warn_unreadable_files(0)

        warn.assert_not_called()

    def test_the_counter_is_monotonic(self):
        before = sync_mod._unreadable_total
        sync_mod._count_unreadable_file()

        assert sync_mod._unreadable_total == before + 1


class TestSafetyNetWarning:
    def test_a_stalled_cycle_is_reported_to_the_ui(self):
        """The only recovery for missed watcher events must not fail silently."""
        import asyncio as aio
        from contextlib import suppress
        from time import monotonic

        async def _run():
            with (
                patch.object(sync_mod, "CONFIG", {"RESYNC": 0.01}),
                patch.object(sync_mod, "emit_warning") as warn,
            ):
                sync_mod._sync_cycle_active.set()
                with sync_mod._cycle_bookkeeping_lock:
                    sync_mod._sync_cycle_started_at = monotonic() - 60.0
                task = aio.create_task(sync_mod.periodic_resync())
                await aio.sleep(0.1)
                task.cancel()
                with suppress(aio.CancelledError):
                    await task
                return warn

        try:
            warn = aio.run(_run())
        finally:
            sync_mod._sync_cycle_active.clear()
            with sync_mod._cycle_bookkeeping_lock:
                sync_mod._sync_cycle_started_at = None

        warn.assert_called()
        assert warn.call_args.kwargs["source"] == "sync"
        assert warn.call_args.kwargs["reason"] == "safety_net_stalled"


class TestTransferWarnings:
    def test_a_download_skipped_for_space_emits_a_terminal_failure(self, tmp_path):
        from hsync_client import transfer as transfer_mod

        file = File(filepath="big.bin", size=10, sha256sum="a" * 64)
        with (
            patch.object(transfer_mod, "CONFIG", {"MIN_FREE_DISK_SPACE": 1024}),
            patch.object(transfer_mod, "check_free_space", return_value=_space(False)),
            patch.object(transfer_mod, "emit_event") as emitted,
        ):
            assert transfer_mod._has_download_space(file, str(tmp_path / "big.bin"), 0) is False

        emitted.assert_called_once()
        assert emitted.call_args.args[0] == "transfer_failed"
        assert emitted.call_args.kwargs["op"] == "download"
        assert "disk space" in emitted.call_args.kwargs["message"]

    def test_an_upload_that_never_stabilises_is_a_transient_warning(self):
        from hsync_client import transfer as transfer_mod

        file = File(filepath="/bucket/live.log", size=10, sha256sum="b" * 64)
        with (
            patch.object(transfer_mod, "CONFIG", {"UPLOAD_MAX_RESCHEDULES": 0}),
            patch.object(transfer_mod, "emit_warning") as warn,
        ):
            transfer_mod._reschedule_upload(file, reschedules_left=0)

        warn.assert_called_once()
        assert warn.call_args.kwargs["source"] == "upload"
        assert warn.call_args.kwargs["reason"] == "transient"


class TestEventQueueWarning:
    @pytest.mark.asyncio
    async def test_a_dropped_event_is_a_transient_warning(self):
        queue = EventQueue(websocket=None)
        with patch("hsync_client.event_queue.emit_warning") as warn:
            queue._drop_event({"name": "modified"})

        assert queue.dropped == 1
        warn.assert_called_once()
        assert warn.call_args.kwargs["source"] == "events"
        assert warn.call_args.kwargs["reason"] == "transient"
