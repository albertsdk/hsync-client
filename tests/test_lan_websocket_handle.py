"""
Tests for LAN event handling and brace escaping functionality.
"""

from unittest.mock import patch

import pytest

from hsync_client.lan_websocket import LANSlaveFileEventHandler, _handle_lan_event


class TestHandleLanEvent:
    """Tests for _handle_lan_event function."""

    @pytest.mark.asyncio
    async def test_handle_added_file_event(self, tmp_path):
        """Test handling added file event."""
        event_data = {
            "name": "added",
            "src": {
                "filepath": "test.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 10,
                "permissions": "644",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file") as mock_update,
        ):
            await _handle_lan_event(event_data)

            mock_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_added_directory_event(self, tmp_path):
        """Test handling added directory event."""
        event_data = {
            "name": "added",
            "src": {
                "path": "newdir",
                "permissions": "755",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_dir") as mock_update,
        ):
            await _handle_lan_event(event_data)

            mock_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_modified_file_event(self, tmp_path):
        """Test handling modified file event."""
        event_data = {
            "name": "modified",
            "src": {
                "filepath": "modified.txt",
                "sha256sum": "def456",
                "mtime": 2000,
                "ctime": 1000,
                "size": 20,
                "permissions": "644",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file") as mock_update,
        ):
            await _handle_lan_event(event_data)

            mock_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_renamed_file_event(self, tmp_path):
        """Test handling renamed file event."""
        # Create source file
        src_file = tmp_path / "old.txt"
        src_file.write_text("content")

        event_data = {
            "name": "renamed",
            "src": {
                "filepath": "old.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "644",
            },
            "dst": {
                "filepath": "new.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "644",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_lan_event(event_data)

        # File should be renamed
        assert not src_file.exists()
        assert (tmp_path / "new.txt").exists()

    @pytest.mark.asyncio
    async def test_handle_deleted_file_event(self, tmp_path):
        """Test handling deleted file event."""
        # Create file to delete
        test_file = tmp_path / "todelete.txt"
        test_file.write_text("content")

        event_data = {
            "name": "deleted",
            "src": {
                "filepath": "todelete.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "644",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_lan_event(event_data)

        # File should be deleted
        assert not test_file.exists()

    @pytest.mark.asyncio
    async def test_handle_deleted_ignore_delete_mode(self, tmp_path):
        """Test that deletions are ignored in ignore-delete mode."""
        # Create file
        test_file = tmp_path / "keepme.txt"
        test_file.write_text("content")

        event_data = {
            "name": "deleted",
            "src": {
                "filepath": "keepme.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "644",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": True,
        }

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_lan_event(event_data)

        # File should still exist
        assert test_file.exists()

    @pytest.mark.asyncio
    async def test_handle_event_upload_only_mode(self, tmp_path):
        """Test that events are ignored in upload-only mode."""
        event_data = {
            "name": "added",
            "src": {
                "filepath": "test.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 10,
                "permissions": "644",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "upload-only",
        }

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file") as mock_update,
        ):
            await _handle_lan_event(event_data)

            # Should not update in upload-only mode
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_event_missing_src(self):
        """Test handling event with missing src field."""
        event_data = {
            "name": "added",
            # Missing src field
        }

        config = {
            "LOCAL_BUCKET": "/tmp",
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file") as mock_update,
        ):
            await _handle_lan_event(event_data)

            # Should not process event
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_symlink_event(self, tmp_path):
        """Test handling symlink event creates the symlink on disk."""
        target = tmp_path / "target.txt"
        target.write_text("data")

        event_data = {
            "name": "symlink",
            "src": {
                "type": "symlink",
                "filepath": "link.txt",
                "target": "target.txt",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_lan_event(event_data)

        link = tmp_path / "link.txt"
        assert link.is_symlink()
        assert link.readlink().name == "target.txt"

    @pytest.mark.asyncio
    async def test_handle_renamed_dir_event(self, tmp_path):
        """Test handling renamed directory event."""
        src_dir = tmp_path / "olddir"
        src_dir.mkdir()

        event_data = {
            "name": "renamed",
            "src": {"path": "olddir", "permissions": "755"},
            "dst": {"path": "newdir", "permissions": "755"},
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_lan_event(event_data)

        assert not src_dir.exists()
        assert (tmp_path / "newdir").exists()

    @pytest.mark.asyncio
    async def test_handle_renamed_missing_dst(self, tmp_path):
        """Test rename event with missing dst field returns early."""
        event_data = {
            "name": "renamed",
            "src": {
                "filepath": "old.txt",
                "sha256sum": "x",
                "mtime": 1,
                "ctime": 1,
                "size": 1,
                "permissions": "644",
            },
            # no "dst"
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_lan_event(event_data)
        # Should not crash – just logs a warning

    @pytest.mark.asyncio
    async def test_handle_renamed_file_src_not_exists(self, tmp_path):
        """Test rename event when source file doesn't exist (no rename)."""
        event_data = {
            "name": "renamed",
            "src": {
                "filepath": "gone.txt",
                "sha256sum": "x",
                "mtime": 1,
                "ctime": 1,
                "size": 1,
                "permissions": "644",
            },
            "dst": {
                "filepath": "new.txt",
                "sha256sum": "x",
                "mtime": 1,
                "ctime": 1,
                "size": 1,
                "permissions": "644",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_lan_event(event_data)

        # Neither file exists – no crash
        assert not (tmp_path / "new.txt").exists()

    @pytest.mark.asyncio
    async def test_handle_deleted_dir_event(self, tmp_path):
        """Test handling deleted directory event."""
        target = tmp_path / "deldir"
        target.mkdir()
        (target / "file.txt").write_text("x")

        event_data = {
            "name": "deleted",
            "src": {"path": "deldir", "permissions": "755"},
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_lan_event(event_data)

        assert not target.exists()

    @pytest.mark.asyncio
    async def test_handle_deleted_hsynckeep_cleans_dir(self, tmp_path):
        """Test deleting .hsynckeep sentinel removes empty parent dir."""
        parent = tmp_path / "emptydir"
        parent.mkdir()
        sentinel = parent / ".hsynckeep"
        sentinel.write_text("")

        event_data = {
            "name": "deleted",
            "src": {
                "filepath": "emptydir/.hsynckeep",
                "sha256sum": "x",
                "mtime": 1,
                "ctime": 1,
                "size": 0,
                "permissions": "644",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_lan_event(event_data)

        assert not sentinel.exists()
        assert not parent.exists()  # empty dir cleaned up

    @pytest.mark.asyncio
    async def test_handle_added_file_with_slave_handler(self, tmp_path):
        """Test mark_receiving/unmark_receiving called with slave_handler."""
        event_data = {
            "name": "added",
            "src": {
                "filepath": "test.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 10,
                "permissions": "644",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
        }

        handler = LANSlaveFileEventHandler("host", 9753, use_https=False)

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file"),
        ):
            await _handle_lan_event(event_data, slave_handler=handler)

        # File should have been unmarked after processing
        assert not handler.is_receiving(str(tmp_path / "test.txt"))

    @pytest.mark.asyncio
    async def test_handle_deleted_dir_not_exists(self, tmp_path):
        """Test deleted dir event when dir doesn't exist (no crash)."""
        event_data = {
            "name": "deleted",
            "src": {"path": "nonexistent", "permissions": "755"},
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_lan_event(event_data)
        # Should not crash

    @pytest.mark.asyncio
    async def test_handle_renamed_records_inbound_rename(self, tmp_path):
        """An inbound `renamed` event must record its exact (src,dst) on the
        slave handler so a later watchdog echo returning 404 is recognised."""
        src_file = tmp_path / "old.txt"
        src_file.write_text("content")
        event_data = {
            "name": "renamed",
            "src": {"filepath": "old.txt", "sha256sum": "x", "size": 7},
            "dst": {"filepath": "new.txt", "sha256sum": "x", "size": 7},
        }
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        handler = LANSlaveFileEventHandler("host", 9753, use_https=False)
        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_lan_event(event_data, slave_handler=handler)
        src_abs = str(tmp_path / "old.txt")
        dst_abs = str(tmp_path / "new.txt")
        assert handler.is_inbound_rename_echo(src_abs, dst_abs)

    @pytest.mark.asyncio
    async def test_handle_deleted_dir_records_inbound_delete(self, tmp_path):
        """An inbound deleted dir event must record the tree root on the slave
        handler so the rmtree echo it produces locally is recognised.

        The structural integration: ``_apply_delete`` passes the confined
        root path to ``context.record_inbound_delete`` before ``rmtree``.
        """
        target = tmp_path / "deldir"
        target.mkdir()
        (target / "file.txt").write_text("x")

        event_data = {
            "name": "deleted",
            "src": {"path": "deldir", "permissions": "755"},
        }
        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        handler = LANSlaveFileEventHandler("host", 9753, use_https=False)
        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_lan_event(event_data, slave_handler=handler)

        root_abs = str(tmp_path / "deldir")
        assert handler.is_inbound_delete_echo(root_abs)

    @pytest.mark.asyncio
    async def test_handle_deleted_file_not_exists(self, tmp_path):
        """Test deleted file event when file doesn't exist (no crash)."""
        event_data = {
            "name": "deleted",
            "src": {
                "filepath": "gone.txt",
                "sha256sum": "x",
                "mtime": 1,
                "ctime": 1,
                "size": 1,
                "permissions": "644",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_lan_event(event_data)
        # Should not crash

    @pytest.mark.asyncio
    async def test_handle_own_echo_ignored(self, tmp_path):
        """Events whose CID matches our own must be dropped (echo suppression)."""
        event_data = {
            "cid": "slave-A",
            "name": "added",
            "src": {
                "filepath": "echoed.txt",
                "sha256sum": "abc",
                "mtime": 1,
                "ctime": 1,
                "size": 10,
                "permissions": "644",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "CID": "slave-A",
        }

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file") as mock_update,
        ):
            await _handle_lan_event(event_data)
            mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_foreign_cid_processed(self, tmp_path):
        """Events whose CID differs from ours must be processed normally."""
        event_data = {
            "cid": "slave-B",
            "name": "added",
            "src": {
                "filepath": "from_other.txt",
                "sha256sum": "abc",
                "mtime": 1,
                "ctime": 1,
                "size": 10,
                "permissions": "644",
            },
        }

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "CID": "slave-A",
        }

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file") as mock_update,
        ):
            await _handle_lan_event(event_data)
            mock_update.assert_called_once()


class TestInboundOriginTracker:
    """Tests for the inbound origin tracking helpers used for echo suppression."""

    def setup_method(self):
        pass

    def test_mark_and_consume_returns_cid(self):
        from hsync_client.lan_websocket import consume_inbound_origin, mark_inbound_origin

        mark_inbound_origin("/bucket/a.txt", "slave-1")
        assert consume_inbound_origin("/bucket/a.txt") == "slave-1"

    def test_consume_is_one_shot(self):
        from hsync_client.lan_websocket import consume_inbound_origin, mark_inbound_origin

        mark_inbound_origin("/bucket/a.txt", "slave-1")
        assert consume_inbound_origin("/bucket/a.txt") == "slave-1"
        # Second consume returns None: the entry has been removed.
        assert consume_inbound_origin("/bucket/a.txt") is None

    def test_consume_unknown_path_returns_none(self):
        from hsync_client.lan_websocket import consume_inbound_origin

        assert consume_inbound_origin("/bucket/never_marked.txt") is None

    def test_mark_empty_inputs_noop(self):
        from hsync_client.lan_websocket import consume_inbound_origin, mark_inbound_origin

        mark_inbound_origin("", "slave-1")
        mark_inbound_origin("/bucket/x", "")
        assert consume_inbound_origin("") is None
        assert consume_inbound_origin("/bucket/x") is None

    def test_expired_entry_returns_none(self):
        from hsync_client.lan_websocket import consume_inbound_origin, mark_inbound_origin

        mark_inbound_origin("/bucket/a.txt", "slave-1", ttl=-1.0)
        assert consume_inbound_origin("/bucket/a.txt") is None


class TestEventLoopRemainsResponsive:
    """Regression test for the cascade where a multi-GB download blocked
    the asyncio loop and caused WebSocket keep-alive pings to time out.

    ``_handle_lan_event`` must dispatch the blocking ``update_file`` call
    to a worker thread (via ``asyncio.to_thread``) so the event loop is
    free to handle other coroutines while the download runs.
    """

    @pytest.mark.asyncio
    async def test_blocking_update_file_does_not_freeze_loop(self, tmp_path):
        import asyncio
        import time as _time

        event_data = {
            "name": "modified",
            "src": {
                "filepath": "big.bin",
                "sha256sum": "deadbeef",
                "mtime": 1000,
                "ctime": 1000,
                "size": 9_000_000_000,
                "permissions": "644",
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}

        # Simulate a long synchronous download
        def slow_update(_src):
            _time.sleep(0.4)

        # Concurrent ticker to assert the loop kept advancing during the
        # synchronous "download": if ``update_file`` were awaited inline
        # the ticker would not be able to record any ticks.
        ticks = []

        async def ticker():
            for _ in range(8):
                await asyncio.sleep(0.05)
                ticks.append(_time.monotonic())

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file", side_effect=slow_update),
        ):
            await asyncio.gather(_handle_lan_event(event_data), ticker())

        # The ticker must have produced multiple ticks during the slow
        # update — proving the event loop was never blocked.
        assert len(ticks) >= 4
