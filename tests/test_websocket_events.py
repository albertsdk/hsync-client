# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for WebSocket event processing, resync, and remote_connect functions."""

import asyncio
import contextlib
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest


class _StopLoop(Exception):
    """Sentinel exception used to break out of event loops under test.

    Tests suppress only this exact type so that any *unexpected* exception
    raised by the code under test still fails the test instead of being
    silently swallowed by a broad ``contextlib.suppress(Exception)``.
    """


class _CancelledTask:
    """Small awaitable task double for create_task cleanup tests."""

    def __init__(self):
        self.cancel = MagicMock()

    def __await__(self):
        async def _raise_cancelled():
            raise asyncio.CancelledError()

        return _raise_cancelled().__await__()


def _close_coro_and_return_task(coro, task: _CancelledTask | None = None) -> _CancelledTask:
    """Consume a coroutine passed to create_task without scheduling it."""
    if hasattr(coro, "close"):
        coro.close()
    return task or _CancelledTask()


class TestProcessWebsocketEvents:
    """Tests for _process_remote_events function."""

    @pytest.mark.asyncio
    async def test_processes_added_directory_event(self):
        """Processes added directory event correctly."""
        from json import dumps

        from hsync_client.remote_websocket import _process_remote_events

        message = {
            "name": "added",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "newdir", "permissions": "755"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), _StopLoop("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_dir") as mock_update,
        ):
            with contextlib.suppress(_StopLoop):
                await _process_remote_events(websocket, "testbucket")
            mock_update.assert_called_once_with(message["src"])

    @pytest.mark.asyncio
    async def test_processes_added_file_event(self):
        """Processes added file event correctly."""
        from json import dumps

        from hsync_client.remote_websocket import _process_remote_events

        message = {
            "name": "added",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {
                "filepath": "newfile.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 10,
                "permissions": "644",
            },
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), _StopLoop("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file") as mock_update,
        ):
            with contextlib.suppress(_StopLoop):
                await _process_remote_events(websocket, "testbucket")
            mock_update.assert_called_once_with(
                message["src"], inventory_observer=ANY, source_resolver=ANY
            )

    @pytest.mark.asyncio
    async def test_skips_event_from_same_client(self):
        """Skips events from the same client (echo prevention)."""
        from json import dumps

        from hsync_client.remote_websocket import _process_remote_events

        message = {
            "name": "added",
            "cid": "my_client",  # Same as CONFIG["CID"]
            "bucket": "testbucket",
            "src": {"path": "newdir", "permissions": "755"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), _StopLoop("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_dir") as mock_update,
        ):
            with contextlib.suppress(_StopLoop):
                await _process_remote_events(websocket, "testbucket")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_event_from_different_bucket(self):
        """Skips events from different buckets."""
        from json import dumps

        from hsync_client.remote_websocket import _process_remote_events

        message = {
            "name": "added",
            "cid": "other_client",
            "bucket": "otherbucket",  # Different bucket
            "src": {"path": "newdir", "permissions": "755"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), _StopLoop("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_dir") as mock_update,
        ):
            with contextlib.suppress(_StopLoop):
                await _process_remote_events(websocket, "testbucket")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_invalid_event_type(self):
        """Skips events with invalid event type."""
        from json import dumps

        from hsync_client.remote_websocket import _process_remote_events

        message = {
            "name": "invalid_event",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "newdir", "permissions": "755"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), _StopLoop("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_dir") as mock_update,
        ):
            with contextlib.suppress(_StopLoop):
                await _process_remote_events(websocket, "testbucket")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_processes_renamed_event(self):
        """Processes renamed event correctly."""
        from json import dumps

        from hsync_client.remote_websocket import _process_remote_events

        message = {
            "name": "renamed",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "olddir", "permissions": "755"},
            "dst": {"path": "newdir", "permissions": "755"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), _StopLoop("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch._apply_rename", new=AsyncMock()) as mock_rename,
        ):
            with contextlib.suppress(_StopLoop):
                await _process_remote_events(websocket, "testbucket")
            mock_rename.assert_called_once()
            args = mock_rename.call_args.args
            assert args[0] == message["src"]
            assert args[1] == message["dst"]

    @pytest.mark.asyncio
    async def test_processes_deleted_event(self):
        """Processes deleted event correctly."""
        from json import dumps

        from hsync_client.remote_websocket import _process_remote_events

        message = {
            "name": "deleted",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "deleteddir", "permissions": "755"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), _StopLoop("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch._apply_delete", new=AsyncMock()) as mock_delete,
        ):
            with contextlib.suppress(_StopLoop):
                await _process_remote_events(websocket, "testbucket")
            mock_delete.assert_called_once()
            assert mock_delete.call_args.args[0] == message["src"]

    @pytest.mark.asyncio
    async def test_processes_modified_directory_event(self):
        """Processes modified directory event correctly."""
        from json import dumps

        from hsync_client.remote_websocket import _process_remote_events

        message = {
            "name": "modified",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "modifieddir", "permissions": "755"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), _StopLoop("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_dir") as mock_update,
        ):
            with contextlib.suppress(_StopLoop):
                await _process_remote_events(websocket, "testbucket")
            mock_update.assert_called_once_with(message["src"])

    @pytest.mark.asyncio
    async def test_processes_modified_file_event(self):
        """Processes modified file event correctly."""
        from json import dumps

        from hsync_client.remote_websocket import _process_remote_events

        message = {
            "name": "modified",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {
                "filepath": "modifiedfile.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 10,
                "permissions": "644",
            },
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), _StopLoop("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file") as mock_update,
        ):
            with contextlib.suppress(_StopLoop):
                await _process_remote_events(websocket, "testbucket")
            mock_update.assert_called_once_with(
                message["src"], inventory_observer=ANY, source_resolver=ANY
            )

    @pytest.mark.asyncio
    async def test_processes_symlink_event(self):
        """Processes symlink events received from the server.

        The server materialises the link and rebroadcasts the ``symlink``
        event to every peer except the sender, so remote-mode clients must
        apply it for real-time symlink replication (parity with LAN mode).
        Regression: the event used to be dropped as an invalid type, so
        symlinks only converged on the next full sync.
        """
        from json import dumps

        from hsync_client.remote_websocket import _process_remote_events

        message = {
            "name": "symlink",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"type": "symlink", "filepath": "link.txt", "target": "target.txt"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), _StopLoop("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch._apply_symlink", new=AsyncMock()) as mock_symlink,
        ):
            with contextlib.suppress(_StopLoop):
                await _process_remote_events(websocket, "testbucket")
            mock_symlink.assert_called_once()
            assert mock_symlink.call_args.args[0] == message["src"]

    @pytest.mark.asyncio
    async def test_skips_event_in_upload_only_mode(self):
        """Skips remote events when SYNC_DIRECTION is upload-only."""
        from json import dumps

        from hsync_client.remote_websocket import _process_remote_events

        message = {
            "name": "added",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "newdir", "permissions": "755"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), _StopLoop("Stop")])

        config = {"CID": "my_client", "SYNC_DIRECTION": "upload-only"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_dir") as mock_update,
        ):
            with contextlib.suppress(_StopLoop):
                await _process_remote_events(websocket, "testbucket")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_json_frame_does_not_kill_loop(self):
        """A non-JSON frame is dropped and the loop keeps processing events.

        Regression: ``loads`` used to run outside the per-message try, so one
        malformed frame killed the whole connection loop (teardown +
        reconnect) instead of being isolated per message.
        """
        from json import dumps

        from hsync_client.remote_websocket import _process_remote_events

        message = {
            "name": "added",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "newdir", "permissions": "755"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(
            side_effect=[
                "this is not json at all {",
                dumps(message),
                _StopLoop("Stop"),
            ]
        )

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_dir") as mock_update,
        ):
            with contextlib.suppress(_StopLoop):
                await _process_remote_events(websocket, "testbucket")

            # The malformed frame was skipped and the subsequent valid event
            # was still processed: the loop survived.
            mock_update.assert_called_once_with(message["src"])


class TestResync:
    """Tests for resync function."""

    @pytest.mark.asyncio
    async def test_resync_calls_sync_periodically(self):
        """resync calls sync after configured delay."""
        from hsync_client.remote_websocket import resync

        config = {"RESYNC": 0.01}  # Very short delay for testing

        call_count = 0

        async def mock_sync(lan_mode: bool = False, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise _StopLoop("Stop after 2 calls")

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.sync.CONFIG", config),
            patch("hsync_client.sync.sync", mock_sync),
        ):
            with contextlib.suppress(_StopLoop):
                await resync()
            assert call_count >= 2


class TestReconnectDelay:
    """Full-jitter exponential backoff helper used by remote_connect."""

    def _delay(self):
        from hsync_client.remote_websocket import _reconnect_delay

        return _reconnect_delay

    def test_zero_attempts_is_small_sniff(self, monkeypatch):
        """Attempt 0 returns a uniform value in (0, base] — a quick retry."""
        delay = self._delay()
        monkeypatch.setattr("hsync_client.remote_websocket.uniform", lambda lo, hi: hi)
        assert delay(5, 0) == 5

    def test_capped_by_reconnect_delay_max(self, monkeypatch):
        """Deep backoff never exceeds RECONNECT_DELAY_MAX."""
        from hsync_client.constants import RECONNECT_DELAY_MAX

        delay = self._delay()
        monkeypatch.setattr("hsync_client.remote_websocket.uniform", lambda lo, hi: hi)
        assert delay(5, 100) == float(RECONNECT_DELAY_MAX)

    def test_backoff_grows_with_attempts(self, monkeypatch):
        """Successive attempts expand the upper bound (until the cap)."""
        delay = self._delay()
        monkeypatch.setattr("hsync_client.remote_websocket.uniform", lambda lo, hi: hi)
        first = delay(2, 0)
        second = delay(2, 1)
        third = delay(2, 2)
        assert first < second < third


class TestRemoteConnect:
    """Tests for remote_connect function."""

    @pytest.mark.asyncio
    async def test_remote_connect_performs_initial_sync(self):
        """remote_connect performs initial sync before entering event loop."""
        from hsync_client.remote_websocket import remote_connect

        config = {"RESYNC": None, "RECONNECT_DELAY": 0.01}

        sync_called = False
        iteration = 0

        async def mock_sync():
            nonlocal sync_called, iteration
            sync_called = True
            iteration += 1
            if iteration > 1:
                raise KeyboardInterrupt("Stop loop")

        async def mock_remote_events():
            raise _StopLoop("Stop after first iteration")

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.remote_websocket.sync", mock_sync),
            patch("hsync_client.remote_websocket.remote_events", mock_remote_events),
        ):
            with contextlib.suppress(TimeoutError, KeyboardInterrupt, _StopLoop):
                await asyncio.wait_for(remote_connect(), timeout=1.0)
            assert sync_called

    @pytest.mark.asyncio
    async def test_remote_connect_refreshes_tuning_before_initial_sync(self):
        """remote_connect re-measures RTT before the initial sync (cold start)."""
        from hsync_client.remote_websocket import remote_connect

        config = {"RESYNC": None, "RECONNECT_DELAY": 0.01, "AUTOTUNE_ENABLED": True}

        order = []

        async def mock_refresh():
            order.append("refresh")

        async def mock_sync():
            order.append("sync")
            raise KeyboardInterrupt("Stop loop")

        async def mock_remote_events():
            raise _StopLoop("Stop")

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.remote_websocket.sync", mock_sync),
            patch("hsync_client.remote_websocket.remote_events", mock_remote_events),
            patch("hsync_client.remote_websocket._refresh_network_tuning", mock_refresh),
        ):
            with contextlib.suppress(TimeoutError, KeyboardInterrupt, _StopLoop):
                await asyncio.wait_for(remote_connect(), timeout=1.0)
            assert order == ["refresh", "sync"]

    @pytest.mark.asyncio
    async def test_remote_connect_starts_resync_task_when_configured(self):
        """remote_connect starts resync task when RESYNC is configured."""
        import hsync_client.remote_websocket as ws_module
        from hsync_client.remote_websocket import remote_connect

        config = {"RESYNC": 60, "RECONNECT_DELAY": 0.01}

        iteration = 0
        task_created = False

        async def mock_sync():
            nonlocal iteration
            iteration += 1
            if iteration > 1:
                raise KeyboardInterrupt("Stop loop")

        async def mock_remote_events():
            nonlocal task_created
            # Check if task was created before raising
            task_created = ws_module._resync_task is not None
            raise _StopLoop("Stop")

        async def mock_resync():
            await asyncio.sleep(10)  # Long sleep to keep task alive

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.remote_websocket.sync", mock_sync),
            patch("hsync_client.remote_websocket.remote_events", mock_remote_events),
            patch("hsync_client.remote_websocket.resync", mock_resync),
        ):
            # Reset global task
            ws_module._resync_task = None
            with contextlib.suppress(TimeoutError, KeyboardInterrupt, _StopLoop):
                await asyncio.wait_for(remote_connect(), timeout=1.0)
            # Task should have been created
            assert task_created

    @pytest.mark.asyncio
    async def test_remote_connect_handles_connection_closed(self):
        """remote_connect handles ConnectionClosed and reconnects."""
        from websockets.exceptions import ConnectionClosed

        from hsync_client.remote_websocket import remote_connect

        config = {"RESYNC": None, "RECONNECT_DELAY": 0.01}

        call_count = 0

        async def mock_sync():
            pass

        async def mock_remote_events():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionClosed(None, None)
            raise KeyboardInterrupt("Stop after reconnect")

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.remote_websocket.sync", mock_sync),
            patch("hsync_client.remote_websocket.remote_events", mock_remote_events),
        ):
            with contextlib.suppress(TimeoutError, KeyboardInterrupt, _StopLoop):
                await asyncio.wait_for(remote_connect(), timeout=2.0)
            assert call_count >= 2  # Should have reconnected

    @pytest.mark.asyncio
    async def test_remote_connect_cancels_previous_resync_task(self):
        """remote_connect cancels previous resync task on reconnect."""
        from websockets.exceptions import ConnectionClosed

        import hsync_client.remote_websocket as ws_module
        from hsync_client.remote_websocket import remote_connect

        config = {"RESYNC": 60, "RECONNECT_DELAY": 0.01}

        iteration = 0
        first_task = None

        async def mock_sync():
            pass

        async def mock_remote_events():
            nonlocal iteration, first_task
            iteration += 1
            if iteration == 1:
                # Save the first resync task reference
                first_task = ws_module._resync_task
                raise ConnectionClosed(None, None)
            raise KeyboardInterrupt("Stop after reconnect")

        async def mock_resync():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise  # let cancellation propagate cleanly

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.remote_websocket.sync", mock_sync),
            patch("hsync_client.remote_websocket.remote_events", mock_remote_events),
            patch("hsync_client.remote_websocket.resync", mock_resync),
        ):
            ws_module._resync_task = None
            with contextlib.suppress(
                TimeoutError, KeyboardInterrupt, asyncio.CancelledError, _StopLoop
            ):
                await asyncio.wait_for(remote_connect(), timeout=2.0)
            # First task should have been cancelled
            assert first_task is not None
            assert first_task.cancelled()


class TestRefreshNetworkTuning:
    """Tests for _refresh_network_tuning (RTT + bandwidth re-seed)."""

    @pytest.mark.asyncio
    async def test_seeds_bandwidth_when_ema_empty(self):
        """Re-seeds the bandwidth EMA from the active probe when it is empty."""
        from hsync_client.remote_websocket import _refresh_network_tuning

        config = {
            "AUTOTUNE_ENABLED": True,
            "AUTOTUNE_MANAGED_KEYS": ["HTTP_TIMEOUT", "MAX_CONCURRENT_TRANSFERS"],
            "API": {"buckets": "https://localhost:9753/api/v1/buckets"},
        }
        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.autotune.measure_rtt", return_value=5.0),
            patch("hsync_client.autotune.measure_bandwidth_active", return_value=900.0),
            patch("hsync_client.autotune._bw_ema", None),
            patch("hsync_client.autotune.seed_bw_ema") as mock_seed,
        ):
            await _refresh_network_tuning()
            mock_seed.assert_called_once_with(900.0)

    @pytest.mark.asyncio
    async def test_skips_bandwidth_when_ema_populated(self):
        """No active bandwidth probe once the passive EMA already has data."""
        from hsync_client.remote_websocket import _refresh_network_tuning

        config = {
            "AUTOTUNE_ENABLED": True,
            "AUTOTUNE_MANAGED_KEYS": ["HTTP_TIMEOUT", "MAX_CONCURRENT_TRANSFERS"],
            "API": {"buckets": "https://localhost:9753/api/v1/buckets"},
        }
        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.autotune.measure_rtt", return_value=5.0),
            patch("hsync_client.autotune.measure_bandwidth_active") as mock_bw,
            patch("hsync_client.autotune._bw_ema", 500.0),
            patch("hsync_client.autotune.seed_bw_ema") as mock_seed,
        ):
            await _refresh_network_tuning()
            mock_bw.assert_not_called()
            mock_seed.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_bandwidth_when_concurrency_not_managed(self):
        """No active bandwidth probe when concurrency is not autotune-managed."""
        from hsync_client.remote_websocket import _refresh_network_tuning

        config = {
            "AUTOTUNE_ENABLED": True,
            "AUTOTUNE_MANAGED_KEYS": ["HTTP_TIMEOUT"],
            "API": {"buckets": "https://localhost:9753/api/v1/buckets"},
        }
        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.autotune.measure_rtt", return_value=5.0),
            patch("hsync_client.autotune.measure_bandwidth_active") as mock_bw,
            patch("hsync_client.autotune._bw_ema", None),
            patch("hsync_client.autotune.seed_bw_ema") as mock_seed,
        ):
            await _refresh_network_tuning()
            mock_bw.assert_not_called()
            mock_seed.assert_not_called()


class TestRemoteEvents:
    """Tests for remote_events function."""

    @pytest.mark.asyncio
    async def test_remote_events_authentication_success(self):
        """remote_events authenticates successfully and processes events."""
        from json import dumps

        from hsync_client.remote_websocket import remote_events

        # Mock auth response
        auth_response = {
            "type": "auth",
            "status": "ok",
            "protocol_version": "ws/1",
            "app_version": "1.0.0",
            "capabilities": ["compression"],
        }

        # Mock event message to stop the loop
        event_message = {
            "name": "added",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "newdir"},
        }

        config = {
            "CID": "my_client",
            "LOCAL_BUCKET": "/tmp/bucket",
            "REMOTE_BUCKET": "testbucket",
            "NO_VERIFY_SSL": False,
            "CA_CERT": None,
            "RECONNECT_DELAY": 0.01,
            "RESYNC": None,
            "SERVER_COMPRESSION_ENABLED": False,
            "API": {"events": "wss://api.example.com/events"},
        }

        mock_websocket = AsyncMock()
        mock_websocket.__aenter__ = AsyncMock(return_value=mock_websocket)
        mock_websocket.__aexit__ = AsyncMock(return_value=None)
        mock_websocket.send = AsyncMock()
        mock_websocket.recv = AsyncMock(
            side_effect=[
                dumps(auth_response),
                dumps(event_message),
                _StopLoop("Stop loop"),
            ]
        )

        mock_observer = MagicMock()
        mock_observer.start = MagicMock()
        mock_observer.stop = MagicMock()
        mock_observer.join = MagicMock()

        with (
            patch("hsync_client.remote_websocket.connect", return_value=mock_websocket),
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.remote_websocket.EventQueue") as MockEventQueue,
            patch("hsync_client.remote_websocket.RemoteFileEventHandler"),
            patch("hsync_client.remote_websocket.WatcherManager", return_value=mock_observer),
            patch("hsync_client.remote_websocket._process_remote_events") as mock_process,
        ):
            MockEventQueue.return_value = MagicMock()
            MockEventQueue.return_value.process = AsyncMock()
            MockEventQueue.return_value.stop = MagicMock()
            mock_process.side_effect = _StopLoop("Stop loop")

            with contextlib.suppress(_StopLoop):
                await asyncio.wait_for(remote_events(), timeout=1.0)

            # Verify authentication message was sent
            mock_websocket.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_remote_events_logs_ready_after_watcher_start(self):
        """[READY] is logged once the watcher is running and the WS is authed.

        The readiness threshold is observable: auth success alone is not
        enough — the line must appear only after ``observer.start()``.
        """
        from json import dumps

        from hsync_client.remote_websocket import remote_events

        auth_response = {
            "type": "auth",
            "status": "ok",
            "protocol_version": "ws/1",
            "app_version": "1.0.0",
            "capabilities": ["compression"],
        }

        config = {
            "CID": "my_client",
            "LOCAL_BUCKET": "/tmp/bucket",
            "REMOTE_BUCKET": "testbucket",
            "NO_VERIFY_SSL": False,
            "CA_CERT": None,
            "RECONNECT_DELAY": 0.01,
            "RESYNC": None,
            "SERVER_COMPRESSION_ENABLED": False,
            "API": {"events": "wss://api.example.com/events"},
        }

        mock_websocket = AsyncMock()
        mock_websocket.__aenter__ = AsyncMock(return_value=mock_websocket)
        mock_websocket.__aexit__ = AsyncMock(return_value=None)
        mock_websocket.send = AsyncMock()
        mock_websocket.recv = AsyncMock(side_effect=[dumps(auth_response), _StopLoop("Stop loop")])

        mock_observer = MagicMock()
        mock_observer.start = MagicMock()
        mock_observer.stop = MagicMock()
        mock_observer.join = MagicMock()

        with (
            patch("hsync_client.remote_websocket.connect", return_value=mock_websocket),
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.remote_websocket.log") as mock_log,
            patch("hsync_client.remote_websocket.EventQueue") as MockEventQueue,
            patch("hsync_client.remote_websocket.RemoteFileEventHandler"),
            patch("hsync_client.remote_websocket.WatcherManager", return_value=mock_observer),
            patch("hsync_client.remote_websocket._process_remote_events") as mock_process,
        ):
            MockEventQueue.return_value = MagicMock()
            MockEventQueue.return_value.process = AsyncMock()
            MockEventQueue.return_value.stop = MagicMock()
            mock_process.side_effect = _StopLoop("Stop loop")

            with contextlib.suppress(_StopLoop):
                await asyncio.wait_for(remote_events(), timeout=1.0)

        ready_msgs = [
            str(c.args[0])
            for c in mock_log.info.call_args_list
            if c.args and "[READY]" in str(c.args[0])
        ]
        assert len(ready_msgs) == 1, f"expected one [READY] line, got {ready_msgs!r}"
        assert "watching '/tmp/bucket'" in ready_msgs[0]

    @pytest.mark.asyncio
    async def test_remote_events_authentication_failed(self):
        """remote_events returns early when authentication fails."""
        from json import dumps

        from hsync_client.remote_websocket import remote_events

        # Mock failed auth response
        auth_response = {
            "type": "auth",
            "status": "error",
            "error": "invalid_token",
        }

        config = {
            "CID": "my_client",
            "LOCAL_BUCKET": "/tmp/bucket",
            "REMOTE_BUCKET": "testbucket",
            "NO_VERIFY_SSL": False,
            "CA_CERT": None,
            "API": {"events": "wss://api.example.com/events"},
        }

        mock_websocket = AsyncMock()
        mock_websocket.__aenter__ = AsyncMock(return_value=mock_websocket)
        mock_websocket.__aexit__ = AsyncMock(return_value=None)
        mock_websocket.send = AsyncMock()
        mock_websocket.recv = AsyncMock(return_value=dumps(auth_response))

        with (
            patch("hsync_client.remote_websocket.connect", return_value=mock_websocket),
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.remote_websocket.log") as mock_log,
        ):
            await remote_events()

            # Should return early without processing events
            mock_websocket.recv.assert_called_once()

        # A failed authentication must never reach the readiness threshold.
        ready_msgs = [
            str(c.args[0])
            for c in mock_log.info.call_args_list
            if c.args and "[READY]" in str(c.args[0])
        ]
        assert ready_msgs == [], f"no [READY] expected on auth failure, got {ready_msgs!r}"

    @pytest.mark.asyncio
    async def test_remote_events_protocol_incompatible(self):
        """remote_events returns early when the server protocol mismatches."""
        from json import dumps

        from hsync_client.remote_websocket import remote_events

        # Auth accepted but the server speaks a different wire protocol.
        auth_response = {
            "type": "auth",
            "status": "ok",
            "protocol_version": "ws/999",
            "app_version": "1.0.0",
            "capabilities": [],
        }

        config = {
            "CID": "my_client",
            "LOCAL_BUCKET": "/tmp/bucket",
            "REMOTE_BUCKET": "testbucket",
            "NO_VERIFY_SSL": False,
            "CA_CERT": None,
            "API": {"events": "wss://api.example.com/events"},
        }

        mock_websocket = AsyncMock()
        mock_websocket.__aenter__ = AsyncMock(return_value=mock_websocket)
        mock_websocket.__aexit__ = AsyncMock(return_value=None)
        mock_websocket.send = AsyncMock()
        mock_websocket.recv = AsyncMock(return_value=dumps(auth_response))

        with (
            patch("hsync_client.remote_websocket.connect", return_value=mock_websocket),
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
        ):
            await remote_events()

            # Should return early after sending the auth frame.
            mock_websocket.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_remote_events_ssl_no_verify(self):
        """remote_events configures SSL with no verification when configured."""
        from json import dumps

        from hsync_client.remote_websocket import remote_events

        auth_response = {
            "type": "auth",
            "status": "ok",
            "protocol_version": "ws/1",
            "app_version": "1.0.0",
            "capabilities": [],
        }

        event_message = {
            "name": "added",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "newdir"},
        }

        config = {
            "CID": "my_client",
            "LOCAL_BUCKET": "/tmp/bucket",
            "REMOTE_BUCKET": "testbucket",
            "NO_VERIFY_SSL": True,
            "CA_CERT": None,
            "RECONNECT_DELAY": 0.01,
            "RESYNC": None,
            "SERVER_COMPRESSION_ENABLED": False,
            "API": {"events": "wss://api.example.com/events"},
        }

        mock_websocket = AsyncMock()
        mock_websocket.__aenter__ = AsyncMock(return_value=mock_websocket)
        mock_websocket.__aexit__ = AsyncMock(return_value=None)
        mock_websocket.send = AsyncMock()
        mock_websocket.recv = AsyncMock(
            side_effect=[
                dumps(auth_response),
                dumps(event_message),
                _StopLoop("Stop loop"),
            ]
        )

        mock_observer = MagicMock()
        ssl_context_used = {}

        def mock_connect(_uri, **kwargs):
            del _uri
            ssl_context_used["context"] = kwargs.get("ssl")
            return mock_websocket

        with (
            patch("hsync_client.remote_websocket.connect", side_effect=mock_connect),
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.remote_websocket.EventQueue") as MockEventQueue,
            patch("hsync_client.remote_websocket.WatcherManager", return_value=mock_observer),
            patch("hsync_client.remote_websocket._process_remote_events") as mock_process,
        ):
            MockEventQueue.return_value = MagicMock()
            MockEventQueue.return_value.process = AsyncMock()
            MockEventQueue.return_value.stop = MagicMock()
            mock_process.side_effect = _StopLoop("Stop loop")

            with contextlib.suppress(_StopLoop):
                await asyncio.wait_for(remote_events(), timeout=1.0)

            # SSL context should be configured with no verification
            assert ssl_context_used.get("context") is not None
            assert ssl_context_used["context"].check_hostname is False

    @pytest.mark.asyncio
    async def test_remote_events_handles_exception(self):
        """remote_events handles unexpected exceptions gracefully."""
        from json import dumps

        from hsync_client.remote_websocket import remote_events

        auth_response = {
            "type": "auth",
            "status": "ok",
            "protocol_version": "ws/1",
            "app_version": "1.0.0",
            "capabilities": [],
        }

        config = {
            "CID": "my_client",
            "LOCAL_BUCKET": "/tmp/bucket",
            "REMOTE_BUCKET": "testbucket",
            "NO_VERIFY_SSL": False,
            "CA_CERT": None,
            "SERVER_COMPRESSION_ENABLED": False,
            "API": {"events": "wss://api.example.com/events"},
            "RECONNECT_DELAY": 0.01,
            "RESYNC": None,
        }

        mock_websocket = AsyncMock()
        mock_websocket.__aenter__ = AsyncMock(return_value=mock_websocket)
        mock_websocket.__aexit__ = AsyncMock(return_value=None)
        mock_websocket.send = AsyncMock()
        mock_websocket.recv = AsyncMock(return_value=dumps(auth_response))

        mock_observer = MagicMock()
        mock_observer.start = MagicMock()
        mock_observer.stop = MagicMock()
        mock_observer.join = MagicMock()

        queue_mock = MagicMock()
        queue_mock.process = AsyncMock()
        queue_mock.stop = MagicMock()

        with (
            patch("hsync_client.remote_websocket.connect", return_value=mock_websocket),
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.remote_websocket.EventQueue", return_value=queue_mock),
            patch("hsync_client.remote_websocket.RemoteFileEventHandler"),
            patch("hsync_client.remote_websocket.WatcherManager", return_value=mock_observer),
            patch(
                "hsync_client.remote_websocket.create_task", side_effect=_close_coro_and_return_task
            ),
            patch(
                "hsync_client.remote_websocket._process_remote_events",
                side_effect=Exception("Unexpected error"),
            ),
            patch("hsync_client.remote_websocket.log") as mock_log,
        ):
            await remote_events()

            # Error should be logged
            assert mock_log.error.called

    @pytest.mark.asyncio
    async def test_remote_events_cleans_up_queue_task(self):
        """remote_events cleans up queue task on exit."""
        from json import dumps

        from hsync_client.remote_websocket import remote_events

        auth_response = {
            "type": "auth",
            "status": "ok",
            "protocol_version": "ws/1",
            "app_version": "1.0.0",
            "capabilities": [],
        }

        config = {
            "CID": "my_client",
            "LOCAL_BUCKET": "/tmp/bucket",
            "REMOTE_BUCKET": "testbucket",
            "NO_VERIFY_SSL": False,
            "CA_CERT": None,
            "SERVER_COMPRESSION_ENABLED": False,
            "API": {"events": "wss://api.example.com/events"},
            "RECONNECT_DELAY": 0.01,
            "RESYNC": None,
        }

        mock_websocket = AsyncMock()
        mock_websocket.__aenter__ = AsyncMock(return_value=mock_websocket)
        mock_websocket.__aexit__ = AsyncMock(return_value=None)
        mock_websocket.send = AsyncMock()
        mock_websocket.recv = AsyncMock(return_value=dumps(auth_response))

        mock_observer = MagicMock()
        mock_observer.start = MagicMock()

        queue_mock = MagicMock()
        mock_task = _CancelledTask()
        queue_mock.process = AsyncMock()
        queue_mock.stop = MagicMock()

        def mock_create_task(coro):
            return _close_coro_and_return_task(coro, mock_task)

        with (
            patch("hsync_client.remote_websocket.connect", return_value=mock_websocket),
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.remote_websocket.EventQueue", return_value=queue_mock),
            patch("hsync_client.remote_websocket.RemoteFileEventHandler"),
            patch("hsync_client.remote_websocket.WatcherManager", return_value=mock_observer),
            patch(
                "hsync_client.remote_websocket._process_remote_events",
                side_effect=_StopLoop("Stop"),
            ),
            patch("hsync_client.remote_websocket.create_task", side_effect=mock_create_task),
        ):
            with contextlib.suppress(_StopLoop):
                await remote_events()

            # Queue task should be cancelled
            mock_task.cancel.assert_called()


class TestRemoteEventsAuthToken:
    """Regression tests for WebSocket auth token resolution."""

    @pytest.mark.asyncio
    async def test_auth_token_uses_config_api_key(self):
        """The auth message token must come from CONFIG['API_KEY']

        (populated by main_logic from --api-key-fd or HSYNC_API_KEY),
        not from a stale import-time constant. Regression for FD-pipe
        path used by hsync-desktop.
        """
        from json import dumps, loads

        from hsync_client.remote_websocket import remote_events

        auth_response = {
            "type": "auth",
            "status": "ok",
            "protocol_version": "ws/1",
            "app_version": "1.0.0",
            "capabilities": [],
        }

        config = {
            "CID": "my_client",
            "LOCAL_BUCKET": "/tmp/bucket",
            "REMOTE_BUCKET": "testbucket",
            "NO_VERIFY_SSL": False,
            "CA_CERT": None,
            "RECONNECT_DELAY": 0.01,
            "RESYNC": None,
            "SERVER_COMPRESSION_ENABLED": False,
            "API": {"events": "wss://api.example.com/events"},
            "API_KEY": "fd-pipe-secret-token",
        }

        mock_websocket = AsyncMock()
        mock_websocket.__aenter__ = AsyncMock(return_value=mock_websocket)
        mock_websocket.__aexit__ = AsyncMock(return_value=None)
        mock_websocket.send = AsyncMock()
        mock_websocket.recv = AsyncMock(side_effect=[dumps(auth_response), _StopLoop("Stop loop")])

        mock_observer = MagicMock()

        with (
            patch("hsync_client.remote_websocket.connect", return_value=mock_websocket),
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.remote_websocket.EventQueue") as MockEventQueue,
            patch("hsync_client.remote_websocket.RemoteFileEventHandler"),
            patch("hsync_client.remote_websocket.WatcherManager", return_value=mock_observer),
            patch("hsync_client.remote_websocket._process_remote_events") as mock_process,
            patch("hsync_client.remote_websocket.get_api_key", return_value="env-fallback"),
        ):
            MockEventQueue.return_value = MagicMock()
            MockEventQueue.return_value.process = AsyncMock()
            MockEventQueue.return_value.stop = MagicMock()
            mock_process.side_effect = _StopLoop("Stop loop")

            with contextlib.suppress(_StopLoop):
                await asyncio.wait_for(remote_events(), timeout=1.0)

            sent = mock_websocket.send.call_args.args[0]
            payload = loads(sent)
            assert payload["type"] == "auth"
            assert payload["token"] == "fd-pipe-secret-token"

    @pytest.mark.asyncio
    async def test_auth_token_falls_back_to_env_getter(self):
        """When CONFIG['API_KEY'] is absent or empty, get_api_key() is used."""
        from json import dumps, loads

        from hsync_client.remote_websocket import remote_events

        auth_response = {
            "type": "auth",
            "status": "ok",
            "protocol_version": "ws/1",
            "app_version": "1.0.0",
            "capabilities": [],
        }

        config = {
            "CID": "my_client",
            "LOCAL_BUCKET": "/tmp/bucket",
            "REMOTE_BUCKET": "testbucket",
            "NO_VERIFY_SSL": False,
            "CA_CERT": None,
            "RECONNECT_DELAY": 0.01,
            "RESYNC": None,
            "SERVER_COMPRESSION_ENABLED": False,
            "API": {"events": "wss://api.example.com/events"},
            "API_KEY": "",
        }

        mock_websocket = AsyncMock()
        mock_websocket.__aenter__ = AsyncMock(return_value=mock_websocket)
        mock_websocket.__aexit__ = AsyncMock(return_value=None)
        mock_websocket.send = AsyncMock()
        mock_websocket.recv = AsyncMock(side_effect=[dumps(auth_response), _StopLoop("Stop loop")])

        mock_observer = MagicMock()

        with (
            patch("hsync_client.remote_websocket.connect", return_value=mock_websocket),
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.remote_websocket.EventQueue") as MockEventQueue,
            patch("hsync_client.remote_websocket.RemoteFileEventHandler"),
            patch("hsync_client.remote_websocket.WatcherManager", return_value=mock_observer),
            patch("hsync_client.remote_websocket._process_remote_events") as mock_process,
            patch(
                "hsync_client.remote_websocket.get_api_key",
                return_value="env-var-token",
            ),
        ):
            MockEventQueue.return_value = MagicMock()
            MockEventQueue.return_value.process = AsyncMock()
            MockEventQueue.return_value.stop = MagicMock()
            mock_process.side_effect = _StopLoop("Stop loop")

            with contextlib.suppress(_StopLoop):
                await asyncio.wait_for(remote_events(), timeout=1.0)

            sent = mock_websocket.send.call_args.args[0]
            payload = loads(sent)
            assert payload["token"] == "env-var-token"
