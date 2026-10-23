"""Tests for the persistent transfer/realtime pools and the compressed
upload spool introduced to speed up transfers (keep-alive session reuse,
bounded realtime parallelism, memory-bounded compression)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from hsync_common.compression import decompress_data

from hsync_client import remote_websocket
from hsync_client import sync as sync_mod
from hsync_client.transfer import _compress_to_temp_file


class TestTransferExecutor:
    def test_executor_is_reused_for_same_worker_count(self):
        from hsync_client.sync import _get_transfer_executor

        executor_a = _get_transfer_executor(3)
        executor_b = _get_transfer_executor(3)
        assert executor_a is executor_b

    def test_executor_is_recreated_when_worker_count_changes(self):
        from hsync_client.sync import _get_transfer_executor

        executor_small = _get_transfer_executor(2)
        executor_large = _get_transfer_executor(4)
        assert executor_small is not executor_large
        assert executor_large._max_workers == 4

    def test_module_state_reset_by_fixture(self):
        """The conftest isolation fixture clears the cached singleton."""
        from hsync_client.sync import _get_transfer_executor

        assert _get_transfer_executor(2) is not None
        assert sync_mod._transfer_executor is not None


class TestRemoteRealtimePool:
    def test_executor_is_singleton(self):
        from hsync_client.remote_websocket import _get_remote_upload_executor

        assert _get_remote_upload_executor() is _get_remote_upload_executor()

    def test_on_file_ready_schedules_upload_on_pool(self):
        """Realtime uploads run on the bounded pool, never inline.

        The watchdog dispatch thread is single: an inline upload would stall
        the processing of every subsequent filesystem event. The hooks return
        ``None`` (the base-class contract), so the test drives its own pool
        and waits it out instead of relying on a returned Future.
        """
        from concurrent.futures import ThreadPoolExecutor

        handler = remote_websocket.RemoteFileEventHandler(
            websocket=None, loop=None, event_queue=None
        )
        file = SimpleNamespace(filepath="/tmp/whatever", name="whatever")
        executor = ThreadPoolExecutor(max_workers=2)

        with (
            patch.object(remote_websocket, "upload_file") as mock_upload,
            patch.object(remote_websocket, "_get_remote_upload_executor", return_value=executor),
        ):
            handler._on_file_ready(file, "/tmp/whatever")
            executor.shutdown(wait=True)

        mock_upload.assert_called_once()
        assert mock_upload.call_args.args[0] is file
        assert mock_upload.call_args.kwargs == {"permissions_only": False, "from_closed": False}

    def test_on_permissions_changed_schedules_permission_only_upload(self):
        from concurrent.futures import ThreadPoolExecutor

        handler = remote_websocket.RemoteFileEventHandler(
            websocket=None, loop=None, event_queue=None
        )
        file = SimpleNamespace(filepath="/tmp/whatever", name="whatever")
        executor = ThreadPoolExecutor(max_workers=2)

        with (
            patch.object(remote_websocket, "upload_file") as mock_upload,
            patch.object(remote_websocket, "_get_remote_upload_executor", return_value=executor),
        ):
            handler._on_permissions_changed(file, "/tmp/whatever")
            executor.shutdown(wait=True)

        mock_upload.assert_called_once()
        assert mock_upload.call_args.kwargs.get("permissions_only") is True


class TestPoolsFactory:
    """Direct tests for the shared lazy-singleton factory (hsync_client.pools)."""

    def test_fixed_pool_is_singleton_and_ignores_worker_count(self):
        """A non-resizable pool is created once and never replaced."""
        import types

        from hsync_client.pools import get_singleton_executor

        module = types.ModuleType("pools_factory_fake_module")
        executor_a = get_singleton_executor(module, "_pool", max_workers=3, thread_name_prefix="t")
        # Same worker count and even a DIFFERENT count return the same pool.
        executor_b = get_singleton_executor(module, "_pool", max_workers=3, thread_name_prefix="t")
        executor_c = get_singleton_executor(module, "_pool", max_workers=8, thread_name_prefix="t")
        assert executor_a is executor_b
        assert executor_a is executor_c
        assert executor_a._max_workers == 3
        executor_a.shutdown(wait=False, cancel_futures=True)

    def test_resizable_pool_is_replaced_on_worker_count_change(self):
        """A resizable pool is swapped when the requested size changes."""
        import types

        from hsync_client.pools import get_singleton_executor

        module = types.ModuleType("pools_factory_fake_module")
        small = get_singleton_executor(
            module, "_pool", max_workers=2, thread_name_prefix="t", resizable=True
        )
        large = get_singleton_executor(
            module, "_pool", max_workers=5, thread_name_prefix="t", resizable=True
        )
        assert small is not large
        assert large._max_workers == 5
        same = get_singleton_executor(
            module, "_pool", max_workers=5, thread_name_prefix="t", resizable=True
        )
        assert same is large
        large.shutdown(wait=False, cancel_futures=True)

    def test_log_future_error_surfaces_exception(self):
        """A failed Future logs once (with the label); a completed one is silent."""
        from concurrent.futures import Future

        from loguru import logger as log

        from hsync_client import pools

        messages: list[str] = []
        sink_id = log.add(messages.append, level="ERROR")
        try:
            failed = Future()
            failed.set_exception(RuntimeError("boom"))
            pools.log_future_error(failed, label="op failed")

            ok = Future()
            ok.set_result(None)
            pools.log_future_error(ok, label="op failed")
        finally:
            log.remove(sink_id)

        assert len(messages) == 1
        assert "op failed" in messages[0]
        assert "boom" in messages[0]


class TestCompressToTempFile:
    def test_roundtrip_and_cleanup(self, tmp_path):
        src = tmp_path / "data.txt"
        src.write_bytes(b"compress me please " * 10_000)

        tmp = _compress_to_temp_file(str(src))
        try:
            assert Path(tmp).stat().st_size > 0
            assert decompress_data(Path(tmp).read_bytes()) == src.read_bytes()
        finally:
            Path(tmp).unlink(missing_ok=True)

    def test_temp_file_removed_on_compression_failure(self, tmp_path):
        import tempfile

        src = tmp_path / "data.txt"
        src.write_bytes(b"irrelevant")

        before = {p.name for p in Path(tempfile.gettempdir()).glob("hsync-upload-*")}
        with (
            patch(
                "hsync_client.transfer.compress_file_to_file",
                side_effect=OSError("boom"),
            ),
            pytest.raises(OSError),
        ):
            _compress_to_temp_file(str(src))
        after = {p.name for p in Path(tempfile.gettempdir()).glob("hsync-upload-*")}

        assert not (after - before)  # no temp file leaked
