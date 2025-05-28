# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for RemoteFileEventHandler."""

from asyncio import new_event_loop
from unittest.mock import MagicMock, patch

from hsync_client.remote_websocket import RemoteFileEventHandler


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

    def test_on_created_broadcasts_visible_directory(self):
        """on_created broadcasts event for visible directories."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = "/bucket/visible"

        config = {
            "CID": "testcid",
            "LOCAL_BUCKET": "/bucket",
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

            mock_get.assert_called_once_with("/bucket/visible")

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
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            self.handler.on_deleted(event)
            # Message should be sent

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
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
        ):
            mock_path_cls.return_value.exists.return_value = True
            mock_path_cls.return_value.stat.return_value = mock_stat

            # Simulate the first broadcast having recorded this stat+hash.
            self.handler._last_broadcast_stat[path] = (100, 42)
            self.handler._last_broadcast_hash[path] = "abc123"

            # 1) Delete the path → cache must be invalidated.
            self.handler.on_deleted(delete_event)
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
        """on_deleted broadcasts event for visible directory deletion."""
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
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            self.handler.on_deleted(event)
            mock_run.assert_called_once()

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

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get") as mock_get,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe") as mock_run,
        ):
            mock_dir = MagicMock()
            mock_get.return_value = mock_dir
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

    def test_on_modified_directory_broadcasts_when_stat_changes(self):
        """A directory MODIFY with a different stat still broadcasts.

        The dedup must not over-suppress: when the directory mtime genuinely
        changes between two IN_ATTRIB fires (e.g. two different children
        modified at different times), the second event broadcasts normally.
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
        stat2 = MagicMock()
        stat2.st_mtime_ns = 200
        stat2.st_size = 4096

        with (
            patch("hsync_client.file_event_handler.CONFIG", config),
            patch("hsync_client.file_event_handler.is_hidden_dir", return_value=False),
            patch("hsync_client.file_event_handler.is_same_paths", return_value=False),
            patch("hsync_client.file_event_handler.Dir.get", return_value=MagicMock()),
            patch("hsync_client.file_event_handler.Path") as mock_path_cls,
            patch.object(self.handler, "_broadcast_event") as mock_broadcast,
            patch("hsync_client.remote_websocket.run_coroutine_threadsafe"),
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
            patch.object(self.handler, "_broadcast_event"),
        ):
            self.handler.on_deleted(event)
            msgs = [str(c.args[0]) if c.args else str(c) for c in mock_log.info.call_args_list]
            assert any("[LOCAL DELETE]" in m for m in msgs), msgs
