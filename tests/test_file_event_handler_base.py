# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for RemoteFileEventHandler."""

from asyncio import new_event_loop
from unittest.mock import MagicMock, patch

from hsync_client.remote_websocket import RemoteFileEventHandler


class _PollingWatcherStub:
    """Minimal WatcherManager stand-in reporting the polling backend."""

    backend = "polling"


class TestFileEventHandler:
    """Tests for RemoteFileEventHandler class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.websocket = MagicMock()
        self.loop = new_event_loop()
        self.event_queue = MagicMock()
        self.handler = RemoteFileEventHandler(self.websocket, self.loop, self.event_queue)

    def teardown_method(self):
        """Clean up event loop."""
        self.loop.close()

    def test_init_stores_websocket_and_loop(self):
        """Handler stores websocket and loop references."""
        assert self.handler.websocket == self.websocket
        assert self.handler.loop == self.loop
        assert self.handler.event_queue == self.event_queue

    def test_send_message_uses_event_queue(self):
        """Handler uses event queue for message delivery."""
        message = MagicMock()
        message.to_dict.return_value = {"test": "data"}

        with patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run:
            self.handler._broadcast_event(message)
            mock_run.assert_called_once()

    def test_send_message_direct_without_queue(self):
        """Handler sends directly if no event queue."""
        handler = RemoteFileEventHandler(self.websocket, self.loop, None)
        message = MagicMock()
        message.to_dict.return_value = {"test": "data"}

        with patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run:
            handler._broadcast_event(message)
            mock_run.assert_called_once()

    def test_send_message_skips_download_only(self):
        """_broadcast_event does nothing in download-only mode."""
        message = MagicMock()
        config = {"SYNC_DIRECTION": "download-only"}

        with (
            patch("hsync_client.remote_websocket.CONFIG", config),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler._broadcast_event(message)
            mock_run.assert_not_called()

    # -- on_any_event --

    def test_on_any_event_logs_hidden_dir(self):
        """on_any_event logs debug for hidden directories."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden"
        event.dest_path = None

        with (
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=True),
            patch("hsync_client.file_event_handler.log") as mock_log,
        ):
            self.handler.on_any_event(event)
            assert mock_log.debug.called

    def test_on_any_event_logs_hidden_file(self):
        """on_any_event logs debug for hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden_file"
        event.dest_path = None

        with (
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=True),
            patch("hsync_client.file_event_handler.log") as mock_log,
        ):
            self.handler.on_any_event(event)
            assert mock_log.debug.called

    # -- on_created --

    def test_on_created_skips_hidden_directory(self):
        """on_created skips hidden directories."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden"

        with patch("hsync_client.file_event_handler.is_hidden_dir", return_value=True):
            self.handler.on_created(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_created_broadcasts_visible_directory(self, tmp_path):
        """on_created broadcasts event for visible directories."""
        test_dir = tmp_path / "visible"
        test_dir.mkdir()

        event = MagicMock()
        event.is_directory = True
        event.src_path = str(test_dir)

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get") as mock_get,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            mock_dir = MagicMock()
            mock_get.return_value = mock_dir

            self.handler.on_created(event)

            mock_get.assert_called_once_with(str(test_dir))

    def test_on_deleted_skips_hidden_directory(self):
        """on_deleted skips hidden directories."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden"

        with patch("hsync_client.file_event_handler.is_hidden_dir", return_value=True):
            self.handler.on_deleted(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_deleted_skips_hidden_file(self):
        """on_deleted skips hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden_file"

        with patch("hsync_client.file_event_handler.is_hidden_file", return_value=True):
            self.handler.on_deleted(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_deleted_broadcasts_visible_file(self):
        """on_deleted broadcasts event for visible files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/visible.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_deleted(event)
            # Deletes are coalesced: the broadcast happens on flush.
            self.handler._flush_pending_deletes()

        # The visible-file deletion must actually reach the WS queue (the
        # void version of this test passed even with an empty on_deleted).
        mock_run.assert_called_once()
        queued_event = mock_run.call_args.args[0]
        assert queued_event is not None

    # -- polling backend propagation --

    def test_on_created_file_polling_backend_triggers_ready(self):
        """On the polling fallback a created file is propagated immediately.

        Regression: the polling observer has no close-write event, and
        ``SUPPORTS_ON_CLOSED`` (platform-based) left file content invisible
        until the next periodic resync.
        """
        self.handler._watcher = _PollingWatcherStub()
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/new.txt"

        with (
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch.object(self.handler, "_process_file_ready") as mock_ready,
        ):
            self.handler.on_created(event)

        mock_ready.assert_called_once_with("/bucket/new.txt")

    def test_on_created_file_inotify_backend_ignored(self):
        """On inotify a created file is NOT propagated from on_created.

        A plain IN_CREATE fires while the file is still being written; content
        propagates via on_closed instead, so processing it here would
        broadcast partial content.
        """
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/new.txt"

        with (
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch.object(self.handler, "_process_file_ready") as mock_ready,
        ):
            self.handler.on_created(event)

        mock_ready.assert_not_called()

    def test_on_modified_file_polling_backend_triggers_ready(self):
        """On the polling fallback a modified file is propagated from on_modified."""
        self.handler._watcher = _PollingWatcherStub()
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/changed.txt"

        config = {"SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch.object(self.handler, "_process_file_ready") as mock_ready,
        ):
            self.handler.on_modified(event)

        mock_ready.assert_called_once_with("/bucket/changed.txt")

    def test_on_modified_file_inotify_backend_permission_only(self):
        """On inotify on_modified does not re-propagate content (close-write owns it)."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/changed.txt"

        with (
            patch("hsync_client.file_event_handler.SUPPORTS_ON_CLOSED", True),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch.object(self.handler, "_process_permission_only") as mock_perm,
            patch.object(self.handler, "_process_file_ready") as mock_ready,
        ):
            self.handler.on_modified(event)

        mock_perm.assert_called_once()
        mock_ready.assert_not_called()

    def test_on_moved_hidden_to_hidden_ignored(self):
        """on_moved ignores moves from hidden to hidden directory."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden1"
        event.dest_path = "/bucket/.hidden2"

        with patch("hsync_client.file_event_handler.is_hidden_dir", return_value=True):
            self.handler.on_moved(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_moved_hidden_file_ignored(self):
        """on_moved ignores hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden_file"
        event.dest_path = "/bucket/visible_file"

        with patch("hsync_client.file_event_handler.is_hidden_file", return_value=True):
            self.handler.on_moved(event)

        # Should not send any message
        assert not self.event_queue.enqueue.called

    def test_on_moved_file_into_tree_from_outside_triggers_ready(self, tmp_path):
        """A file moved in from outside the tree (empty source) propagates.

        Regression for the lost move-in: the standard emitter collapsed the
        unmatched IN_MOVED_TO into FileCreatedEvent, which on_created drops
        (on inotify content propagates via on_closed, but a move-in has no
        close-write).
        """
        dest = tmp_path / "moved_in.txt"
        dest.write_bytes(b"content")

        event = MagicMock()
        event.is_directory = False
        event.src_path = ""
        event.dest_path = str(dest)

        with patch.object(self.handler, "_process_file_ready") as mock_ready:
            self.handler.on_moved(event)
            mock_ready.assert_called_once_with(str(dest), trigger="move-in")

    def test_on_moved_file_out_of_tree_triggers_deleted(self):
        """A file moved out of the tree (empty destination) is a delete."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/gone.txt"
        event.dest_path = ""

        with patch.object(self.handler, "on_deleted") as mock_deleted:
            self.handler.on_moved(event)

        mock_deleted.assert_called_once()
        deleted_event = mock_deleted.call_args.args[0]
        assert deleted_event.src_path == "/bucket/gone.txt"
        assert not deleted_event.is_directory

    def test_on_moved_dir_into_tree_from_outside_triggers_created(self):
        """A directory moved in from outside the tree is handled as created."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = ""
        event.dest_path = "/bucket/newdir"

        with patch.object(self.handler, "on_created") as mock_created:
            self.handler.on_moved(event)

        mock_created.assert_called_once()
        created_event = mock_created.call_args.args[0]
        assert created_event.src_path == "/bucket/newdir"
        assert created_event.is_directory

    def test_on_moved_dir_out_of_tree_triggers_deleted(self):
        """A directory moved out of the tree is handled as deleted."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/removed"
        event.dest_path = ""

        with patch.object(self.handler, "on_deleted") as mock_deleted:
            self.handler.on_moved(event)

        mock_deleted.assert_called_once()
        deleted_event = mock_deleted.call_args.args[0]
        assert deleted_event.src_path == "/bucket/removed"
        assert deleted_event.is_directory

    def test_on_moved_hidden_to_visible_triggers_created(self):
        """on_moved from hidden to visible directory triggers on_created."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/.hidden"
        event.dest_path = "/bucket/visible"

        def side_effect(path):
            return path == "/bucket/.hidden"

        with (
            patch("hsync_client.file_event_handler.is_hidden_dir", side_effect=side_effect),
            patch.object(self.handler, "on_created") as mock_created,
        ):
            self.handler.on_moved(event)
            mock_created.assert_called_once()

    def test_on_moved_visible_to_hidden_triggers_deleted(self):
        """on_moved from visible to hidden directory triggers on_deleted."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/visible"
        event.dest_path = "/bucket/.hidden"

        def side_effect(path):
            return path == "/bucket/.hidden"

        with (
            patch("hsync_client.file_event_handler.is_hidden_dir", side_effect=side_effect),
            patch.object(self.handler, "on_deleted") as mock_deleted,
        ):
            self.handler.on_moved(event)
            mock_deleted.assert_called_once()

    def test_on_moved_dir_rename_sends_message(self):
        """on_moved sends rename event for visible directory rename."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/old_dir"
        event.dest_path = "/bucket/new_dir"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_dir = MagicMock()

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get", return_value=mock_dir),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_moved(event)
            mock_run.assert_called_once()

    def test_on_moved_dir_rename_no_dst_dir_get(self):
        """on_moved does not send message when Dir.get returns None."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/old_dir"
        event.dest_path = "/bucket/new_dir"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get", return_value=None),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_moved(event)
            mock_run.assert_not_called()

    def test_on_moved_file_rename_sends_message(self):
        """on_moved sends rename event for visible file rename."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/old.txt"
        event.dest_path = "/bucket/new.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_file = MagicMock()

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.File.get", return_value=mock_file),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_moved(event)
            mock_run.assert_called_once()

    def test_on_moved_file_rename_no_dst_file_get(self):
        """on_moved does not send message when File.get returns None."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/old.txt"
        event.dest_path = "/bucket/new.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.File.get", return_value=None),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_moved(event)
            mock_run.assert_not_called()

    # -- on_moved safe-save (editor temp → final) --

    def test_on_moved_safe_save_reframes_as_modified(self):
        """on_moved from a hidden temp to a visible dst reframes as modified.

        An editor safe-save renames ``.goutputstream-XXXX`` over the final
        file. The temp is hidden (ignored) so the rename must NOT broadcast a
        ``renamed`` event; instead it propagates the destination as a content
        modification via ``_on_file_ready(trigger="safe-save")``.
        """
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.goutputstream-XXXX"
        event.dest_path = "/bucket/hello.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_file = MagicMock()
        mock_file.sha256sum = "abc123"

        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 100
        mock_stat.st_size = 42

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file") as mock_hidden,
            patch("hsync_client.file_event_handler.File.get", return_value=mock_file),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_on_file_ready") as mock_ready,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            # src is hidden (temp), dst is NOT hidden (final file)
            mock_hidden.side_effect = lambda p: p.endswith(".goutputstream-XXXX")
            mock_path_cls.return_value.exists.return_value = True
            mock_path_cls.return_value.is_symlink.return_value = False
            mock_path_cls.return_value.stat.return_value = mock_stat

            self.handler.on_moved(event)

            # Must NOT broadcast a renamed event
            mock_run.assert_not_called()
            # Must call _on_file_ready on the destination with safe-save trigger
            mock_ready.assert_called_once_with(
                mock_file,
                "/bucket/hello.txt",
                False,
                trigger="safe-save",
            )

    def test_on_moved_safe_save_both_hidden_drops_event(self):
        """on_moved from hidden to hidden is dropped entirely (temp→temp)."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.goutputstream-XXXX"
        event.dest_path = "/bucket/.goutputstream-YYYY"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=True),
            patch.object(self.handler, "_on_file_ready") as mock_ready,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_moved(event)
            mock_run.assert_not_called()
            mock_ready.assert_not_called()

    def test_on_moved_safe_save_stat_dedup_suppresses_subsequent_close(self):
        """Stat dedup in _process_file_ready suppresses duplicate after safe-save.

        After a safe-save rename triggers _process_file_ready (trigger=
        "safe-save"), the (mtime, size) and hash are recorded. A subsequent
        on_closed on the same path with the same stat is suppressed at
        stage-1 (no second _on_file_ready call, no re-hash).
        """
        event_moved = MagicMock()
        event_moved.is_directory = False
        event_moved.src_path = "/bucket/.goutputstream-XXXX"
        event_moved.dest_path = "/bucket/hello.txt"

        event_closed = MagicMock()
        event_closed.is_directory = False
        event_closed.src_path = "/bucket/hello.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_file = MagicMock()
        mock_file.sha256sum = "abc123"

        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 100
        mock_stat.st_size = 42

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file") as mock_hidden,
            patch("hsync_client.file_event_handler.File.get", return_value=mock_file),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_on_file_ready") as mock_ready,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            mock_hidden.side_effect = lambda p: p.endswith(".goutputstream-XXXX")
            mock_path_cls.return_value.exists.return_value = True
            mock_path_cls.return_value.is_symlink.return_value = False
            mock_path_cls.return_value.stat.return_value = mock_stat

            self.handler.on_moved(event_moved)
            self.handler.on_closed(event_closed)

            # Only the safe-save call, not the close (stat dedup)
            assert mock_ready.call_count == 1
            mock_run.assert_not_called()

    def test_no_op_close_unchanged_hash_skips_broadcast(self):
        """A close where content (hash) is unchanged is skipped at stage 2.

        Simulates an editor that opens a file and closes it without saving:
        the stat (mtime) may change (editor touches mtime on open) but the
        hash is identical to the last broadcast → stage-2 skip.
        """
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/hello.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
            "SYNC_DIRECTION": "bidirectional",
        }

        mock_file = MagicMock()
        mock_file.sha256sum = "abc123"

        # Stat differs from last broadcast (mtime advanced) but hash is same
        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 200
        mock_stat.st_size = 42

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.File.get", return_value=mock_file),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_on_file_ready") as mock_ready,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            mock_path_cls.return_value.exists.return_value = True
            mock_path_cls.return_value.is_symlink.return_value = False
            mock_path_cls.return_value.stat.return_value = mock_stat

            # Pre-set: last broadcast had different mtime but same hash
            self.handler._last_broadcast_stat["/bucket/hello.txt"] = (100, 42)
            self.handler._last_broadcast_hash["/bucket/hello.txt"] = "abc123"

            self.handler.on_closed(event)

            # Hash matches → skip (no broadcast, no _on_file_ready)
            mock_ready.assert_not_called()
            mock_run.assert_not_called()

    def test_same_second_different_ns_not_skipped(self):
        """Two writes within the same second (same size) are NOT skipped.

        st_mtime_ns gives nanosecond precision: two writes at 10:00:00.500
        and 10:00:00.800 produce different st_mtime_ns values, so stage-1
        does not falsely skip the second write even though int(st_mtime)
        (truncated to seconds) would be identical.
        """
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/hello.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
            "SYNC_DIRECTION": "bidirectional",
        }

        mock_file = MagicMock()
        mock_file.sha256sum = "def456"

        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 1000000800000  # 10:00:00.800 in ns
        mock_stat.st_size = 42

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.File.get", return_value=mock_file),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_on_file_ready") as mock_ready,
        ):
            mock_path_cls.return_value.exists.return_value = True
            mock_path_cls.return_value.is_symlink.return_value = False
            mock_path_cls.return_value.stat.return_value = mock_stat

            # Pre-set: last broadcast at 10:00:00.500 (same second, same size,
            # different ns). With int(st_mtime) both would be 1000000000 and
            # stage-1 would wrongly skip. With st_mtime_ns they differ.
            self.handler._last_broadcast_stat["/bucket/hello.txt"] = (1000000500000, 42)
            self.handler._last_broadcast_hash["/bucket/hello.txt"] = "abc123"

            self.handler.on_closed(event)

            # stat differs (ns) → hash differs → must broadcast
            mock_ready.assert_called_once()

    def test_delete_invalidates_dedup_so_recreated_file_broadcasts(self):
        """Regression: create -> delete -> re-create must broadcast the third event.

        Before the fix, ``on_deleted`` did not invalidate the per-path dedup
        cache (``_last_broadcast_stat`` / ``_last_broadcast_hash``). A re-created
        file sharing ``mtime_ns`` + ``size`` (and even ``sha256``) with its
        pre-deletion version was silently skipped at stage 1 as
        "Skip unchanged (stat)", so peers never received the re-creation.

        This test seeds the dedup cache as if a first broadcast had happened,
        fires ``on_deleted`` for the same path, then re-runs
        ``_process_file_ready`` with the *identical* stat+hash. The re-creation
        must reach ``_on_file_ready`` (i.e. be broadcast), not be skipped.
        """
        path = "/bucket/hello.txt"
        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_file = MagicMock()
        mock_file.sha256sum = "abc123"

        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 100
        mock_stat.st_size = 42

        delete_event = MagicMock()
        delete_event.is_directory = False
        delete_event.src_path = path

        closed_event = MagicMock()
        closed_event.is_directory = False
        closed_event.src_path = path

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.File.get", return_value=mock_file),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_on_file_ready") as mock_ready,
            patch.object(self.handler, "_broadcast_event"),
            patch.object(self.handler, "_on_deleted"),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            mock_path_cls.return_value.exists.return_value = True
            mock_path_cls.return_value.is_symlink.return_value = False
            mock_path_cls.return_value.stat.return_value = mock_stat

            # Simulate the first broadcast having recorded this stat+hash.
            self.handler._last_broadcast_stat[path] = (100, 42)
            self.handler._last_broadcast_hash[path] = "abc123"

            # 1) Delete the path → flush → cache must be invalidated.
            self.handler.on_deleted(delete_event)
            self.handler._flush_pending_deletes()
            assert path not in self.handler._last_broadcast_stat
            assert path not in self.handler._last_broadcast_hash

            # 2) Re-create with identical stat+hash → must broadcast, not skip.
            self.handler.on_closed(closed_event)
            mock_ready.assert_called_once()

    def test_rename_invalidates_dedup_so_recreated_at_source_broadcasts(self):
        """Regression: rename A→B then re-create at A must broadcast the re-creation.

        Mirrors ``test_delete_invalidates_dedup_so_recreated_file_broadcasts`` for
        the visible→visible file rename path: ``on_moved`` must pop the source
        path from the dedup cache, otherwise a file later re-created at the same
        source path with identical stat+hash is skipped at stage 1 as
        "Skip unchanged (stat)" and never broadcast.
        """
        src = "/bucket/old.txt"
        dst = "/bucket/new.txt"
        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_file = MagicMock()
        mock_file.sha256sum = "abc123"

        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 100
        mock_stat.st_size = 42

        moved_event = MagicMock()
        moved_event.is_directory = False
        moved_event.src_path = src
        moved_event.dest_path = dst

        closed_event = MagicMock()
        closed_event.is_directory = False
        closed_event.src_path = src

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.File.get", return_value=mock_file),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_on_file_ready") as mock_ready,
            patch.object(self.handler, "_broadcast_event"),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            mock_path_cls.return_value.exists.return_value = True
            mock_path_cls.return_value.is_symlink.return_value = False
            mock_path_cls.return_value.stat.return_value = mock_stat

            # Simulate a prior broadcast at the source path.
            self.handler._last_broadcast_stat[src] = (100, 42)
            self.handler._last_broadcast_hash[src] = "abc123"

            # Rename src -> dst must invalidate the source dedup cache.
            self.handler.on_moved(moved_event)
            assert src not in self.handler._last_broadcast_stat
            assert src not in self.handler._last_broadcast_hash

            # Re-create at src with identical stat+hash → must broadcast.
            self.handler.on_closed(closed_event)
            mock_ready.assert_called_once()

    def test_dedup_evicts_oldest_at_capacity(self):
        """_record_broadcast evicts the oldest entry when at capacity."""
        from hsync_client.file_event_handler import DEDUP_MAX_ENTRIES

        # Fill to capacity
        for i in range(DEDUP_MAX_ENTRIES):
            path = f"/bucket/file_{i}.txt"
            self.handler._last_broadcast_stat[path] = (i, 100)
            self.handler._last_broadcast_hash[path] = f"hash_{i}"

        # Add one more → oldest (file_0) should be evicted
        self.handler._record_broadcast("/bucket/new.txt", (999, 100), "new_hash")

        assert "/bucket/file_0.txt" not in self.handler._last_broadcast_stat
        assert "/bucket/file_0.txt" not in self.handler._last_broadcast_hash
        assert "/bucket/new.txt" in self.handler._last_broadcast_stat
        assert len(self.handler._last_broadcast_stat) == DEDUP_MAX_ENTRIES

    # -- on_deleted ignore-delete --

    def test_on_deleted_ignore_delete_skips_file(self):
        """on_deleted skips file deletion when IGNORE_DELETE is True."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        config = {"IGNORE_DELETE": True}

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_deleted(event)
            mock_run.assert_not_called()

    def test_on_deleted_ignore_delete_skips_dir(self):
        """on_deleted skips directory deletion when IGNORE_DELETE is True."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/subdir"

        config = {"IGNORE_DELETE": True}

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_deleted(event)
            mock_run.assert_not_called()

    def test_on_deleted_broadcasts_visible_dir(self):
        """on_deleted broadcasts a recursive event for visible directory deletion."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/subdir"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
            "IGNORE_DELETE": False,
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_deleted(event)
            self.handler._flush_pending_deletes()

        mock_run.assert_called_once()

    def test_on_deleted_dir_folds_descendants_into_single_recursive_event(self):
        """A directory deletion folds every descendant into one recursive event.

        Simulates the inotify burst of a ``rm -rf`` (children first, then the
        directory): after the flush exactly ONE ``deleted`` event must be
        broadcast — for the directory, with ``recursive=True`` — and the
        descendants must not be broadcast.
        """
        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
            "IGNORE_DELETE": False,
        }

        child_file = MagicMock()
        child_file.is_directory = False
        child_file.src_path = "/bucket/subdir/a.txt"
        child_dir = MagicMock()
        child_dir.is_directory = True
        child_dir.src_path = "/bucket/subdir/deep"
        root_dir = MagicMock()
        root_dir.is_directory = True
        root_dir.src_path = "/bucket/subdir"

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch.object(self.handler, "_on_deleted"),
        ):
            self.handler.on_deleted(child_file)
            self.handler.on_deleted(child_dir)
            self.handler.on_deleted(root_dir)
            self.handler._flush_pending_deletes()

        # Exactly one broadcast: the root directory with the recursive flag.
        # The Event constructor strips LOCAL_BUCKET, so the wire path is the
        # bucket-relative "subdir".
        mock_broadcast.assert_called_once()
        event = mock_broadcast.call_args.args[0]
        assert event.name == "deleted"
        assert event.src.path == "subdir"
        assert event.src.recursive is True

    def test_on_deleted_two_sibling_files_broadcast_individually(self):
        """Two unrelated file deletions still broadcast two separate events."""
        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
            "IGNORE_DELETE": False,
        }

        file_a = MagicMock()
        file_a.is_directory = False
        file_a.src_path = "/bucket/one.txt"
        file_b = MagicMock()
        file_b.is_directory = False
        file_b.src_path = "/bucket/two.txt"

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch.object(self.handler, "_on_deleted"),
        ):
            self.handler.on_deleted(file_a)
            self.handler.on_deleted(file_b)
            self.handler._flush_pending_deletes()

        assert mock_broadcast.call_count == 2

    def test_stale_delete_dropped_when_path_recreated_inside_window(self, tmp_path):
        """A delete buffered then re-created before the flush is not broadcast.

        Regression: the coalescing window can outlive the path it refers to.
        ``rm`` + immediate rewrite of the same name (ordinary atomic-writer
        pattern) re-creates the file while its delete is still buffered; the
        flush then broadcast a delete for a live file, wiping the fresh bytes
        on every peer and — through the LAN delete echo — on the source
        itself. The stale entry must be dropped (no broadcast, no tombstone).
        """
        live = tmp_path / "recreated.txt"
        live.write_bytes(b"old")
        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "mybucket",
            "IGNORE_DELETE": False,
        }
        event = MagicMock()
        event.is_directory = False
        event.src_path = str(live)

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch.object(self.handler, "_on_deleted") as mock_on_deleted,
        ):
            # Buffered while the file is gone (the rm already happened).
            live.unlink()
            self.handler.on_deleted(event)
            # Re-created inside the window: the entry now points at a live file.
            live.write_bytes(b"new")
            self.handler._flush_pending_deletes()

        mock_broadcast.assert_not_called()
        mock_on_deleted.assert_not_called()

    def test_stale_delete_does_not_swallow_live_descendants(self, tmp_path):
        """A re-created directory must not fold away its live children's deletes.

        The folding prefixes are built from the surviving entries only, so a
        directory deleted and re-created inside the window does not count as a
        deleted ancestor: a sibling child that really is gone is still
        broadcast individually.
        """
        root = tmp_path / "subdir"
        root.mkdir()
        gone = root / "gone.txt"
        live = root / "kept.txt"
        gone.write_bytes(b"x")
        live.write_bytes(b"y")
        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "mybucket",
            "IGNORE_DELETE": False,
        }
        gone_event = MagicMock()
        gone_event.is_directory = False
        gone_event.src_path = str(gone)
        dir_event = MagicMock()
        dir_event.is_directory = True
        dir_event.src_path = str(root)

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch.object(self.handler, "_on_deleted"),
        ):
            gone.unlink()
            self.handler.on_deleted(gone_event)
            # The directory itself is deleted and re-created inside the window.
            self.handler.on_deleted(dir_event)
            self.handler._flush_pending_deletes()

        # Exactly the genuinely removed child is broadcast; the live (re-created)
        # directory is not.
        mock_broadcast.assert_called_once()
        event = mock_broadcast.call_args.args[0]
        assert event.name == "deleted"
        assert event.src.filepath == "subdir/gone.txt"

    def test_on_deleted_folded_file_still_records_lan_tombstone(self):
        """Descendant files folded into a dir delete still pass _on_deleted."""
        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
            "IGNORE_DELETE": False,
        }

        child_file = MagicMock()
        child_file.is_directory = False
        child_file.src_path = "/bucket/subdir/a.txt"
        root_dir = MagicMock()
        root_dir.is_directory = True
        root_dir.src_path = "/bucket/subdir"

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch.object(self.handler, "_broadcast_event"),
            patch.object(self.handler, "_on_deleted") as mock_on_deleted,
        ):
            self.handler.on_deleted(child_file)
            self.handler.on_deleted(root_dir)
            self.handler._flush_pending_deletes()

        # The folded FILE hits _on_deleted (LAN master tombstone chokepoint);
        # the directory itself too (once, after its broadcast).
        mock_on_deleted.assert_any_call("/bucket/subdir/a.txt", False)
        mock_on_deleted.assert_any_call("/bucket/subdir", True)

    def test_on_closed_skips_directories(self):
        """on_closed skips directory events."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/dir"

        # on_closed should not process directories
        with patch("hsync_client.remote_websocket.upload_file") as mock_upload:
            self.handler.on_closed(event)
            mock_upload.assert_not_called()

    def test_on_closed_skips_hidden_files(self):
        """on_closed skips hidden files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/.hidden"

        with (
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=True),
            patch("hsync_client.remote_websocket.upload_file") as mock_upload,
        ):
            self.handler.on_closed(event)
            mock_upload.assert_not_called()

    def test_on_closed_uploads_visible_file(self):
        """on_closed uploads visible files."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 100
        mock_stat.st_size = 42

        with (
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.is_symlink", return_value=False),
            patch("pathlib.Path.stat", return_value=mock_stat),
            patch("hsync_client.file_event_handler.File.get") as mock_get,
            patch("hsync_client.remote_websocket.upload_file") as mock_upload,
        ):
            mock_file = MagicMock()
            mock_file.sha256sum = "abc123"
            mock_get.return_value = mock_file

            self.handler.on_closed(event)

            mock_upload.assert_called_once_with(mock_file, from_closed=True)

    # -- on_created symlink --

    def test_on_created_broadcasts_symlink(self):
        """on_created broadcasts symlink event for visible symlinks."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/link.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("pathlib.Path.is_symlink", return_value=True),
            patch("hsync_client.file_event_handler.Symlink.get") as mock_get,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            mock_symlink = MagicMock()
            mock_get.return_value = mock_symlink
            self.handler.on_created(event)
            mock_get.assert_called_once_with("/bucket/link.txt")

    def test_on_created_skips_symlink_when_get_returns_none(self):
        """on_created does not send message when Symlink.get returns None."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/link.txt"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("pathlib.Path.is_symlink", return_value=True),
            patch("hsync_client.file_event_handler.Symlink.get", return_value=None),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_created(event)
            mock_run.assert_not_called()

    def test_on_created_skips_escaping_symlink(self):
        """A symlink whose target escapes the bucket is never broadcast.

        Regression: the shared watcher broadcast the ``symlink`` event for any
        visible symlink, so an escaping target such as ``secret -> /etc/passwd``
        propagated to peers that would re-create the out-of-bucket link (or, on
        the LAN slave, materialise the target's content).
        """
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/secret"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_symlink = MagicMock()
        mock_symlink.target = "/etc/passwd"

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("pathlib.Path.is_symlink", return_value=True),
            patch("hsync_client.file_event_handler.Symlink.get", return_value=mock_symlink),
            patch("hsync_client.file_event_handler.is_safe_symlink_target", return_value=False),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_created(event)
            mock_run.assert_not_called()

    def test_on_created_broadcasts_in_bucket_symlink(self):
        """A symlink whose target stays inside the bucket is broadcast."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/alias"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_symlink = MagicMock()
        mock_symlink.target = "real.txt"

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("pathlib.Path.is_symlink", return_value=True),
            patch("hsync_client.file_event_handler.Symlink.get", return_value=mock_symlink),
            patch("hsync_client.file_event_handler.is_safe_symlink_target", return_value=True),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_created(event)
            mock_run.assert_called_once()

    # -- on_modified --

    def test_on_modified_broadcasts_visible_directory(self):
        """on_modified broadcasts event for visible non-root directory."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/subdir"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 100
        mock_stat.st_size = 4096

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get") as mock_get,
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            mock_dir = MagicMock()
            mock_get.return_value = mock_dir
            mock_path_cls.return_value.stat.return_value = mock_stat
            self.handler.on_modified(event)
            mock_get.assert_called_once_with("/bucket/subdir")
            mock_run.assert_called_once()

    def test_on_modified_skips_root_directory(self):
        """on_modified skips the root bucket directory."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket"

        config = {"LOCAL_BUCKET": "/bucket"}

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=True),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_modified(event)
            mock_run.assert_not_called()

    def test_on_modified_directory_dedup_suppresses_identical_stat(self):
        """Regression: identical directory MODIFY events are deduplicated.

        inotify fires IN_ATTRIB on a directory whenever a child is created,
        written, renamed or removed, so a single file save can emit several
        directory MODIFY lines with the same (mtime_ns, size). The stat-based
        dedup (mirroring the file stage-1 filter) must suppress the second
        broadcast and the second INFO log line.
        """
        path = "/bucket/subdir"
        event = MagicMock()
        event.is_directory = True
        event.src_path = path

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 100
        mock_stat.st_size = 4096

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get", return_value=MagicMock()),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            mock_path_cls.return_value.stat.return_value = mock_stat

            # First fire: broadcast + log
            self.handler.on_modified(event)
            assert mock_broadcast.call_count == 1
            # Second fire with identical stat: suppressed (no new broadcast)
            self.handler.on_modified(event)
            assert mock_broadcast.call_count == 1

    def test_on_modified_directory_suppresses_mtime_burst_within_window(self):
        """A burst of directory MODIFY with changing mtime but same mode folds.

        inotify fires IN_ATTRIB once per child remove during an rmtree, so the
        receiving side logs+broadcasts the same directory many times, each with
        a distinct mtime that the stat dedup cannot collapse. The time-window
        dedup must fold the burst into a single broadcast.
        """
        path = "/bucket/subdir"
        event = MagicMock()
        event.is_directory = True
        event.src_path = path

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        stats = []
        for i in range(5):
            s = MagicMock()
            s.st_mtime_ns = 100 + i
            s.st_size = 4096
            s.st_mode = 0o40755
            stats.append(s)

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get", return_value=MagicMock()),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
            patch(
                "hsync_client.file_event_handler.monotonic",
                side_effect=[0.0, 0.01, 0.02, 0.03, 0.04],
            ),
        ):
            mock_path_cls.return_value.stat.side_effect = stats
            for _ in stats:
                self.handler.on_modified(event)

            assert mock_broadcast.call_count == 1

    def test_on_modified_directory_mode_change_broadcasts_within_window(self):
        """A directory chmod (mode change) still broadcasts, even within the window.

        The window dedup must never fold a meaningful signal: a permission
        change is the one directory metadata event that must propagate, so it
        bypasses the mtime-churn window.
        """
        path = "/bucket/subdir"
        event = MagicMock()
        event.is_directory = True
        event.src_path = path

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        stat1 = MagicMock()
        stat1.st_mtime_ns = 100
        stat1.st_size = 4096
        stat1.st_mode = 0o40755
        stat2 = MagicMock()
        stat2.st_mtime_ns = 200
        stat2.st_size = 4096
        stat2.st_mode = 0o40700

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get", return_value=MagicMock()),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
            patch(
                "hsync_client.file_event_handler.monotonic",
                side_effect=[0.0, 0.01, 0.02],
            ),
        ):
            mock_path_cls.return_value.stat.side_effect = [stat1, stat2]
            self.handler.on_modified(event)
            self.handler.on_modified(event)

            assert mock_broadcast.call_count == 2

    def test_on_modified_directory_chmod_logs_permission(self):
        """A directory chmod (mode-only change) logs [PERMISSION], not MODIFY.

        chmod changes only st_mode (not mtime/size); a child add/remove changes
        the directory mtime. A pure mode change is therefore a permission-only
        event and must be labelled [PERMISSION] mode change, like the file-chmod
        path, instead of [LOCAL MODIFY].
        """
        path = "/bucket/subdir"
        event = MagicMock()
        event.is_directory = True
        event.src_path = path

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        # Identical mtime+size, different mode (chmod 755 -> 700).
        stat1 = MagicMock()
        stat1.st_mtime_ns = 100
        stat1.st_size = 4096
        stat1.st_mode = 0o40755
        stat2 = MagicMock()
        stat2.st_mtime_ns = 100
        stat2.st_size = 4096
        stat2.st_mode = 0o40700

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get", return_value=MagicMock()),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch("hsync_client.file_event_handler.log") as mock_log,
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
            patch(
                "hsync_client.file_event_handler.monotonic",
                side_effect=[0.0, 0.0, 0.01, 0.01],
            ),
        ):
            mock_path_cls.return_value.stat.side_effect = [stat1, stat2]
            # First call records the baseline (logs [LOCAL MODIFY]); second is
            # the chmod and must log [PERMISSION].
            self.handler.on_modified(event)
            self.handler.on_modified(event)

            msgs = [str(c.args[0]) if c.args else str(c) for c in mock_log.info.call_args_list]
            # The chmod (second call) logs [PERMISSION] mode change (dir); the
            # baseline first call logs [LOCAL MODIFY] (dir) exactly once.
            assert any("[PERMISSION]" in m and "mode change" in m and "(dir)" in m for m in msgs), (
                msgs
            )
            assert sum(1 for m in msgs if "[LOCAL MODIFY]" in m and "(dir)" in m) == 1, msgs
            # The chmod still propagates.
            assert mock_broadcast.call_count == 2

    def test_on_modified_directory_broadcasts_after_window_expires(self):
        """An mtime-only change after the window still broadcasts.

        The window must not over-suppress: two genuinely spaced changes to the
        same directory (beyond DIR_MODIFY_WINDOW) each broadcast, even when the
        mode is unchanged.
        """
        path = "/bucket/subdir"
        event = MagicMock()
        event.is_directory = True
        event.src_path = path

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        stat1 = MagicMock()
        stat1.st_mtime_ns = 100
        stat1.st_size = 4096
        stat1.st_mode = 0o40755
        stat2 = MagicMock()
        stat2.st_mtime_ns = 200
        stat2.st_size = 4096
        stat2.st_mode = 0o40755

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get", return_value=MagicMock()),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
            patch(
                "hsync_client.file_event_handler.monotonic",
                side_effect=[0.0, 1.0, 1.5],
            ),
        ):
            mock_path_cls.return_value.stat.side_effect = [stat1, stat2]
            self.handler.on_modified(event)
            self.handler.on_modified(event)

            assert mock_broadcast.call_count == 2

    def test_on_modified_directory_records_stat_before_broadcast(self):
        """Regression: the dedup stat is recorded *before* the broadcast.

        mkdir emits a burst of IN_ATTRIB events ~1ms apart. The second one can
        be dispatched while Dir.get + broadcast of the first is still in
        flight. Recording the stat up front (before ``_broadcast_event``) lets
        the dedup pre-check catch that sibling immediately. We observe this by
        re-invoking ``on_modified`` from inside the broadcast: the nested call
        must hit the dedup and not broadcast again.
        """
        path = "/bucket/subdir"
        event = MagicMock()
        event.is_directory = True
        event.src_path = path

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 100
        mock_stat.st_size = 4096

        broadcast_calls = 0

        def broadcast_reentrant(*args, **kwargs):
            nonlocal broadcast_calls
            broadcast_calls += 1
            # Re-enter on_modified while the first broadcast is "in flight":
            # the stat must already be recorded, so this nested call is a
            # duplicate and must not trigger another broadcast.
            self.handler.on_modified(event)

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get", return_value=MagicMock()),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_broadcast_event", side_effect=broadcast_reentrant),
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            mock_path_cls.return_value.stat.return_value = mock_stat
            self.handler.on_modified(event)

        # Only the outer call broadcast; the nested (re-entrant) call was
        # deduplicated because the stat was recorded before broadcasting.
        assert broadcast_calls == 1

    def test_on_modified_non_linux_uploads_file(self):
        """on_modified uploads file on non-Linux (fallback path)."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        config = {"SYNC_DIRECTION": "bidirectional"}

        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 100
        mock_stat.st_size = 42

        with (
            patch("hsync_client.file_event_handler.SUPPORTS_ON_CLOSED", False),
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.is_symlink", return_value=False),
            patch("pathlib.Path.stat", return_value=mock_stat),
            patch("hsync_client.file_event_handler.File.get") as mock_get,
            patch("hsync_client.remote_websocket.upload_file") as mock_upload,
        ):
            mock_file = MagicMock()
            mock_file.sha256sum = "abc123"
            mock_get.return_value = mock_file
            self.handler.on_modified(event)
            mock_upload.assert_called_once_with(mock_file, from_closed=False)

    def test_on_modified_non_linux_unchanged_stat_skips(self):
        """on_modified skips upload on non-Linux when stat is unchanged.

        The two-stage stat+hash dedup replaces the old time-based debounce:
        if (mtime, size) matches the last broadcast, the upload is skipped
        without even computing the hash.
        """
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        config = {"SYNC_DIRECTION": "bidirectional"}

        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 100
        mock_stat.st_size = 42

        with (
            patch("hsync_client.file_event_handler.SUPPORTS_ON_CLOSED", False),
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch("hsync_client.remote_websocket.upload_file") as mock_upload,
        ):
            # File exists, stat matches the pre-set broadcast stat
            mock_path_cls.return_value.exists.return_value = True
            mock_path_cls.return_value.is_symlink.return_value = False
            mock_path_cls.return_value.stat.return_value = mock_stat
            self.handler._last_broadcast_stat["/bucket/file.txt"] = (100, 42)

            self.handler.on_modified(event)
            mock_upload.assert_not_called()

    def test_on_modified_non_linux_download_only_skips(self):
        """on_modified skips upload in download-only mode on non-Linux."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        config = {"SYNC_DIRECTION": "download-only"}

        with (
            patch("hsync_client.file_event_handler.SUPPORTS_ON_CLOSED", False),
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.remote_websocket.upload_file") as mock_upload,
        ):
            self.handler.on_modified(event)
            mock_upload.assert_not_called()

    # -- on_closed edge cases --

    def test_on_closed_download_only_skips(self):
        """on_closed skips upload in download-only mode."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        config = {"SYNC_DIRECTION": "download-only"}

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("hsync_client.remote_websocket.upload_file") as mock_upload,
        ):
            self.handler.on_closed(event)
            mock_upload.assert_not_called()

    def test_on_closed_file_not_found_logs_warning(self):
        """on_closed logs warning when file was deleted before upload."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/gone.txt"

        with (
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("pathlib.Path.exists", return_value=False),
            patch("hsync_client.file_event_handler.log") as mock_log,
        ):
            self.handler.on_closed(event)
            assert mock_log.warning.called

    def test_on_closed_exception_logs_error(self):
        """on_closed logs error when exception occurs."""
        event = MagicMock()
        event.is_directory = False
        event.src_path = "/bucket/file.txt"

        with (
            patch("hsync_client.file_event_handler.is_hidden_file", return_value=False),
            patch("pathlib.Path.exists", side_effect=OSError("boom")),
            patch("hsync_client.file_event_handler.log") as mock_log,
        ):
            self.handler.on_closed(event)
            assert mock_log.error.called

    # -- rm -rf directory deletion (MODIFY burst + duplicate DELETEs) --

    def test_duplicate_dir_delete_event_broadcasts_once(self):
        """Two identical delete events for the same directory broadcast once.

        A directory with its own recursive watch reports a deletion twice:
        ``IN_DELETE`` (from its parent's watch) and ``IN_DELETE_SELF`` (from
        its own watch on the same path). Both arrive within the coalescing
        window and must result in a single broadcast, not two identical
        ``[LOCAL DELETE]`` lines and two identical ``deleted`` messages.
        """
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/a"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
            "IGNORE_DELETE": False,
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch.object(self.handler, "_on_deleted"),
        ):
            self.handler.on_deleted(event)
            self.handler.on_deleted(event)
            self.handler._flush_pending_deletes()

        # Exactly one broadcast overall, and it is the recursive dir delete.
        mock_broadcast.assert_called_once()
        broadcast_event = mock_broadcast.call_args.args[0]
        assert broadcast_event.name == "deleted"
        assert broadcast_event.src.path == "a"
        assert broadcast_event.src.recursive is True

    def test_rmtree_dir_modify_burst_suppressed_by_pending_delete(self):
        """Dir MODIFY events covered by a buffered delete are suppressed.

        During ``rm -rf``, inotify fires ``IN_ATTRIB`` on a directory whenever
        a child entry is removed, so a deep tree produces one directory MODIFY
        per removed child *before* the tree's delete events are coalesced.
        A MODIFY whose path equals — or descends from — a buffered directory
        delete must be skipped entirely (no log, no broadcast), because the
        directory is being removed, not genuinely modified.
        """
        modify_event = MagicMock()
        modify_event.is_directory = True
        modify_event.src_path = "/bucket/a/b/c"

        delete_event = MagicMock()
        delete_event.is_directory = True
        delete_event.src_path = "/bucket/a"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
            "IGNORE_DELETE": False,
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch.object(self.handler, "_on_deleted"),
        ):
            # The delete of the tree root is buffered first...
            self.handler.on_deleted(delete_event)
            # ...then the directory MODIFY burst for its descendants arrives.
            self.handler.on_modified(modify_event)
            self.handler._flush_pending_deletes()

        # No MODIFY broadcast: the only broadcast is the recursive delete.
        mock_broadcast.assert_called_once()
        broadcast_event = mock_broadcast.call_args.args[0]
        assert broadcast_event.name == "deleted"
        assert broadcast_event.src.path == "a"

    def test_rmtree_delete_dir_modify_of_root_suppressed(self):
        """A MODIFY of the deleted directory itself is also suppressed."""
        path = "/bucket/a"
        modify_event = MagicMock()
        modify_event.is_directory = True
        modify_event.src_path = path

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
            "IGNORE_DELETE": False,
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch.object(self.handler, "_on_deleted"),
        ):
            self.handler.on_deleted(modify_event)
            # Second delete event for the same path is counted as the pending
            # delete (dedup), and the MODIFY for the same path is suppressed.
            self.handler.on_deleted(modify_event)
            self.handler.on_modified(modify_event)
            self.handler._flush_pending_deletes()

        mock_broadcast.assert_called_once()
        broadcast_event = mock_broadcast.call_args.args[0]
        assert broadcast_event.name == "deleted"
        assert broadcast_event.src.path == "a"

    def test_dir_modify_of_gone_directory_suppressed(self):
        """A directory MODIFY for a path that no longer exists is suppressed.

        After the last ``IN_ATTRIB`` of an rmtree the directory is already
        gone (``stat()`` raises): the event is noise, not a metadata change,
        and must not be logged or broadcast.
        """
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/gone"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            mock_path_cls.return_value.stat.side_effect = OSError("gone")
            self.handler.on_modified(event)

        mock_broadcast.assert_not_called()

    def test_dir_modify_not_suppressed_without_pending_delete(self):
        """A genuine dir MODIFY without any pending delete still broadcasts."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/freshdir"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
        }

        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 100
        mock_stat.st_size = 4096

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get", return_value=MagicMock()),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            mock_path_cls.return_value.stat.return_value = mock_stat
            self.handler.on_modified(event)

        mock_broadcast.assert_called_once()

    def test_dir_modify_suppressed_when_descendant_is_pending_delete(self):
        """A dir MODIFY is suppressed when a descendant dir is pending deletion.

        During an inbound ``rmtree`` (applied by ``_apply_delete``),
        inotify delivers ``IN_ATTRIB`` (modify) for a parent directory
        *before* ``IN_DELETE_SELF`` for the child directory that is being
        removed, so the ancestor check alone misses it.  The descendant
        check recognises that a directory whose *children* are buffered for
        deletion is being emptied, not genuinely modified.
        """
        # Child is buffered for deletion first (inotify bottom-up)
        delete_event = MagicMock()
        delete_event.is_directory = True
        delete_event.src_path = "/bucket/a/b"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
            "IGNORE_DELETE": False,
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch.object(self.handler, "_on_deleted"),
        ):
            # inotify fires IN_DELETE for /a/b *before* IN_ATTRIB for /a
            self.handler.on_deleted(delete_event)

            # Now IN_ATTRIB fires on the parent /a -- the descendant check
            # should suppress it because /a/b is pending deletion under /a.
            modify_event = MagicMock()
            modify_event.is_directory = True
            modify_event.src_path = "/bucket/a"
            self.handler.on_modified(modify_event)

            # Flush so the delete is broadcast.
            self.handler._flush_pending_deletes()

        # Only the delete is broadcast; the modify of /a is suppressed.
        mock_broadcast.assert_called_once()
        broadcast_event = mock_broadcast.call_args.args[0]
        assert broadcast_event.name == "deleted"

    def test_dir_modify_suppressed_by_deeply_nested_descendant_delete(self):
        """A dir MODIFY is suppressed when a deeply nested descendant is pending deletion.

        /a/b/c/d is pending -> IN_ATTRIB on /a is noise from the ongoing rmtree.
        """
        delete_event = MagicMock()
        delete_event.is_directory = True
        delete_event.src_path = "/bucket/a/b/c/d"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
            "IGNORE_DELETE": False,
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch.object(self.handler, "_on_deleted"),
        ):
            self.handler.on_deleted(delete_event)

            # IN_ATTRIB fires on /a (grandparent of the deleted /a/b/c/d)
            modify_event = MagicMock()
            modify_event.is_directory = True
            modify_event.src_path = "/bucket/a"
            self.handler.on_modified(modify_event)

            self.handler._flush_pending_deletes()

        # The modify is suppressed -- only the delete is broadcast.
        mock_broadcast.assert_called_once()
        assert mock_broadcast.call_args.args[0].name == "deleted"

    def test_dir_modify_not_suppressed_by_unrelated_delete(self):
        """A dir MODIFY is NOT suppressed when the pending delete is unrelated.

        /bucket/x/y is pending -- /bucket/a is NOT an ancestor of /bucket/x/y,
        and /bucket/x/y does NOT descend from /bucket/a, so the modify is valid.
        """
        delete_event = MagicMock()
        delete_event.is_directory = True
        delete_event.src_path = "/bucket/x/y"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "mybucket",
            "IGNORE_DELETE": False,
        }

        mock_stat = MagicMock()
        mock_stat.st_mtime_ns = 200
        mock_stat.st_size = 4096

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch("hsync_client.file_event_handler.Dir.get", return_value=MagicMock()),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            mock_path_cls.return_value.stat.return_value = mock_stat
            self.handler.on_deleted(delete_event)

            # A genuinely modified directory that is unrelated to the delete.
            modify_event = MagicMock()
            modify_event.is_directory = True
            modify_event.src_path = "/bucket/a"
            self.handler.on_modified(modify_event)

            self.handler._flush_pending_deletes()

        # Both events broadcast: the delete for /x/y AND the modify for /a.
        assert mock_broadcast.call_count == 2
        names = [c.args[0].name for c in mock_broadcast.call_args_list]
        assert "deleted" in names
        assert "modified" in names


class TestRemoteModeOriginLabels:
    """Remote-mode watcher labels are always LOCAL-prefixed (origin-aware _label)."""

    def setup_method(self):
        """Set up test fixtures."""
        self.websocket = MagicMock()
        self.loop = new_event_loop()
        self.event_queue = MagicMock()
        self.handler = RemoteFileEventHandler(self.websocket, self.loop, self.event_queue)

    def teardown_method(self):
        """Clean up event loop."""
        self.loop.close()

    def test_on_created_dir_logs_local_create(self, tmp_path):
        """Remote-mode watcher: a local directory create logs [LOCAL CREATE]."""
        from watchdog.events import DirCreatedEvent

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
            patch("hsync_client.file_event_handler.log") as mock_log,
            patch.object(self.handler, "_broadcast_event"),
        ):
            mock_dir.get.return_value = MagicMock()
            self.handler.on_created(event)
            msgs = [str(c.args[0]) if c.args else str(c) for c in mock_log.info.call_args_list]
            assert any("[LOCAL CREATE]" in m for m in msgs), msgs

    def test_on_deleted_logs_local_delete(self, tmp_path):
        """Remote-mode watcher: a local delete logs [LOCAL DELETE]."""
        from watchdog.events import FileDeletedEvent

        event = FileDeletedEvent(str(tmp_path / "gone.txt"))

        config = {
            "CID": "test-client",
            "LOCAL_BUCKET": str(tmp_path),
            "REMOTE_BUCKET": "remote",
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.log") as mock_log,
            patch("hsync_client.file_event_handler.threading.Timer"),
            patch.object(self.handler, "_broadcast_event"),
        ):
            self.handler.on_deleted(event)
            self.handler._flush_pending_deletes()
            msgs = [str(c.args[0]) if c.args else str(c) for c in mock_log.info.call_args_list]
            assert any("[LOCAL DELETE]" in m for m in msgs), msgs
