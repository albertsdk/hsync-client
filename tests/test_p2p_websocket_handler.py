"""
Tests for P2P file event handler (master side) functionality.
"""

from unittest.mock import MagicMock, patch

import pytest
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from hsync_client.p2p_websocket import P2PMasterFileEventHandler, P2PWebSocketServer


class TestLANFileEventHandler:
    """Tests for P2PMasterFileEventHandler class."""

    @pytest.fixture
    def ws_server(self):
        """Create a mock WebSocket server."""
        server = MagicMock(spec=P2PWebSocketServer)
        return server

    @pytest.fixture
    def handler(self, ws_server):
        """Create a P2PMasterFileEventHandler instance."""
        return P2PMasterFileEventHandler(ws_server)

    def test_handler_creation(self, ws_server):
        """Test creating a file event handler."""
        handler = P2PMasterFileEventHandler(ws_server)
        assert handler._ws_server is ws_server
        assert handler._last_broadcast_stat == {}
        assert handler._last_broadcast_hash == {}

    def test_a_served_change_reports_the_classification_hook(self, handler, ws_server):
        """The hook fires before the direction guard: download-only still re-classifies.

        A device's upload lands in the served share, so the serving role must
        re-classify even when the master broadcasts nothing.
        """
        config = {"SYNC_DIRECTION": "download-only"}
        with patch("hsync_client.p2p_websocket.CONFIG", config):
            handler._broadcast_event(FileCreatedEvent("/share/a.bin"))

        ws_server._notify_local_change.assert_called_once_with()

    def test_the_local_change_hook_is_optional_and_clearable(self):
        server = P2PWebSocketServer(port=1)
        server._notify_local_change()  # no callback installed: a no-op

        calls: list[int] = []
        server.set_local_change_callback(lambda: calls.append(1))
        server._notify_local_change()
        server.set_local_change_callback(None)
        server._notify_local_change()

        assert calls == [1]

    def test_on_created_directory(self, handler, ws_server, tmp_path):
        """Test handling directory creation event."""
        test_dir = tmp_path / "newdir"
        test_dir.mkdir()

        event = DirCreatedEvent(str(test_dir))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as mock_dir,
        ):
            mock_dir_obj = MagicMock()
            mock_dir.get.return_value = mock_dir_obj

            handler.on_created(event)

            # Should broadcast the event
            ws_server.broadcast_sync.assert_called_once()

    def test_on_created_file_symlink(self, handler, ws_server, tmp_path):
        """Test handling symlink creation event."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        event = FileCreatedEvent(str(link))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Symlink") as mock_symlink,
        ):
            mock_symlink_obj = MagicMock()
            mock_symlink.get.return_value = mock_symlink_obj

            handler.on_created(event)

            # Should broadcast the symlink event
            ws_server.broadcast_sync.assert_called_once()

    def test_on_created_os_cache_directory(self, handler, ws_server, tmp_path):
        """Test that OS cache directories (e.g. .DS_Store) are ignored."""
        cache_dir = tmp_path / ".DS_Store"
        cache_dir.mkdir()

        event = DirCreatedEvent(str(cache_dir))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
        }

        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_created(event)

            # Should not broadcast
            ws_server.broadcast_sync.assert_not_called()

    def test_on_created_directory_duplicate_burst_suppressed(self, handler, ws_server, tmp_path):
        """Regression: a duplicate DirCreatedEvent burst is not re-broadcast.

        Watchdog emits a synthetic DirCreatedEvent for children already
        present whenever a watch is scheduled on a fresh directory, so a
        chain of mkdirs can fire two DirCreatedEvent for the same directory
        within milliseconds. The second identical event must be suppressed
        (stat dedup), the re-created directory after a delete must not be.
        """
        test_dir = tmp_path / "dupd"
        test_dir.mkdir()

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as mock_dir,
        ):
            mock_dir_obj = MagicMock()
            mock_dir.get.return_value = mock_dir_obj

            handler.on_created(DirCreatedEvent(str(test_dir)))
            # Duplicate arrival of the same create (synthetic re-scan burst).
            handler.on_created(DirCreatedEvent(str(test_dir)))

        # One broadcast, not two: the duplicate was stat-suppressed.
        ws_server.broadcast_sync.assert_called_once()

    def test_on_created_directory_recreated_after_delete_broadcasts(
        self, handler, ws_server, tmp_path
    ):
        """A directory deleted and re-created is a real change: both broadcast.

        The delete flush invalidates the dedup cache for the path, so the
        re-creation (even with an identical stat) must not be skipped as a
        duplicate create.
        """
        test_dir = tmp_path / "recreated"
        test_dir.mkdir()

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
            "DELETION_POLICY": "propagate",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.Dir") as mock_dir,
            patch("hsync_client.file_event_handler.threading.Timer"),
        ):
            mock_dir_obj = MagicMock()
            mock_dir.get.return_value = mock_dir_obj

            handler.on_created(DirCreatedEvent(str(test_dir)))
            handler.on_deleted(DirDeletedEvent(str(test_dir)))
            handler._flush_pending_deletes()
            ws_server.broadcast_sync.reset_mock()

            # Simulate the burst the mkdir of the re-created directory can
            # produce: two identical DirCreatedEvent for the same path.
            handler.on_created(DirCreatedEvent(str(test_dir)))
            handler.on_created(DirCreatedEvent(str(test_dir)))

        # One broadcast for the re-create (the duplicate burst was
        # stat-suppressed); the earlier create/delete were already counted.
        assert ws_server.broadcast_sync.call_count == 1

    def test_dir_rename_invalidates_create_dedup(self, handler, ws_server, tmp_path):
        """Regression: a directory renamed away vacates its source path.

        Renaming preserves the directory's mtime, so re-creating it at the
        old path (e.g. an undone rename) must not be stat-suppressed by the
        directory-create dedup; the duplicate burst of that re-create is
        still deduplicated.
        """
        src_dir = tmp_path / "oldname"
        src_dir.mkdir()
        dst_dir = tmp_path / "newname"

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as mock_dir,
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
        ):
            mock_dir_obj = MagicMock()
            mock_dir.get.return_value = mock_dir_obj

            # First create at src: broadcast + dedup record.
            handler.on_created(DirCreatedEvent(str(src_dir)))

            # Rename src -> dst on disk (preserves mtime) and fire on_moved.
            src_dir.rename(dst_dir)
            handler.on_moved(DirMovedEvent(str(src_dir), str(dst_dir)))

            # The vacated source path must be gone from the dedup cache.
            assert str(src_dir) not in handler._last_broadcast_stat

            # Undo the rename: same directory, same inode, same mtime.
            dst_dir.rename(src_dir)
            ws_server.broadcast_sync.reset_mock()

            # The re-create must broadcast (not be stat-suppressed), while
            # the duplicate burst is still deduplicated.
            handler.on_created(DirCreatedEvent(str(src_dir)))
            handler.on_created(DirCreatedEvent(str(src_dir)))

        assert ws_server.broadcast_sync.call_count == 1

    def test_on_modified_directory(self, handler, ws_server, tmp_path):
        """Test handling directory modification event."""
        test_dir = tmp_path / "moddir"
        test_dir.mkdir()

        event = DirCreatedEvent(str(test_dir))
        event.event_type = "modified"

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as mock_dir,
        ):
            mock_dir_obj = MagicMock()
            mock_dir.get.return_value = mock_dir_obj

            handler.on_modified(event)

            # Should broadcast the event
            ws_server.broadcast_sync.assert_called_once()

    def test_on_closed_file(self, handler, ws_server, tmp_path):
        """Test handling file close event."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        event = FileModifiedEvent(str(test_file))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.p2p_websocket.CONFIG", config),
            patch("hsync_client.file_event_handler.File") as mock_file,
        ):
            mock_file_obj = MagicMock()
            mock_file_obj.filepath = str(test_file)
            mock_file.get.return_value = mock_file_obj

            handler.on_closed(event)

            # Should broadcast the event
            ws_server.broadcast_sync.assert_called_once()

    def test_in_tree_file_rename_records_a_deletion_marker(self, handler, tmp_path):
        """A rename vacates the source path: an offline peer needs the marker."""
        src = tmp_path / "x.jpg"
        src.write_text("content")
        dst = tmp_path / "x_2.jpg"
        src.rename(dst)

        with (
            patch.object(handler, "_on_deleted") as marker,
            patch.object(handler, "_make_event", return_value=MagicMock()),
            patch.object(handler, "_broadcast_event"),
            patch("hsync_client.file_event_handler.File") as mock_file,
        ):
            mock_file.get.return_value = MagicMock()
            handler.on_moved(FileMovedEvent(str(src), str(dst)))

        marker.assert_called_once_with(str(src), is_directory=False)

    def test_in_tree_directory_rename_marks_every_vacated_file(self, handler, tmp_path):
        """Directories carry no tombstones: each vacated child needs one."""
        old_dir = tmp_path / "old"
        old_dir.mkdir()
        (old_dir / "a.txt").write_text("a")
        (old_dir / "b.txt").write_text("b")
        new_dir = tmp_path / "new"
        old_dir.rename(new_dir)

        with (
            patch.object(handler, "_on_deleted") as marker,
            patch.object(handler, "_make_event", return_value=MagicMock()),
            patch.object(handler, "_broadcast_event"),
            patch("hsync_client.file_event_handler.Dir") as mock_dir,
        ):
            mock_dir.get.return_value = MagicMock()
            handler.on_moved(DirMovedEvent(str(old_dir), str(new_dir)))

        marked = {call.args[0] for call in marker.call_args_list}
        assert marked == {str(old_dir / "a.txt"), str(old_dir / "b.txt")}
        assert all(call.kwargs["is_directory"] is False for call in marker.call_args_list)

    def test_in_tree_symlink_rename_publishes_the_link(self, handler, tmp_path):
        """A renamed symlink is replicated as a link, never as its target's bytes."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        src = tmp_path / "link"
        src.symlink_to(target)
        dst = tmp_path / "link2"
        src.rename(dst)

        with (
            patch.object(handler, "on_deleted") as deleted,
            patch.object(handler, "_broadcast_symlink") as broadcast,
            patch("hsync_client.file_event_handler.File") as mock_file,
        ):
            handler.on_moved(FileMovedEvent(str(src), str(dst)))

        deleted.assert_called_once()
        broadcast.assert_called_once_with(str(dst))
        # The File branch would stat the target and lie about the entry type.
        mock_file.get.assert_not_called()

    def test_a_marker_dominates_the_version_a_peer_downloaded(self, tmp_path):
        """The marker must be newer than the clock peers recorded on download.

        A master only serves the files it owns, so it never stored their clock:
        without the first-tick fallback the marker would tie with the version a
        peer downloaded and the resurrection guard would not fire.
        """
        from hsync_common.models.tombstone import tombstone_key

        from hsync_client import tombstones as ts_mod
        from hsync_client.p2p_websocket import _record_deletion_marker
        from hsync_client.sync import _tombstone_clock_dominates

        cid = "TestMaster12345678901"
        config = {"LOCAL_BUCKET": str(tmp_path), "CID": cid}
        with (
            patch("hsync_client.p2p_websocket.CONFIG", config),
            patch("hsync_client.state_backend.CONFIG", config),
        ):
            _record_deletion_marker(str(tmp_path / "x.jpg"))
            entry = ts_mod.get_payload()[tombstone_key("x.jpg")]

        assert entry["vclock"] == {cid: 2}
        assert _tombstone_clock_dominates(entry, {cid: 1}) is True

    def test_peek_inbound_origin(self):
        """Test peek_inbound_origin returns the CID without consuming it."""
        from hsync_client.p2p_websocket import (
            consume_inbound_origin,
            mark_inbound_origin,
            peek_inbound_origin,
        )

        mark_inbound_origin("/tmp/test.txt", "test-cid-123")
        assert peek_inbound_origin("/tmp/test.txt") == "test-cid-123"
        # peek does not consume
        assert peek_inbound_origin("/tmp/test.txt") == "test-cid-123"
        # consume does
        assert consume_inbound_origin("/tmp/test.txt") == "test-cid-123"
        assert peek_inbound_origin("/tmp/test.txt") is None

    def test_origin_cid_on_rename_log(self, handler, tmp_path):
        """Test _origin_cid returns the origin CID for a marked path."""
        from hsync_client.p2p_websocket import mark_inbound_origin

        src = str(tmp_path / "old.txt")
        dst = str(tmp_path / "new.txt")
        mark_inbound_origin(src, "peer-cid-456")
        mark_inbound_origin(dst, "peer-cid-456")

        assert handler._origin_cid(src, dst) == "peer-cid-456"

    def test_on_file_ready_logs_local_modify_with_trigger(self, handler, ws_server, tmp_path):
        """_on_file_ready logs [LOCAL CREATE] with the (closed)/(modified) trigger.

        Without an inbound-origin marker the event originates from the
        master's own watcher, and since it is the first time the path is
        seen, the label is LOCAL CREATE (not LOCAL MODIFY).
        The trigger suffix distinguishes an ``IN_CLOSE_WRITE`` fire
        (``closed``) from the ``on_modified`` debounce fallback (``modified``).
        """
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        file_obj = MagicMock()
        file_obj.filepath = str(test_file)

        with (
            patch("hsync_client.p2p_websocket.CONFIG", config),
            patch("hsync_client.p2p_websocket.log") as mock_log,
        ):
            handler._on_file_ready(file_obj, str(test_file), from_closed=True)
            handler._on_file_ready(file_obj, str(test_file), from_closed=False)

            msgs = [str(c.args[0]) if c.args else str(c) for c in mock_log.info.call_args_list]
            assert any("[LOCAL CREATE]" in m and "(closed)" in m for m in msgs), msgs
            assert any("[LOCAL CREATE]" in m and "(modified)" in m for m in msgs), msgs

    def test_on_file_ready_logs_remote_modify_with_origin_marker(
        self, handler, ws_server, tmp_path
    ):
        """_on_file_ready logs [REMOTE CREATE] when the path was slave-written.

        When the master applies a peer upload via the HTTP API, the
        inbound-origin map attributes the resulting watchdog event to the
        peer: the label must be REMOTE-prefixed and carry the CID suffix.
        """
        from hsync_client.p2p_websocket import mark_inbound_origin

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        mark_inbound_origin(str(test_file), "peer-cid-789")

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        file_obj = MagicMock()
        file_obj.filepath = str(test_file)

        with (
            patch("hsync_client.p2p_websocket.CONFIG", config),
            patch("hsync_client.p2p_websocket.log") as mock_log,
        ):
            handler._on_file_ready(file_obj, str(test_file), from_closed=True)

            msgs = [str(c.args[0]) if c.args else str(c) for c in mock_log.info.call_args_list]
            assert any("[REMOTE CREATE]" in m and "(closed)" in m for m in msgs), msgs
            assert any("(CID: peer-cid-789)" in m for m in msgs), msgs

    def test_on_deleted_logs_remote_delete_with_origin_marker(self, handler, ws_server, tmp_path):
        """on_deleted logs [REMOTE DELETE] when the path was deleted by a peer.

        When the master applies a slave-initiated delete via the HTTP API,
        the inbound-origin map attributes the resulting watchdog event to
        the peer: the label must be REMOTE-prefixed and carry the CID suffix.
        """
        from hsync_client.p2p_websocket import mark_inbound_origin

        test_file = tmp_path / "deleted.txt"
        mark_inbound_origin(str(test_file), "peer-cid-321")

        event = FileDeletedEvent(str(test_file))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
            "DELETION_POLICY": "propagate",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.log") as mock_log,
            patch("hsync_client.file_event_handler.threading.Timer"),
        ):
            handler.on_deleted(event)
            handler._flush_pending_deletes()

            msgs = [str(c.args[0]) if c.args else str(c) for c in mock_log.info.call_args_list]
            assert any("[REMOTE DELETE]" in m for m in msgs), msgs
            assert any("(CID: peer-cid-321)" in m for m in msgs), msgs
            ws_server.broadcast_sync.assert_called_once()

    def test_watcher_logs_local_when_origin_marker_expired(self, handler, ws_server, tmp_path):
        """An expired inbound-origin marker degrades the label to LOCAL.

        Pin the known TTL limitation (INBOUND_ORIGIN_TTL): if the watchdog
        event arrives after the marker expired, the line prints LOCAL even
        though the change was peer-caused — the CID suffix degrades together
        with the prefix, keeping the line internally coherent.
        """
        from hsync_client.p2p_websocket import mark_inbound_origin

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        mark_inbound_origin(str(test_file), "peer-cid-expired", ttl=-1.0)

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        file_obj = MagicMock()
        file_obj.filepath = str(test_file)

        with (
            patch("hsync_client.p2p_websocket.CONFIG", config),
            patch("hsync_client.p2p_websocket.log") as mock_log,
        ):
            handler._on_file_ready(file_obj, str(test_file), from_closed=True)

            msgs = [str(c.args[0]) if c.args else str(c) for c in mock_log.info.call_args_list]
            assert any("[LOCAL CREATE]" in m for m in msgs), msgs
            assert not any("(CID:" in m for m in msgs), msgs

    def test_on_deleted_file(self, handler, ws_server, tmp_path):
        """Test handling file deletion event."""
        test_file = tmp_path / "deleted.txt"

        event = FileDeletedEvent(str(test_file))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
            "DELETION_POLICY": "propagate",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.threading.Timer"),
        ):
            handler.on_deleted(event)
            # Deletes are coalesced: the broadcast happens on flush.
            handler._flush_pending_deletes()

            # Should broadcast the event
            ws_server.broadcast_sync.assert_called_once()

    def test_on_deleted_keep_policy(self, handler, ws_server, tmp_path):
        """Deletions are ignored when the policy is keep."""
        test_file = tmp_path / "deleted.txt"

        event = FileDeletedEvent(str(test_file))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "DELETION_POLICY": "keep",
        }

        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_deleted(event)

            # Should not broadcast
            ws_server.broadcast_sync.assert_not_called()

    def test_send_event_download_only_mode(self, handler, ws_server):
        """Test that events are not sent in download-only mode."""
        from hsync_common.models.event import Event, EventName
        from hsync_common.models.file import File

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": "/test",
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "download-only",
        }

        file_obj = File("/test/file.txt")
        event = Event("test-client", "/test", "remote", EventName.added, file_obj)

        with patch("hsync_client.p2p_websocket.CONFIG", config):
            handler._broadcast_event(event)

            # Should not broadcast in download-only mode
            ws_server.broadcast_sync.assert_not_called()

    def test_send_event_normal_broadcasts(self, handler, ws_server):
        """Test that events are broadcast in normal mode."""
        from hsync_common.models.event import Event, EventName
        from hsync_common.models.file import File

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": "/test",
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
        }

        file_obj = File("/test/file.txt")
        event = Event("test-client", "/test", "remote", EventName.added, file_obj)

        with patch("hsync_client.p2p_websocket.CONFIG", config):
            handler._broadcast_event(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_any_event_os_cache_dir_skipped(self, handler, tmp_path):
        """Test on_any_event returns early for OS cache directory."""
        cache = tmp_path / ".DS_Store"
        cache.mkdir()
        event = DirCreatedEvent(str(cache))
        # Should not raise
        handler.on_any_event(event)

    def test_on_any_event_os_cache_file_skipped(self, handler, tmp_path):
        """Test on_any_event returns early for OS cache file."""
        cache = tmp_path / "Thumbs.db"
        cache.write_text("x")
        event = FileCreatedEvent(str(cache))
        handler.on_any_event(event)

    def test_on_any_event_visible_logs(self, handler, tmp_path):
        """Test on_any_event logs visible event."""
        visible = tmp_path / "file.txt"
        visible.write_text("x")
        event = FileCreatedEvent(str(visible))
        handler.on_any_event(event)  # just exercises the log line

    def test_on_created_dir_get_returns_none(self, handler, ws_server, tmp_path):
        """Test on_created when Dir.get returns None."""
        test_dir = tmp_path / "gone"
        test_dir.mkdir()
        event = DirCreatedEvent(str(test_dir))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as D,
        ):
            D.get.return_value = None
            handler.on_created(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_created_symlink_get_returns_none(self, handler, ws_server, tmp_path):
        """Test on_created when Symlink.get returns None for a symlink."""
        target = tmp_path / "tgt"
        target.write_text("x")
        link = tmp_path / "lnk"
        link.symlink_to(target)
        event = FileCreatedEvent(str(link))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Symlink") as S,
        ):
            S.get.return_value = None
            handler.on_created(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_created_regular_file_ignored(self, handler, ws_server, tmp_path):
        """Test on_created for regular file (non-symlink) does nothing."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        event = FileCreatedEvent(str(f))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r"}
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_created(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_modified_root_directory_skipped(self, handler, ws_server, tmp_path):
        """Test on_modified skips root LOCAL_BUCKET directory."""
        event = DirModifiedEvent(str(tmp_path))
        event.is_directory = True
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_modified(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_modified_dir_get_returns_none(self, handler, ws_server, tmp_path):
        """Test on_modified when Dir.get returns None."""
        sub = tmp_path / "sub"
        sub.mkdir()
        event = DirModifiedEvent(str(sub))
        event.is_directory = True
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as D,
        ):
            D.get.return_value = None
            handler.on_modified(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_modified_file_non_linux_debounce(self, handler, ws_server, tmp_path):
        """Test on_modified file debounce on non-Linux."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        event = FileModifiedEvent(str(f))

        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.p2p_websocket.CONFIG", config),
            patch("hsync_client.file_event_handler.SUPPORTS_ON_CLOSED", False),
            patch("hsync_client.file_event_handler.File") as F,
        ):
            F.get.return_value = MagicMock()
            handler.on_modified(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_modified_file_non_linux_unchanged_stat_skips(self, handler, ws_server, tmp_path):
        """on_modified file skipped when stat (mtime, size) is unchanged.

        Replaces the old time-based debounce test: the two-stage stat+hash
        dedup suppresses the broadcast when the file's (mtime, size) matches
        the last propagated state.
        """
        f = tmp_path / "file.txt"
        f.write_text("x")

        event = FileModifiedEvent(str(f))

        # Pre-set the stat to the current file's (mtime, size) so stage-1
        # catches it as unchanged.
        st = f.stat()
        handler._last_broadcast_stat[str(f)] = (st.st_mtime_ns, st.st_size)

        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r"}
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.SUPPORTS_ON_CLOSED", False),
        ):
            handler.on_modified(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_closed_directory_skipped(self, handler, ws_server, tmp_path):
        """Test on_closed skips directory events."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = str(tmp_path / "dir")
        handler.on_closed(event)
        ws_server.broadcast_sync.assert_not_called()

    def test_on_closed_os_cache_file_skipped(self, handler, ws_server, tmp_path):
        """Test on_closed skips OS cache files."""
        event = FileModifiedEvent(str(tmp_path / "Thumbs.db"))
        handler.on_closed(event)
        ws_server.broadcast_sync.assert_not_called()

    def test_handle_file_modified_not_exists(self, handler, ws_server, tmp_path):
        """Test _handle_file_modified returns early when file not exists."""
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler._process_file_ready(str(tmp_path / "gone.txt"))
            ws_server.broadcast_sync.assert_not_called()

    def test_handle_file_modified_file_get_none(self, handler, ws_server, tmp_path):
        """Test _handle_file_modified when File.get returns None."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.File") as F,
        ):
            F.get.return_value = None
            handler._process_file_ready(str(f))
            ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_no_dest_path(self, handler, ws_server, tmp_path):
        """on_moved with an empty destination is a move-out: routed to on_deleted.

        A file moved out of the watched tree arrives (full-event emitter) as a
        move event whose destination is empty; it must be treated as a delete,
        not dropped (regression for the lost move-out).
        """
        event = MagicMock()
        event.src_path = str(tmp_path / "a.txt")
        event.dest_path = None
        event.is_directory = False

        with patch.object(handler, "on_deleted") as mock_deleted:
            handler.on_moved(event)
            mock_deleted.assert_called_once()

    def test_on_moved_os_cache_to_os_cache_dir(self, handler, ws_server, tmp_path):
        """Test on_moved returns for OS-cache-to-OS-cache directory."""
        event = DirMovedEvent(str(tmp_path / ".DS_Store"), str(tmp_path / "Thumbs.db"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
            "DELETION_POLICY": "propagate",
        }
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_os_cache_to_visible_dir(self, handler, ws_server, tmp_path):
        """Test on_moved OS-cache->visible dir triggers on_created."""
        visible = tmp_path / "visible"
        visible.mkdir()
        event = DirMovedEvent(str(tmp_path / ".DS_Store"), str(visible))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as D,
        ):
            D.get.return_value = MagicMock()
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_moved_visible_to_os_cache_dir(self, handler, ws_server, tmp_path):
        """Test on_moved visible->OS-cache dir triggers on_deleted."""
        event = DirMovedEvent(str(tmp_path / "visible"), str(tmp_path / ".DS_Store"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
            "DELETION_POLICY": "propagate",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.threading.Timer"),
        ):
            # visible->hidden synthesizes on_deleted (now coalesced): flush first.
            handler.on_moved(event)
            handler._flush_pending_deletes()
            ws_server.broadcast_sync.assert_called_once()

    def test_on_moved_dir_rename_dir_get_none(self, handler, ws_server, tmp_path):
        """Test on_moved dir rename when Dir.get returns None."""
        event = DirMovedEvent(str(tmp_path / "old"), str(tmp_path / "new"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as D,
        ):
            D.get.return_value = None
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_dir_rename_success(self, handler, ws_server, tmp_path):
        """Test on_moved dir rename broadcasts event."""
        event = DirMovedEvent(str(tmp_path / "old"), str(tmp_path / "new"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.Dir") as D,
        ):
            mock_dir = MagicMock()
            D.get.return_value = mock_dir
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_moved_file_os_cache_src(self, handler, ws_server, tmp_path):
        """Test on_moved returns when file source is OS cache."""
        event = FileMovedEvent(str(tmp_path / "Thumbs.db"), str(tmp_path / "visible"))
        config = {"CID": "c", "LOCAL_BUCKET": str(tmp_path), "REMOTE_BUCKET": "r"}
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_moved_file_rename_success(self, handler, ws_server, tmp_path):
        """Test on_moved file rename broadcasts event."""
        event = FileMovedEvent(str(tmp_path / "old.txt"), str(tmp_path / "new.txt"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.File") as F,
        ):
            mock_file = MagicMock()
            F.get.return_value = mock_file
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_called_once()

    def test_on_moved_file_rename_file_get_none(self, handler, ws_server, tmp_path):
        """Test on_moved file rename when File.get returns None."""
        event = FileMovedEvent(str(tmp_path / "old.txt"), str(tmp_path / "new.txt"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.File") as F,
        ):
            F.get.return_value = None
            handler.on_moved(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_deleted_os_cache_directory_skipped(self, handler, ws_server, tmp_path):
        """Test on_deleted skips OS cache directories."""
        event = DirDeletedEvent(str(tmp_path / ".DS_Store"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_deleted(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_deleted_os_cache_file_skipped(self, handler, ws_server, tmp_path):
        """Test on_deleted skips OS cache files."""
        event = FileDeletedEvent(str(tmp_path / "Thumbs.db"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
        }
        with patch("hsync_client.file_event_handler.CONFIG", config):
            handler.on_deleted(event)
            ws_server.broadcast_sync.assert_not_called()

    def test_on_deleted_visible_directory(self, handler, ws_server, tmp_path):
        """Test on_deleted broadcasts visible directory deletion."""
        event = DirDeletedEvent(str(tmp_path / "mydir"))
        config = {
            "CID": "c",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "r",
            "SYNC_DIRECTION": "bidirectional",
            "DELETION_POLICY": "propagate",
        }
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.threading.Timer"),
        ):
            handler.on_deleted(event)
            handler._flush_pending_deletes()
            ws_server.broadcast_sync.assert_called_once()
