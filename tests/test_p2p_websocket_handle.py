"""
Tests for P2P event handling and brace escaping functionality.
"""

from unittest.mock import patch

import pytest

from hsync_client.p2p_websocket import P2PSlaveFileEventHandler, _handle_p2p_event


class TestHandleLanEvent:
    """Tests for _handle_p2p_event function."""

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
            await _handle_p2p_event(event_data)

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
            await _handle_p2p_event(event_data)

            mock_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_duplicate_dir_events_shared_context(self, tmp_path):
        """Regression: the P2P dispatch context must be shared across events.

        A single mkdir broadcast produces a burst of near-identical
        added/modified directory events; with a shared per-connection context
        the inbound directory dedup suppresses the duplicates (one INFO
        [REMOTE CREATE] line), while a genuine permission change is still
        logged.
        """
        from loguru import logger

        from hsync_client.event_dispatch import EventDispatchContext
        from hsync_client.logger import _format_record_plain

        ctx = EventDispatchContext()  # the per-connection context

        captured: list[str] = []

        def sink(message):
            captured.append(str(message))

        handler_id = logger.add(sink, level="INFO", format=_format_record_plain, catch=False)
        try:
            config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
            with (
                patch("hsync_client.event_dispatch.CONFIG", config),
                patch("hsync_client.sync.CONFIG", config),
            ):
                event = {"name": "added", "src": {"path": "d", "permissions": "755"}}
                await _handle_p2p_event(event, ctx=ctx)  # first: applied + logged
                await _handle_p2p_event(event, ctx=ctx)  # duplicate burst: suppressed
                changed = {"name": "added", "src": {"path": "d", "permissions": "700"}}
                await _handle_p2p_event(changed, ctx=ctx)  # real permission change
        finally:
            logger.remove(handler_id)

        creates = [m for m in captured if "[REMOTE CREATE]" in m]
        assert len(creates) == 2, captured

    @pytest.mark.asyncio
    async def test_converged_dir_event_logged_at_debug(self, tmp_path):
        """An inbound directory event that changes nothing locally is a converged
        no-op: the directory already exists with the permissions the event
        carries. It must not log at INFO and must not emit a transfer UI event
        (it is the echo loopback of this node's own POST /dirs, visible only
        when no P2P key enables CID echo suppression); a genuine creation or
        permission change stays INFO (covered elsewhere).
        """
        from hsync_common.permissions import mode_to_octal
        from loguru import logger

        from hsync_client.event_dispatch import EventDispatchContext
        from hsync_client.logger import _format_record_plain

        ctx = EventDispatchContext()
        captured: list[str] = []

        def sink(message):
            captured.append(str(message))

        handler_id = logger.add(sink, level="DEBUG", format=_format_record_plain, catch=False)
        mock_emit = None
        try:
            config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
            with (
                patch("hsync_client.event_dispatch.CONFIG", config),
                patch("hsync_client.sync.CONFIG", config),
                patch("hsync_client.event_dispatch.emit_event") as mock_emit,
            ):
                # Pre-create the directory as our own local mkdir would, and
                # send an added event carrying its actual permissions.
                echo_dir = tmp_path / "echo"
                echo_dir.mkdir()
                actual = mode_to_octal(echo_dir.stat().st_mode)
                event = {"name": "added", "src": {"path": "echo", "permissions": actual}}
                await _handle_p2p_event(event, ctx=ctx)
        finally:
            logger.remove(handler_id)

        info_lines = [m for m in captured if "INFO" in m and "REMOTE" in m]
        assert info_lines == [], captured
        assert any("[REMOTE CREATE]" in m and "Already converged" in m for m in captured)
        assert mock_emit is not None and mock_emit.call_count == 0

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
            await _handle_p2p_event(event_data)

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
            await _handle_p2p_event(event_data)

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
            await _handle_p2p_event(event_data)

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
            await _handle_p2p_event(event_data)

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
            await _handle_p2p_event(event_data)

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
            await _handle_p2p_event(event_data)

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
            await _handle_p2p_event(event_data)

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
            await _handle_p2p_event(event_data)

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
            await _handle_p2p_event(event_data)
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
            await _handle_p2p_event(event_data)

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
            await _handle_p2p_event(event_data)

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
            await _handle_p2p_event(event_data)

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

        handler = P2PSlaveFileEventHandler("host", 9753, use_https=False)

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_file"),
        ):
            await _handle_p2p_event(event_data, slave_handler=handler)

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
            await _handle_p2p_event(event_data)
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
        handler = P2PSlaveFileEventHandler("host", 9753, use_https=False)
        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_p2p_event(event_data, slave_handler=handler)
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
        handler = P2PSlaveFileEventHandler("host", 9753, use_https=False)
        with patch("hsync_client.event_dispatch.CONFIG", config):
            await _handle_p2p_event(event_data, slave_handler=handler)

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
            await _handle_p2p_event(event_data)
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
            await _handle_p2p_event(event_data)
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
            await _handle_p2p_event(event_data)
            mock_update.assert_called_once()


class TestInboundOriginTracker:
    """Tests for the inbound origin tracking helpers used for echo suppression."""

    def setup_method(self):
        pass

    def test_mark_and_consume_returns_cid(self):
        from hsync_client.p2p_websocket import consume_inbound_origin, mark_inbound_origin

        mark_inbound_origin("/bucket/a.txt", "slave-1")
        assert consume_inbound_origin("/bucket/a.txt") == "slave-1"

    def test_consume_is_one_shot(self):
        from hsync_client.p2p_websocket import consume_inbound_origin, mark_inbound_origin

        mark_inbound_origin("/bucket/a.txt", "slave-1")
        assert consume_inbound_origin("/bucket/a.txt") == "slave-1"
        # Second consume returns None: the entry has been removed.
        assert consume_inbound_origin("/bucket/a.txt") is None

    def test_consume_unknown_path_returns_none(self):
        from hsync_client.p2p_websocket import consume_inbound_origin

        assert consume_inbound_origin("/bucket/never_marked.txt") is None

    def test_mark_empty_inputs_noop(self):
        from hsync_client.p2p_websocket import consume_inbound_origin, mark_inbound_origin

        mark_inbound_origin("", "slave-1")
        mark_inbound_origin("/bucket/x", "")
        assert consume_inbound_origin("") is None
        assert consume_inbound_origin("/bucket/x") is None

    def test_expired_entry_returns_none(self):
        from hsync_client.p2p_websocket import consume_inbound_origin, mark_inbound_origin

        mark_inbound_origin("/bucket/a.txt", "slave-1", ttl=-1.0)
        assert consume_inbound_origin("/bucket/a.txt") is None


class TestSlaveRecordsLocalDeletionMarker:
    """Regression: a slave-side deletion must survive locally.

    When the master is unreachable at deletion time, the best-effort HTTP
    DELETE is lost; the causal tombstone recorded by the slave is what lets
    the next reconciliation delete the stale remote copy instead of
    downloading it back (ADR-0003).
    """

    def test_on_deleted_records_causal_tombstone(self, tmp_path):
        from unittest.mock import MagicMock

        from hsync_common.models.file import File as FileModel
        from hsync_common.models.tombstone import tombstone_key

        from hsync_client import sync_state as ss_mod
        from hsync_client import tombstones as ts_mod

        target = tmp_path / "gone.txt"
        target.write_text("content")
        file_obj = FileModel.get(str(target))
        assert file_obj is not None
        base_hash = file_obj.sha256sum

        cid = "SlaveLocal1234567890A"
        cfg = {
            "LOCAL_BUCKET": str(tmp_path),
            "CID": cid,
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        # A tracked file: the marker must inherit the recorded hash + clock.
        with patch("hsync_client.state_backend.CONFIG", cfg):
            ss_mod.record_base(str(target), base_hash or "")
            ss_mod.flush()
            ss_mod._stores.clear()

        handler = P2PSlaveFileEventHandler("host", 9753, use_https=False)
        # The file is already gone when the watchdog delivers the event.
        target.unlink()

        event = MagicMock()
        event.is_directory = False
        event.src_path = str(target)

        with (
            # No real thread toward a fake master: the HTTP DELETE is
            # irrelevant to the tombstone guarantee under test.
            patch.object(handler, "_start_operation"),
            patch("hsync_client.p2p_websocket.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            handler.on_deleted(event)
            handler._flush_pending_deletes()

        with patch("hsync_client.state_backend.CONFIG", cfg):
            payload = ts_mod.get_payload()
            key = tombstone_key("gone.txt")
            assert key in payload, "slave did not record a local deletion marker"
            assert payload[key]["sha256"] == base_hash
            # The inherited clock is ticked by the local cid (causal provenance).
            assert payload[key]["vclock"] == {cid: 1}

        with patch("hsync_client.state_backend.CONFIG", cfg):
            # The merge base is dropped: a re-created path starts fresh.
            assert ss_mod.get_base_hash(str(target)) is None

    def test_visible_to_hidden_move_records_tombstone(self, tmp_path):
        from unittest.mock import MagicMock

        from hsync_common.models.file import File as FileModel
        from hsync_common.models.tombstone import tombstone_key

        from hsync_client import sync_state as ss_mod
        from hsync_client import tombstones as ts_mod

        target = tmp_path / "data.txt"
        hidden_dest = tmp_path / ".goutputstream-data.txt"
        target.write_text("content")
        file_obj = FileModel.get(str(target))
        assert file_obj is not None
        base_hash = file_obj.sha256sum

        cid = "SlaveLocal1234567890A"
        cfg = {
            "LOCAL_BUCKET": str(tmp_path),
            "CID": cid,
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        with patch("hsync_client.state_backend.CONFIG", cfg):
            ss_mod.record_base(str(target), base_hash or "")
            ss_mod.flush()
            ss_mod._stores.clear()

        handler = P2PSlaveFileEventHandler("host", 9753, use_https=False)
        target.unlink()

        event = MagicMock()
        event.is_directory = False
        event.src_path = str(target)
        event.dest_path = str(hidden_dest)

        def _fake_hidden(path: str) -> bool:
            return path == str(hidden_dest)

        with (
            patch.object(handler, "_start_operation"),
            patch("hsync_client.p2p_websocket.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
            patch("hsync_client.p2p_websocket.is_hidden_file", side_effect=_fake_hidden),
        ):
            handler.on_moved(event)

        with patch("hsync_client.state_backend.CONFIG", cfg):
            key = tombstone_key("data.txt")
            assert key in ts_mod.get_payload(), (
                "visible→hidden move did not record a local deletion marker"
            )


class TestEventLoopRemainsResponsive:
    """Regression test for the cascade where a multi-GB download blocked
    the asyncio loop and caused WebSocket keep-alive pings to time out.

    ``_handle_p2p_event`` must dispatch the blocking ``update_file`` call
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
        def slow_update(_src, **kwargs):
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
            await asyncio.gather(_handle_p2p_event(event_data), ticker())

        # The ticker must have produced multiple ticks during the slow
        # update — proving the event loop was never blocked.
        assert len(ticks) >= 4
