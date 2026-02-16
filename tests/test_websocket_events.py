# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for WebSocket event processing, resync, and remote_connect functions."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestProcessWebsocketEvents:
    """Tests for _process_websocket_events function."""

    @pytest.mark.asyncio
    async def test_processes_added_directory_event(self):
        """Processes added directory event correctly."""
        from json import dumps

        from hsync_client.websocket_handler import _process_websocket_events

        message = {
            "name": "added",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "newdir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.update_dir") as mock_update,
        ):
            with contextlib.suppress(Exception):
                await _process_websocket_events(websocket, "testbucket")
            mock_update.assert_called_once_with(message["src"])

    @pytest.mark.asyncio
    async def test_processes_added_file_event(self):
        """Processes added file event correctly."""
        from json import dumps

        from hsync_client.websocket_handler import _process_websocket_events

        message = {
            "name": "added",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {
                "filepath": "newfile.txt",
                "md5sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 10,
                "permissions": "-rw-r--r--",
            },
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.update_file") as mock_update,
        ):
            with contextlib.suppress(Exception):
                await _process_websocket_events(websocket, "testbucket")
            mock_update.assert_called_once_with(message["src"])

    @pytest.mark.asyncio
    async def test_skips_event_from_same_client(self):
        """Skips events from the same client (echo prevention)."""
        from json import dumps

        from hsync_client.websocket_handler import _process_websocket_events

        message = {
            "name": "added",
            "cid": "my_client",  # Same as CONFIG["CID"]
            "bucket": "testbucket",
            "src": {"path": "newdir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.update_dir") as mock_update,
        ):
            with contextlib.suppress(Exception):
                await _process_websocket_events(websocket, "testbucket")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_event_from_different_bucket(self):
        """Skips events from different buckets."""
        from json import dumps

        from hsync_client.websocket_handler import _process_websocket_events

        message = {
            "name": "added",
            "cid": "other_client",
            "bucket": "otherbucket",  # Different bucket
            "src": {"path": "newdir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.update_dir") as mock_update,
        ):
            with contextlib.suppress(Exception):
                await _process_websocket_events(websocket, "testbucket")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_invalid_event_type(self):
        """Skips events with invalid event type."""
        from json import dumps

        from hsync_client.websocket_handler import _process_websocket_events

        message = {
            "name": "invalid_event",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "newdir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.update_dir") as mock_update,
        ):
            with contextlib.suppress(Exception):
                await _process_websocket_events(websocket, "testbucket")
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_processes_renamed_event(self):
        """Processes renamed event correctly."""
        from json import dumps

        from hsync_client.websocket_handler import _process_websocket_events

        message = {
            "name": "renamed",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "olddir", "permissions": "drwxr-xr-x"},
            "dst": {"path": "newdir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler._handle_rename_event") as mock_rename,
        ):
            with contextlib.suppress(Exception):
                await _process_websocket_events(websocket, "testbucket")
            mock_rename.assert_called_once_with(message["src"], message["dst"])

    @pytest.mark.asyncio
    async def test_processes_deleted_event(self):
        """Processes deleted event correctly."""
        from json import dumps

        from hsync_client.websocket_handler import _process_websocket_events

        message = {
            "name": "deleted",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "deleteddir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler._handle_delete_event") as mock_delete,
        ):
            with contextlib.suppress(Exception):
                await _process_websocket_events(websocket, "testbucket")
            mock_delete.assert_called_once_with(message["src"])

    @pytest.mark.asyncio
    async def test_processes_modified_directory_event(self):
        """Processes modified directory event correctly."""
        from json import dumps

        from hsync_client.websocket_handler import _process_websocket_events

        message = {
            "name": "modified",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "modifieddir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.update_dir") as mock_update,
        ):
            with contextlib.suppress(Exception):
                await _process_websocket_events(websocket, "testbucket")
            mock_update.assert_called_once_with(message["src"])

    @pytest.mark.asyncio
    async def test_processes_modified_file_event(self):
        """Processes modified file event correctly."""
        from json import dumps

        from hsync_client.websocket_handler import _process_websocket_events

        message = {
            "name": "modified",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {
                "filepath": "modifiedfile.txt",
                "md5sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 10,
                "permissions": "-rw-r--r--",
            },
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.update_file") as mock_update,
        ):
            with contextlib.suppress(Exception):
                await _process_websocket_events(websocket, "testbucket")
            mock_update.assert_called_once_with(message["src"])

    @pytest.mark.asyncio
    async def test_skips_symlink_event(self):
        """Skips symlink events (not in valid event types)."""
        from json import dumps

        from hsync_client.websocket_handler import _process_websocket_events

        # Note: symlink events are not in the valid event types list
        # They are handled locally but not received via WebSocket
        message = {
            "name": "symlink",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "link.txt", "target": "target.txt", "permissions": "lrwxrwxrwx"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler._handle_symlink_event") as mock_symlink,
        ):
            with contextlib.suppress(Exception):
                await _process_websocket_events(websocket, "testbucket")
            # Symlink events are filtered out as invalid
            mock_symlink.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_event_in_upload_only_mode(self):
        """Skips remote events when SYNC_DIRECTION is upload-only."""
        from json import dumps

        from hsync_client.websocket_handler import _process_websocket_events

        message = {
            "name": "added",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "newdir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client", "SYNC_DIRECTION": "upload-only"}

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.update_dir") as mock_update,
        ):
            with contextlib.suppress(Exception):
                await _process_websocket_events(websocket, "testbucket")
            mock_update.assert_not_called()


class TestResync:
    """Tests for resync function."""

    @pytest.mark.asyncio
    async def test_resync_calls_sync_periodically(self):
        """resync calls sync after configured delay."""
        from hsync_client.websocket_handler import resync

        config = {"RESYNC": 0.01}  # Very short delay for testing

        call_count = 0

        async def mock_sync():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise Exception("Stop after 2 calls")

        with (
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.sync", mock_sync),
        ):
            with contextlib.suppress(Exception):
                await resync()
            assert call_count >= 2


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


class TestRemoteEvents:
    """Tests for remote_events function."""

    @pytest.mark.asyncio
    async def test_remote_events_authentication_success(self):
        """remote_events authenticates successfully and processes events."""
        from json import dumps

        from hsync_client.websocket_handler import remote_events

        # Mock auth response
        auth_response = {
            "type": "auth",
            "status": "ok",
            "version": "1.0.0",
            "compression_enabled": True,
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
                Exception("Stop loop"),
            ]
        )

        mock_observer = MagicMock()
        mock_observer.start = MagicMock()
        mock_observer.stop = MagicMock()
        mock_observer.join = MagicMock()

        with (
            patch("hsync_client.websocket_handler.connect", return_value=mock_websocket),
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.EventQueue") as MockEventQueue,
            patch("hsync_client.websocket_handler.FileEventHandler"),
            patch("hsync_client.websocket_handler.Observer", return_value=mock_observer),
            patch("hsync_client.websocket_handler._process_websocket_events") as mock_process,
            patch(
                "hsync_client.websocket_handler.check_version_compatibility",
                return_value=(True, None),
            ),
        ):
            MockEventQueue.return_value = MagicMock()
            mock_process.side_effect = Exception("Stop loop")

            with contextlib.suppress(Exception):
                await asyncio.wait_for(remote_events(), timeout=1.0)

            # Verify authentication message was sent
            mock_websocket.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_remote_events_authentication_failed(self):
        """remote_events returns early when authentication fails."""
        from json import dumps

        from hsync_client.websocket_handler import remote_events

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
            patch("hsync_client.websocket_handler.connect", return_value=mock_websocket),
            patch("hsync_client.websocket_handler.CONFIG", config),
        ):
            await remote_events()

            # Should return early without processing events
            mock_websocket.recv.assert_called_once()

    @pytest.mark.asyncio
    async def test_remote_events_version_incompatible(self):
        """remote_events returns early when version is incompatible."""
        from json import dumps

        from hsync_client.websocket_handler import remote_events

        # Mock auth response with version incompatibility
        auth_response = {
            "type": "auth",
            "status": "ok",
            "version": "0.1.0",
            "error": "version_incompatible",
            "server_version": "0.1.0",
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
            patch("hsync_client.websocket_handler.connect", return_value=mock_websocket),
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch(
                "hsync_client.websocket_handler.check_version_compatibility",
                return_value=(False, "version_too_old"),
            ),
        ):
            await remote_events()

            # Should return early
            mock_websocket.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_remote_events_version_warning(self):
        """remote_events logs warning for minor version mismatch."""
        from json import dumps

        from hsync_client.websocket_handler import remote_events

        auth_response = {
            "type": "auth",
            "status": "ok",
            "version": "1.1.0",
            "compression_enabled": False,
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
            "NO_VERIFY_SSL": False,
            "CA_CERT": None,
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
                Exception("Stop loop"),
            ]
        )

        mock_observer = MagicMock()

        with (
            patch("hsync_client.websocket_handler.connect", return_value=mock_websocket),
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.EventQueue") as MockEventQueue,
            patch("hsync_client.websocket_handler.Observer", return_value=mock_observer),
            patch("hsync_client.websocket_handler._process_websocket_events") as mock_process,
            patch(
                "hsync_client.websocket_handler.check_version_compatibility",
                return_value=(True, "minor_mismatch"),
            ),
            patch("hsync_client.websocket_handler.log") as mock_log,
        ):
            MockEventQueue.return_value = MagicMock()
            mock_process.side_effect = Exception("Stop loop")

            with contextlib.suppress(Exception):
                await asyncio.wait_for(remote_events(), timeout=1.0)

            # Should log warning for minor mismatch
            assert mock_log.warning.called

    @pytest.mark.asyncio
    async def test_remote_events_ssl_no_verify(self):
        """remote_events configures SSL with no verification when configured."""
        from json import dumps

        from hsync_client.websocket_handler import remote_events

        auth_response = {
            "type": "auth",
            "status": "ok",
            "version": "1.0.0",
            "compression_enabled": False,
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
                Exception("Stop loop"),
            ]
        )

        mock_observer = MagicMock()
        ssl_context_used = {}

        def mock_connect(uri, **kwargs):
            ssl_context_used["context"] = kwargs.get("ssl")
            return mock_websocket

        with (
            patch("hsync_client.websocket_handler.connect", side_effect=mock_connect),
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.EventQueue") as MockEventQueue,
            patch("hsync_client.websocket_handler.Observer", return_value=mock_observer),
            patch("hsync_client.websocket_handler._process_websocket_events") as mock_process,
            patch(
                "hsync_client.websocket_handler.check_version_compatibility",
                return_value=(True, None),
            ),
        ):
            MockEventQueue.return_value = MagicMock()
            mock_process.side_effect = Exception("Stop loop")

            with contextlib.suppress(Exception):
                await asyncio.wait_for(remote_events(), timeout=1.0)

            # SSL context should be configured with no verification
            assert ssl_context_used.get("context") is not None
            assert ssl_context_used["context"].check_hostname is False

    @pytest.mark.asyncio
    async def test_remote_events_handles_exception(self):
        """remote_events handles unexpected exceptions gracefully."""
        from json import dumps

        from hsync_client.websocket_handler import remote_events

        auth_response = {
            "type": "auth",
            "status": "ok",
            "version": "1.0.0",
            "compression_enabled": False,
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

        async def mock_create_task(coro):
            """Create a mock task that raises CancelledError when awaited."""
            task = AsyncMock()
            task.cancel = MagicMock()

            async def await_coro():
                raise asyncio.CancelledError()

            return task

        with (
            patch("hsync_client.websocket_handler.connect", return_value=mock_websocket),
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.EventQueue", return_value=queue_mock),
            patch("hsync_client.websocket_handler.FileEventHandler"),
            patch("hsync_client.websocket_handler.Observer", return_value=mock_observer),
            patch("hsync_client.websocket_handler.create_task", side_effect=mock_create_task),
            patch(
                "hsync_client.websocket_handler._process_websocket_events",
                side_effect=Exception("Unexpected error"),
            ),
            patch("hsync_client.websocket_handler.log") as mock_log,
        ):
            # Should not raise exception
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await remote_events()

            # Error should be logged
            assert mock_log.error.called

    @pytest.mark.asyncio
    async def test_remote_events_cleans_up_queue_task(self):
        """remote_events cleans up queue task on exit."""
        from json import dumps

        from hsync_client.websocket_handler import remote_events

        auth_response = {
            "type": "auth",
            "status": "ok",
            "version": "1.0.0",
            "compression_enabled": False,
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
        mock_task = AsyncMock()
        queue_mock.process = mock_task
        queue_mock.stop = MagicMock()

        with (
            patch("hsync_client.websocket_handler.connect", return_value=mock_websocket),
            patch("hsync_client.websocket_handler.CONFIG", config),
            patch("hsync_client.websocket_handler.EventQueue", return_value=queue_mock),
            patch("hsync_client.websocket_handler.FileEventHandler"),
            patch("hsync_client.websocket_handler.Observer", return_value=mock_observer),
            patch(
                "hsync_client.websocket_handler._process_websocket_events",
                side_effect=Exception("Stop"),
            ),
            patch("hsync_client.websocket_handler.create_task", return_value=mock_task),
        ):
            with contextlib.suppress(Exception):
                await remote_events()

            # Queue task should be cancelled
            mock_task.cancel.assert_called()
