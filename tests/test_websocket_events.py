# HSYNC Client - HTTP-based file synchronization client
# Copyright (C) 2025 Alberto Basso
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Tests for WebSocket event processing, resync, and remote_connect functions."""

import asyncio
import pytest
from unittest.mock import patch, AsyncMock


class TestProcessWebsocketEvents:
    """Tests for _process_websocket_events function."""

    @pytest.mark.asyncio
    async def test_processes_added_directory_event(self):
        """Processes added directory event correctly."""
        from hsync_client.websocket_handler import _process_websocket_events
        from json import dumps

        message = {
            "name": "added",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "newdir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.update_dir') as mock_update:
            try:
                await _process_websocket_events(websocket, "testbucket")
            except Exception:
                pass
            mock_update.assert_called_once_with(message["src"])

    @pytest.mark.asyncio
    async def test_processes_added_file_event(self):
        """Processes added file event correctly."""
        from hsync_client.websocket_handler import _process_websocket_events
        from json import dumps

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

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.update_file') as mock_update:
            try:
                await _process_websocket_events(websocket, "testbucket")
            except Exception:
                pass
            mock_update.assert_called_once_with(message["src"])

    @pytest.mark.asyncio
    async def test_skips_event_from_same_client(self):
        """Skips events from the same client (echo prevention)."""
        from hsync_client.websocket_handler import _process_websocket_events
        from json import dumps

        message = {
            "name": "added",
            "cid": "my_client",  # Same as CONFIG["CID"]
            "bucket": "testbucket",
            "src": {"path": "newdir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.update_dir') as mock_update:
            try:
                await _process_websocket_events(websocket, "testbucket")
            except Exception:
                pass
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_event_from_different_bucket(self):
        """Skips events from different buckets."""
        from hsync_client.websocket_handler import _process_websocket_events
        from json import dumps

        message = {
            "name": "added",
            "cid": "other_client",
            "bucket": "otherbucket",  # Different bucket
            "src": {"path": "newdir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.update_dir') as mock_update:
            try:
                await _process_websocket_events(websocket, "testbucket")
            except Exception:
                pass
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_invalid_event_type(self):
        """Skips events with invalid event type."""
        from hsync_client.websocket_handler import _process_websocket_events
        from json import dumps

        message = {
            "name": "invalid_event",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "newdir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.update_dir') as mock_update:
            try:
                await _process_websocket_events(websocket, "testbucket")
            except Exception:
                pass
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_processes_renamed_event(self):
        """Processes renamed event correctly."""
        from hsync_client.websocket_handler import _process_websocket_events
        from json import dumps

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

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler._handle_rename_event') as mock_rename:
            try:
                await _process_websocket_events(websocket, "testbucket")
            except Exception:
                pass
            mock_rename.assert_called_once_with(message["src"], message["dst"])

    @pytest.mark.asyncio
    async def test_processes_deleted_event(self):
        """Processes deleted event correctly."""
        from hsync_client.websocket_handler import _process_websocket_events
        from json import dumps

        message = {
            "name": "deleted",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "deleteddir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler._handle_delete_event') as mock_delete:
            try:
                await _process_websocket_events(websocket, "testbucket")
            except Exception:
                pass
            mock_delete.assert_called_once_with(message["src"])

    @pytest.mark.asyncio
    async def test_processes_modified_directory_event(self):
        """Processes modified directory event correctly."""
        from hsync_client.websocket_handler import _process_websocket_events
        from json import dumps

        message = {
            "name": "modified",
            "cid": "other_client",
            "bucket": "testbucket",
            "src": {"path": "modifieddir", "permissions": "drwxr-xr-x"},
        }

        websocket = AsyncMock()
        websocket.recv = AsyncMock(side_effect=[dumps(message), Exception("Stop")])

        config = {"CID": "my_client"}

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.update_dir') as mock_update:
            try:
                await _process_websocket_events(websocket, "testbucket")
            except Exception:
                pass
            mock_update.assert_called_once_with(message["src"])

    @pytest.mark.asyncio
    async def test_processes_modified_file_event(self):
        """Processes modified file event correctly."""
        from hsync_client.websocket_handler import _process_websocket_events
        from json import dumps

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

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.update_file') as mock_update:
            try:
                await _process_websocket_events(websocket, "testbucket")
            except Exception:
                pass
            mock_update.assert_called_once_with(message["src"])

    @pytest.mark.asyncio
    async def test_skips_symlink_event(self):
        """Skips symlink events (not in valid event types)."""
        from hsync_client.websocket_handler import _process_websocket_events
        from json import dumps

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

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler._handle_symlink_event') as mock_symlink:
            try:
                await _process_websocket_events(websocket, "testbucket")
            except Exception:
                pass
            # Symlink events are filtered out as invalid
            mock_symlink.assert_not_called()


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

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.sync', mock_sync):
            try:
                await resync()
            except Exception:
                pass
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

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.sync', mock_sync), \
             patch('hsync_client.websocket_handler.remote_events', mock_remote_events):
            try:
                await asyncio.wait_for(remote_connect(), timeout=1.0)
            except (asyncio.TimeoutError, KeyboardInterrupt, Exception):
                pass
            assert sync_called

    @pytest.mark.asyncio
    async def test_remote_connect_starts_resync_task_when_configured(self):
        """remote_connect starts resync task when RESYNC is configured."""
        from hsync_client.websocket_handler import remote_connect
        import hsync_client.websocket_handler as ws_module

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

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.sync', mock_sync), \
             patch('hsync_client.websocket_handler.remote_events', mock_remote_events), \
             patch('hsync_client.websocket_handler.resync', mock_resync):
            # Reset global task
            ws_module._resync_task = None
            try:
                await asyncio.wait_for(remote_connect(), timeout=1.0)
            except (asyncio.TimeoutError, KeyboardInterrupt, Exception):
                pass
            # Task should have been created
            assert task_created

    @pytest.mark.asyncio
    async def test_remote_connect_handles_connection_closed(self):
        """remote_connect handles ConnectionClosed and reconnects."""
        from hsync_client.websocket_handler import remote_connect
        from websockets.exceptions import ConnectionClosed

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

        with patch('hsync_client.websocket_handler.CONFIG', config), \
             patch('hsync_client.websocket_handler.sync', mock_sync), \
             patch('hsync_client.websocket_handler.remote_events', mock_remote_events):
            try:
                await asyncio.wait_for(remote_connect(), timeout=2.0)
            except (asyncio.TimeoutError, KeyboardInterrupt, Exception):
                pass
            assert call_count >= 2  # Should have reconnected
