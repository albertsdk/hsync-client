# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for remote_events function in websocket_handler module."""

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
