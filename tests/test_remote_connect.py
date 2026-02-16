# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for remote_connect function in websocket_handler module."""

import asyncio
import contextlib
from unittest.mock import patch

import pytest


class TestRemoteConnect:
    """Tests for remote_connect function."""

    @pytest.mark.asyncio
    async def test_remote_connect_performs_initial_sync(self):
        """remote_connect performs initial sync before entering event loop."""
        from hsync_client.websocket_handler import remote_connect

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
            raise Exception("Stop after first iteration")

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.sync", mock_sync),
            patch("hsync_client.websocket_handler.remote_events", mock_remote_events),
        ):
            with contextlib.suppress(TimeoutError, KeyboardInterrupt, Exception):
                await asyncio.wait_for(remote_connect(), timeout=1.0)
            assert sync_called

    @pytest.mark.asyncio
    async def test_remote_connect_starts_resync_task_when_configured(self):
        """remote_connect starts resync task when RESYNC is configured."""
        import hsync_client.websocket_handler as ws_module
        from hsync_client.websocket_handler import remote_connect

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
            raise Exception("Stop")

        async def mock_resync():
            await asyncio.sleep(10)  # Long sleep to keep task alive

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.sync", mock_sync),
            patch("hsync_client.websocket_handler.remote_events", mock_remote_events),
            patch("hsync_client.websocket_handler.resync", mock_resync),
        ):
            # Reset global task
            ws_module._resync_task = None
            with contextlib.suppress(TimeoutError, KeyboardInterrupt, Exception):
                await asyncio.wait_for(remote_connect(), timeout=1.0)
            # Task should have been created
            assert task_created

    @pytest.mark.asyncio
    async def test_remote_connect_handles_connection_closed(self):
        """remote_connect handles ConnectionClosed and reconnects."""
        from websockets.exceptions import ConnectionClosed

        from hsync_client.websocket_handler import remote_connect

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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.sync", mock_sync),
            patch("hsync_client.websocket_handler.remote_events", mock_remote_events),
        ):
            with contextlib.suppress(TimeoutError, KeyboardInterrupt, Exception):
                await asyncio.wait_for(remote_connect(), timeout=2.0)
            assert call_count >= 2  # Should have reconnected

    @pytest.mark.asyncio
    async def test_remote_connect_cancels_previous_resync_task(self):
        """remote_connect cancels previous resync task on reconnect."""
        from websockets.exceptions import ConnectionClosed

        import hsync_client.websocket_handler as ws_module
        from hsync_client.websocket_handler import remote_connect

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
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.sync", mock_sync),
            patch("hsync_client.websocket_handler.remote_events", mock_remote_events),
            patch("hsync_client.websocket_handler.resync", mock_resync),
        ):
            ws_module._resync_task = None
            with contextlib.suppress(
                TimeoutError, KeyboardInterrupt, asyncio.CancelledError, Exception
            ):
                await asyncio.wait_for(remote_connect(), timeout=2.0)
            # First task should have been cancelled
            assert first_task is not None
            assert first_task.cancelled()
