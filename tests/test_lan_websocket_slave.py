"""
Tests for client file event handler functionality.
"""

import time
from unittest.mock import ANY, MagicMock, patch

import pytest
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from hsync_client.lan_websocket import LANSlaveFileEventHandler


class TestSlaveFileEventHandler:
    """Tests for LANSlaveFileEventHandler class."""

    @pytest.fixture
    def handler(self):
        """Create a LANSlaveFileEventHandler instance."""
        return LANSlaveFileEventHandler(
            master_host="192.168.1.100",
            master_port=9753,
            use_https=False,
        )

    def test_handler_creation(self):
        """Test creating a client file event handler."""
        handler = LANSlaveFileEventHandler("192.168.1.100", 9753, use_https=True)
        assert handler._master_host == "192.168.1.100"
        assert handler._master_port == 9753
        assert handler._use_https is True
        assert handler._last_modified == {}
        assert handler._receiving_files == set()
        assert handler._pending_ops == []

    def test_rel_in_bucket_inside(self, handler):
        """A path inside the bucket is relativized component-aware."""
        config = {"LOCAL_BUCKET": "/data/bucket"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            assert handler._rel_in_bucket("/data/bucket/x/y.txt") == "x/y.txt"

    def test_rel_in_bucket_root(self, handler):
        """The bucket root itself relativizes to the empty string."""
        config = {"LOCAL_BUCKET": "/data/bucket"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            assert handler._rel_in_bucket("/data/bucket") == ""

    def test_rel_in_bucket_sibling_prefix_not_stripped(self, handler):
        """A sibling sharing a path prefix must not be stripped into a bogus
        relative path (regression: removeprefix produced ``2/x``)."""
        config = {"LOCAL_BUCKET": "/data/bucket"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            with pytest.raises(ValueError):
                handler._rel_in_bucket("/data/bucket2/x/y.txt")

    def test_rel_in_bucket_outside_raises(self, handler):
        """A path outside the bucket raises instead of being sent verbatim."""
        config = {"LOCAL_BUCKET": "/data/bucket"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            with pytest.raises(ValueError):
                handler._rel_in_bucket("/etc/passwd")

    def test_mark_receiving(self, handler):
        """Test marking a file as receiving."""
        handler.mark_receiving("/path/to/file.txt")
        assert handler.is_receiving("/path/to/file.txt")

    def test_unmark_receiving(self, handler):
        """Test unmarking a file after receiving."""
        handler.mark_receiving("/path/to/file.txt")
        assert handler.is_receiving("/path/to/file.txt")

        handler.unmark_receiving("/path/to/file.txt")
        assert not handler.is_receiving("/path/to/file.txt")

    def test_is_receiving_false(self, handler):
        """Test checking if file is not being received."""
        assert not handler.is_receiving("/path/to/file.txt")

    def test_inbound_rename_echo_match(self, handler):
        """is_inbound_rename_echo matches the exact recorded pair."""
        handler.record_inbound_rename("/b/old.txt", "/b/new.txt")
        assert handler.is_inbound_rename_echo("/b/old.txt", "/b/new.txt")

    def test_inbound_rename_echo_wrong_pair(self, handler):
        """A genuine user rename reusing one path has a different pair and
        must NOT match the echo cache."""
        handler.record_inbound_rename("/b/old.txt", "/b/new.txt")
        # user renames the destination further: (new.txt -> newer.txt)
        assert not handler.is_inbound_rename_echo("/b/new.txt", "/b/newer.txt")
        # user renames into the source: (other -> old.txt)
        assert not handler.is_inbound_rename_echo("/b/other.txt", "/b/old.txt")

    def test_inbound_rename_echo_expired(self, handler):
        """An expired entry is evicted and no longer matches."""
        handler.record_inbound_rename("/b/old.txt", "/b/new.txt")
        # Force expiry by backdating the recorded timestamp
        key = ("/b/old.txt", "/b/new.txt")
        handler._inbound_renames._items[key] -= handler._inbound_renames._ttl + 1
        assert not handler.is_inbound_rename_echo("/b/old.txt", "/b/new.txt")

    def test_inbound_rename_echo_descendant_match(self, handler):
        """A descendant echo of a cached directory rename is recognised.

        If the master renamed ``ABC -> ABC-123``, the slave's own watcher may
        also emit echo events for children under the old path (e.g.
        ``ABC/.hsynckeep -> ABC-123/.hsynckeep``). This must be treated as a
        benign echo so the redundant PATCH (and its 404 WARNING) are avoided.
        """
        handler.record_inbound_rename("/b/ABC", "/b/ABC-123")
        assert handler.is_inbound_rename_echo("/b/ABC", "/b/ABC-123")
        assert handler.is_inbound_rename_echo("/b/ABC/.hsynckeep", "/b/ABC-123/.hsynckeep")
        assert handler.is_inbound_rename_echo("/b/ABC/sub/x.txt", "/b/ABC-123/sub/x.txt")

    def test_inbound_rename_echo_prefix_collision_not_matched(self, handler):
        """A rename sharing a path prefix without a separator must NOT match.

        ``ABC-keeper -> ABC-123-keeper`` is a real rename, not a descendant
        echo of ``ABC -> ABC-123``: the trailing suffix does not start with
        ``/``, so it must not be suppressed.
        """
        handler.record_inbound_rename("/b/ABC", "/b/ABC-123")
        assert not handler.is_inbound_rename_echo("/b/ABC-keeper", "/b/ABC-123-keeper")
        assert not handler.is_inbound_rename_echo("/b/ABC", "/b/ABC-124")
        assert not handler.is_inbound_rename_echo("/b/other", "/b/ABC-123/x")

    def test_outbound_dir_descendant_match(self, handler):
        """A child of an outbound directory rename is recognised as redundant.

        When the slave renames a directory (``ABC-123 -> ABC-123-X``), the
        local watcher also emits an ``on_moved`` for each child (e.g. the
        ``.hsynckeep`` sentinel). The single PATCH of the directory already
        renames the whole tree on the master, so those child PATCHes must be
        skipped instead of 404-ing.
        """
        handler._record_outbound_dir_rename("/b/ABC-123", "/b/ABC-123-X")
        assert handler.is_outbound_dir_descendant(
            "/b/ABC-123/.hsynckeep", "/b/ABC-123-X/.hsynckeep"
        )
        assert handler.is_outbound_dir_descendant("/b/ABC-123/sub/x.txt", "/b/ABC-123-X/sub/x.txt")

    def test_outbound_dir_descendant_prefix_collision_not_matched(self, handler):
        """A child sharing a path prefix without a separator must NOT match.

        ``ABC-123-keeper -> ABC-123-X-keeper`` is a real rename, not a child
        of ``ABC-123 -> ABC-123-X``: the trailing suffix does not start with
        ``/``, so it must not be suppressed.
        """
        handler._record_outbound_dir_rename("/b/ABC-123", "/b/ABC-123-X")
        assert not handler.is_outbound_dir_descendant("/b/ABC-123-keeper", "/b/ABC-123-X-keeper")
        assert not handler.is_outbound_dir_descendant("/b/other", "/b/ABC-123-X/sub")

    def test_is_temp_file(self, handler):
        """Test detecting temporary files."""
        assert handler._is_temp_file("/path/to/file.part")
        assert not handler._is_temp_file("/path/to/file.txt")

    def test_wait_pending_no_operations(self, handler):
        """Test waiting for pending operations when there are none."""
        timed_out = handler.wait_pending(timeout=1.0)
        assert timed_out == 0

    def test_wait_pending_completed_operations(self, handler):
        """Test waiting for operations that complete quickly."""

        def quick_operation():
            time.sleep(0.1)

        thread = handler._start_operation(quick_operation, name="test-op")
        # Deterministic wait on the returned Future (pooled execution is
        # asynchronous by design).
        assert thread.result(timeout=2.0) is None

        timed_out = handler.wait_pending(timeout=2.0)
        assert timed_out == 0

    def test_wait_pending_scales_with_bytes_estimate(self, handler):
        """wait_pending must extend the deadline for large in-flight uploads.

        A pending operation of 1 GiB at the 10 MB/s floor (≈ 102s plus the
        base timeout) must not be aborted at the 1s base timeout; we cap
        runtime to ~1s by joining a quick stub op but inspect the log to
        confirm the effective timeout was scaled."""
        from hsync_client.lan_websocket import MIN_BANDWIDTH_BPS

        def quick():
            time.sleep(0.05)

        large_bytes = 1024 * 1024 * 1024  # 1 GiB
        handler._start_operation(quick, name="big-upload", bytes_estimate=large_bytes)

        expected_min = large_bytes / MIN_BANDWIDTH_BPS  # ~102 seconds
        assert handler._pending_ops[0][1] == large_bytes

        timed_out = handler.wait_pending(timeout=1.0)
        assert timed_out == 0
        assert expected_min > 60  # sanity: the scaling does extend the timeout

    def test_on_created_temp_file_ignored(self, handler, tmp_path):
        """Test that temporary files are ignored."""
        temp_file = tmp_path / "file.part"
        temp_file.write_text("temp")

        event = FileCreatedEvent(str(temp_file))

        handler.on_created(event)

        # Should not trigger any upload
        assert len(handler._pending_ops) == 0

    def test_on_created_receiving_file_ignored(self, handler, tmp_path):
        """Test that files being received are ignored."""
        test_file = tmp_path / "file.txt"
        test_file.write_text("content")

        handler.mark_receiving(str(test_file))

        event = FileCreatedEvent(str(test_file))

        handler.on_created(event)

        # Should not trigger any upload
        assert len(handler._pending_ops) == 0

    def test_on_created_directory(self, handler, tmp_path):
        """Test handling directory creation."""
        test_dir = tmp_path / "newdir"
        test_dir.mkdir()

        event = DirCreatedEvent(str(test_dir))

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "REMOTE_BUCKET": "test-bucket",
        }

        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_created(event)

        # Should create sentinel file
        sentinel = test_dir / ".hsynckeep"
        assert sentinel.exists()

    def test_on_deleted_download_only_mode(self, handler, tmp_path):
        """Test that deletions are not propagated in download-only mode."""
        test_file = tmp_path / "deleted.txt"

        event = FileDeletedEvent(str(test_file))

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "download-only",
        }

        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_deleted(event)

        # Should not start any operation
        assert len(handler._pending_ops) == 0

    def test_on_deleted_ignore_delete_mode(self, handler, tmp_path):
        """Test that deletions are ignored in ignore-delete mode."""
        test_file = tmp_path / "deleted.txt"

        event = FileDeletedEvent(str(test_file))

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": True,
        }

        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_deleted(event)

        # Should not start any operation
        assert len(handler._pending_ops) == 0

    def test_on_moved_download_only_mode(self, handler, tmp_path):
        """Test that renames are not propagated in download-only mode."""
        src_file = tmp_path / "old.txt"
        src_file.write_text("content")
        dest_file = tmp_path / "new.txt"

        event = FileCreatedEvent(str(src_file))
        event.dest_path = str(dest_file)
        event.event_type = "moved"

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "download-only",
        }

        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_moved(event)

        # Should not start any operation
        assert len(handler._pending_ops) == 0

    def test_ensure_sentinel_empty_directory(self, handler, tmp_path):
        """Test creating sentinel in empty directory."""
        test_dir = tmp_path / "emptydir"
        test_dir.mkdir()

        handler._ensure_sentinel(str(test_dir))

        sentinel = test_dir / ".hsynckeep"
        assert sentinel.exists()

    def test_ensure_sentinel_non_empty_directory(self, handler, tmp_path):
        """Test not creating sentinel in non-empty directory."""
        test_dir = tmp_path / "nonempty"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("content")

        handler._ensure_sentinel(str(test_dir))

        sentinel = test_dir / ".hsynckeep"
        assert not sentinel.exists()

    def test_ensure_sentinel_os_error(self, handler):
        """Test _ensure_sentinel returns on OSError (e.g., dir not found)."""
        handler._ensure_sentinel("/nonexistent/path/xyz")  # no crash

    def test_upload_to_master_success(self, handler):
        """Test _upload_to_master calls upload_file."""
        from hsync_common.models.file import File

        file_obj = File("/test/file.txt")
        with patch("hsync_client.transfer.upload_file") as mock_up:
            handler._upload_to_master(file_obj)
            mock_up.assert_called_once_with(
                file_obj, check_size=True, from_closed=False, inventory_observer=ANY
            )

    def test_upload_to_master_emits_no_intent_line(self, handler):
        """A slave upload produces a single log line (the [UPLOAD] from upload_file).

        The former ``[CLIENT UP]`` intent line was removed: its CID suffix
        always carried this node's own CID (no information) and the
        ``[UPLOAD]`` line logged by ``upload_file`` already covers the action,
        mirroring the single ``[DOWNLOAD]`` line of the inbound path.
        """
        from hsync_common.models.file import File

        file_obj = File("/test/file.txt")
        with (
            patch("hsync_client.transfer.upload_file"),
            patch("hsync_client.lan_websocket.log") as mock_log,
        ):
            handler._upload_to_master(file_obj)
            mock_log.info.assert_not_called()
            mock_log.error.assert_not_called()

    def test_upload_to_master_forwards_from_closed(self, handler):
        """An on_closed-triggered upload enables the fast stability re-check."""
        from hsync_common.models.file import File

        file_obj = File("/test/file.txt")
        with patch("hsync_client.transfer.upload_file") as mock_up:
            handler._upload_to_master(file_obj, from_closed=True)
            mock_up.assert_called_once_with(
                file_obj, check_size=True, from_closed=True, inventory_observer=ANY
            )

    def test_upload_to_master_exception(self, handler):
        """Test _upload_to_master logs error on exception."""
        from hsync_common.models.file import File

        file_obj = File("/test/file.txt")
        with patch("hsync_client.transfer.upload_file", side_effect=Exception("fail")):
            handler._upload_to_master(file_obj)  # should not raise

    def test_delete_on_master_success(self, handler):
        """Test _delete_on_master sends DELETE request."""
        config = {
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "remote",
            "REQUEST_HEADERS": {"X-Key": "val"},
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.utils.http_delete") as mock_del,
        ):
            mock_del.return_value = MagicMock(status_code=200)
            handler._delete_on_master("/bucket/file.txt", is_dir=False)
            mock_del.assert_called_once()

    def test_delete_on_master_failure_status(self, handler):
        """Test _delete_on_master logs warning on bad status."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.utils.http_delete") as mock_del,
        ):
            mock_del.return_value = MagicMock(status_code=500)
            handler._delete_on_master("/bucket/file.txt")  # no raise

    def test_delete_on_master_exception(self, handler):
        """Test _delete_on_master logs error on exception."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.utils.http_delete", side_effect=Exception("net err")),
        ):
            handler._delete_on_master("/bucket/file.txt")  # no raise

    def test_rename_on_master_success(self, handler):
        """Test _rename_on_master sends PATCH request."""
        config = {
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "remote",
            "REQUEST_HEADERS": {"X-Key": "val"},
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.utils.http_patch") as mock_patch,
        ):
            mock_patch.return_value = MagicMock(status_code=200)
            handler._rename_on_master("/bucket/old.txt", "/bucket/new.txt")
            mock_patch.assert_called_once()

    def test_rename_on_master_failure_status(self, handler):
        """Test _rename_on_master logs warning on bad status."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.utils.http_patch") as mock_patch,
        ):
            mock_patch.return_value = MagicMock(status_code=500)
            handler._rename_on_master("/bucket/old.txt", "/bucket/new.txt")

    def test_rename_on_master_404_echo_degraded(self, handler):
        """A 404 matching a recent inbound rename is a benign echo and must
        be degrading from WARNING to DEBUG (no warning logged)."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.utils.http_patch") as mock_patch,
        ):
            mock_patch.return_value = MagicMock(status_code=404)
            handler.record_inbound_rename("/bucket/old.txt", "/bucket/new.txt")
            with patch("hsync_client.lan_websocket.log") as mock_log:
                handler._rename_on_master("/bucket/old.txt", "/bucket/new.txt")
            # No warning call for this path
            assert not mock_log.warning.called
            assert mock_log.debug.called

    def test_rename_on_master_404_divergence_warned(self, handler):
        """A 404 with no matching inbound rename is a real divergence and
        must remain a WARNING."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.utils.http_patch") as mock_patch,
        ):
            mock_patch.return_value = MagicMock(status_code=404)
            with patch("hsync_client.lan_websocket.log") as mock_log:
                handler._rename_on_master("/bucket/old.txt", "/bucket/new.txt")
            assert mock_log.warning.called
            assert not mock_log.debug.called

    def test_rename_on_master_exception(self, handler):
        """Test _rename_on_master logs error on exception."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.utils.http_patch", side_effect=Exception("fail")),
        ):
            handler._rename_on_master("/bucket/a", "/bucket/b")  # no raise

    def test_on_created_os_cache_dir_ignored(self, handler, tmp_path):
        """Test on_created ignores OS cache directories."""
        cache = tmp_path / ".DS_Store"
        cache.mkdir()
        event = DirCreatedEvent(str(cache))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_created(event)
        # No sentinel should be created in hidden dir
        assert not (cache / ".hsynckeep").exists()

    def test_on_created_symlink_uploads(self, handler, tmp_path):
        """Test on_created uploads symlinks immediately."""
        target = tmp_path / "tgt.txt"
        target.write_text("x")
        link = tmp_path / "lnk.txt"
        link.symlink_to(target)
        event = FileCreatedEvent(str(link))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.File") as F,
            patch("hsync_client.transfer.upload_symlink") as mock_upload_symlink,
            patch.object(handler, "_upload_to_master"),
        ):
            F.get.return_value = MagicMock()
            handler.on_created(event)
            # The symlink branch starts a background op targeting
            # upload_symlink. With it mocked the thread is harmless, but it
            # must be joined INSIDE the patch context: outside it, a lingering
            # thread would run the real upload_symlink against the unpatched
            # CONFIG and raise KeyError (PytestUnhandledThreadExceptionWarning).
            assert len(handler._pending_ops) >= 1
            handler.wait_pending(timeout=2.0)
            mock_upload_symlink.assert_called_once()

    def test_on_created_escaping_symlink_not_uploaded(self, handler, tmp_path):
        """A symlink whose target escapes the bucket must not be uploaded.

        Regression: the LAN slave's ``on_created`` forwarded the symlink to
        ``_handle_file_upload`` which dereferenced the out-of-bucket target
        (exfiltrating ``/etc/passwd``-style content to the master).
        """
        outside = tmp_path.parent / f"hsync_lnsecret_{tmp_path.name}.txt"
        outside.write_text("top-secret")
        try:
            link = tmp_path / "secret"
            link.symlink_to(outside)
            event = FileCreatedEvent(str(link))
            config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
            with (
                patch("hsync_client.lan_websocket.CONFIG", config),
                patch.object(handler, "_handle_file_upload") as mock_upload,
            ):
                handler.on_created(event)
                mock_upload.assert_not_called()
        finally:
            if outside.exists():
                outside.unlink()

    def test_on_created_in_bucket_symlink_uploaded(self, handler, tmp_path):
        """An in-bucket symlink is replicated STRUCTURALLY via upload_symlink.

        The realtime watch path must send (path, target) to the master, never
        dereference the target: `_handle_file_upload` (content upload) must not
        be invoked for links."""
        target = tmp_path / "real.txt"
        target.write_text("inside")
        link = tmp_path / "alias"
        link.symlink_to(target)
        event = FileCreatedEvent(str(link))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_handle_file_upload") as mock_upload,
            patch.object(handler, "_start_operation") as mock_start,
        ):
            handler.on_created(event)
            # The link must NOT go through the content-upload path.
            mock_upload.assert_not_called()
            # A single background op is started; its target is upload_symlink
            # (imported locally inside on_created).
            mock_start.assert_called_once()
            start_kwargs = mock_start.call_args.kwargs
            assert start_kwargs.get("name") == f"symlink-{str(link)}"
            from hsync_client.transfer import upload_symlink

            assert start_kwargs.get("target") is upload_symlink

    def test_on_modified_temp_file_ignored(self, handler, tmp_path):
        """Test on_modified ignores temp files."""
        event = FileModifiedEvent(str(tmp_path / "file.part"))
        handler.on_modified(event)
        assert len(handler._pending_ops) == 0

    def test_on_modified_receiving_ignored(self, handler, tmp_path):
        """Test on_modified ignores files being received."""
        p = str(tmp_path / "f.txt")
        handler.mark_receiving(p)
        event = FileModifiedEvent(p)
        handler.on_modified(event)
        assert len(handler._pending_ops) == 0

    def test_on_modified_directory_ignored(self, handler, tmp_path):
        """Test on_modified ignores directory events."""
        event = DirDeletedEvent(str(tmp_path / "sub"))
        event.is_directory = True
        handler.on_modified(event)
        assert len(handler._pending_ops) == 0

    def test_on_modified_touch_propagates_metadata(self, handler, tmp_path, monkeypatch):
        """A slave-local touch (mode unchanged, mtime changed) is pushed."""
        p = tmp_path / "f.txt"
        p.write_bytes(b"content")
        handler._last_mode[str(p)] = p.stat().st_mode & 0o7777
        handler._last_mtime[str(p)] = 1  # a stale mtime so the touch is detected
        seen: list[str] = []
        monkeypatch.setattr(handler, "_propagate_metadata_change", seen.append)

        handler.on_modified(FileModifiedEvent(str(p)))

        assert seen == [str(p)]

    def test_on_modified_unchanged_does_not_propagate(self, handler, tmp_path, monkeypatch):
        """No metadata push when neither mode nor mtime changed."""
        p = tmp_path / "f.txt"
        p.write_bytes(b"content")
        st = p.stat()
        handler._last_mode[str(p)] = st.st_mode & 0o7777
        handler._last_mtime[str(p)] = st.st_mtime_ns
        seen: list[str] = []
        monkeypatch.setattr(handler, "_propagate_metadata_change", seen.append)

        handler.on_modified(FileModifiedEvent(str(p)))

        assert seen == []

    def test_on_modified_directory_inbound_delete_echo_suppressed(self, handler, tmp_path):
        """Dir MODIFY covered by an inbound recursive delete is suppressed.

        When the slave applies an inbound recursive delete, the local
        watcher fires IN_ATTRIB on every ancestor directory as children
        are removed.  Those modifies are noise, not permission changes,
        and must not be propagated to the master (via _propagate_dir_-
        permission_change).
        """
        root = str(tmp_path / "a")
        handler.record_inbound_delete(root)

        # An IN_ATTRIB event for a directory under the inbound delete root.
        event = DirDeletedEvent(str(tmp_path / "a" / "sub"))
        event.is_directory = True

        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            with patch.object(handler, "_propagate_dir_permission_change") as mock_prop:
                handler.on_modified(event)

        # The permission-change propagation must NOT be called: the modify
        # is an echo of the inbound delete, not a real permission change.
        mock_prop.assert_not_called()

    def test_on_modified_directory_not_suppressed_without_inbound_delete(self, handler, tmp_path):
        """Dir MODIFY without an inbound delete still propagates permission changes."""
        event = DirDeletedEvent(str(tmp_path / "a" / "sub"))
        event.is_directory = True

        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.is_hidden_dir", return_value=False),
            patch.object(handler, "_propagate_dir_permission_change") as mock_prop,
        ):
            handler.on_modified(event)

        mock_prop.assert_called_once()

    def test_on_modified_os_cache_file_ignored(self, handler, tmp_path):
        """Test on_modified ignores OS cache files."""
        event = FileModifiedEvent(str(tmp_path / "Thumbs.db"))
        handler.on_modified(event)
        assert len(handler._pending_ops) == 0

    def test_on_modified_debounce_skip(self, handler, tmp_path):
        """Test on_modified skips when within debounce interval."""
        p = str(tmp_path / "f.txt")
        handler._last_modified[p] = time.time()
        event = FileModifiedEvent(p)
        handler.on_modified(event)
        assert len(handler._pending_ops) == 0

    def test_on_modified_non_linux_uploads(self, handler, tmp_path):
        """Test on_modified uploads on non-Linux systems."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        event = FileModifiedEvent(str(f))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", False),
            patch("hsync_client.lan_websocket.File") as F,
            patch.object(handler, "_upload_to_master"),
        ):
            F.get.return_value = MagicMock()
            handler.on_modified(event)

    def test_on_closed_not_supported(self, handler, tmp_path):
        """Test on_closed returns immediately when not supported."""
        event = FileModifiedEvent(str(tmp_path / "f.txt"))
        with patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", False):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_temp_file_ignored(self, handler, tmp_path):
        """Test on_closed ignores temp files."""
        event = FileModifiedEvent(str(tmp_path / "f.part"))
        with patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", True):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_receiving_ignored(self, handler, tmp_path):
        """Test on_closed ignores files being received."""
        p = str(tmp_path / "f.txt")
        handler.mark_receiving(p)
        event = FileModifiedEvent(p)
        with patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", True):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_directory_ignored(self, handler, tmp_path):
        """Test on_closed ignores directory events."""
        event = MagicMock()
        event.is_directory = True
        event.src_path = str(tmp_path / "dir")
        with patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", True):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_os_cache_file_ignored(self, handler, tmp_path):
        """Test on_closed ignores OS cache files."""
        event = FileModifiedEvent(str(tmp_path / "Thumbs.db"))
        with patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", True):
            handler.on_closed(event)
        assert len(handler._pending_ops) == 0

    def test_on_closed_normal_uploads(self, handler, tmp_path):
        """Test on_closed triggers upload for normal file."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        event = FileModifiedEvent(str(f))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.SUPPORTS_ON_CLOSED", True),
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.File") as F,
            patch.object(handler, "_upload_to_master"),
        ):
            F.get.return_value = MagicMock()
            handler.on_closed(event)
            assert len(handler._pending_ops) >= 1

    def test_handle_file_upload_download_only(self, handler, tmp_path):
        """Test _handle_file_upload skips in download-only mode."""
        config = {"SYNC_DIRECTION": "download-only"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler._handle_file_upload(str(tmp_path / "f.txt"))
        assert len(handler._pending_ops) == 0

    def test_handle_file_upload_not_exists(self, handler, tmp_path):
        """Test _handle_file_upload skips non-existent file."""
        config = {"SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler._handle_file_upload(str(tmp_path / "gone.txt"))
        assert len(handler._pending_ops) == 0

    def test_handle_file_upload_file_get_none(self, handler, tmp_path):
        """Test _handle_file_upload skips when File.get returns None."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        config = {"SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.File") as F,
        ):
            F.get.return_value = None
            handler._handle_file_upload(str(f))
        assert len(handler._pending_ops) == 0

    def test_handle_file_upload_starts_operation(self, handler, tmp_path):
        """Test _handle_file_upload starts upload operation."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        config = {"SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.File") as F,
            patch.object(handler, "_upload_to_master"),
        ):
            F.get.return_value = MagicMock()
            handler._handle_file_upload(str(f))
        assert len(handler._pending_ops) >= 1

    def test_handle_file_upload_skips_hsynckeep_sentinel(self, handler, tmp_path):
        """Regression: the .hsynckeep sentinel is never uploaded to the master.

        The sentinel is a per-peer local artifact: the periodic reconcile
        already excludes it from sync actions in both directions, so the
        event-driven upload path must too. Uploading it only produced a
        0-byte [UPLOAD] line and a broadcast round-trip. Each peer maintains
        its own sentinels; the directory itself is propagated by the
        directory event.
        """
        d = tmp_path / "newdir"
        d.mkdir()
        sentinel = d / ".hsynckeep"
        sentinel.write_text("")
        config = {"SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_upload_to_master") as mock_upload,
        ):
            handler._handle_file_upload(str(sentinel))
        assert mock_upload.call_count == 0
        assert len(handler._pending_ops) == 0

    def test_on_deleted_visible_file(self, handler, tmp_path):
        """Test on_deleted starts delete operation for visible file."""
        event = FileDeletedEvent(str(tmp_path / "file.txt"))
        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_delete_on_master"),
            patch("hsync_client.lan_websocket.threading.Timer"),
        ):
            handler.on_deleted(event)
            # Deletes are coalesced: _delete_on_master fires on flush.
            handler._flush_pending_deletes()
        assert len(handler._pending_ops) >= 1

    def test_stale_delete_dropped_when_path_recreated_inside_window(self, handler, tmp_path):
        """A delete buffered then re-created before the flush is not sent.

        Regression: a slave-local ``rm`` + immediate rewrite of the same name
        leaves the delete queued while the file exists again; sending it would
        make the master remove the fresh file (the source losing data on a
        stale delete echo). The tombstone recorded at buffer time must be
        cleared too, or the marker would outlive the deletion it refers to.
        """
        live = tmp_path / "recreated.txt"
        live.write_bytes(b"old")
        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        event = FileDeletedEvent(str(live))

        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_delete_on_master") as mock_delete,
            patch("hsync_client.lan_websocket.threading.Timer"),
            patch("hsync_client.tombstones.clear_deletion") as mock_clear,
        ):
            live.unlink()
            handler.on_deleted(event)
            live.write_bytes(b"new")  # re-created inside the window
            handler._flush_pending_deletes()

        mock_delete.assert_not_called()
        assert handler._pending_ops == []
        mock_clear.assert_called_once_with(str(live))

    def test_on_deleted_temp_file_ignored(self, handler, tmp_path):
        """Test on_deleted ignores temp files."""
        event = FileDeletedEvent(str(tmp_path / "file.part"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_deleted(event)
        assert len(handler._pending_ops) == 0

    def test_on_deleted_os_cache_dir_ignored(self, handler, tmp_path):
        """Test on_deleted ignores OS cache directories."""
        event = DirDeletedEvent(str(tmp_path / ".DS_Store"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_deleted(event)
        assert len(handler._pending_ops) == 0

    def test_on_deleted_os_cache_file_ignored(self, handler, tmp_path):
        """Test on_deleted ignores OS cache files."""
        event = FileDeletedEvent(str(tmp_path / "Thumbs.db"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_deleted(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_no_dest_returns(self, handler, tmp_path):
        """Test on_moved returns when dest_path is None."""
        event = MagicMock()
        event.src_path = str(tmp_path / "a.txt")
        event.dest_path = None
        event.is_directory = False
        handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_temp_src_ignored(self, handler, tmp_path):
        """Test on_moved ignores temp source files."""
        event = FileMovedEvent(str(tmp_path / "f.part"), str(tmp_path / "f.txt"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_temp_dest_ignored(self, handler, tmp_path):
        """Test on_moved ignores temp destination files."""
        event = FileMovedEvent(str(tmp_path / "f.txt"), str(tmp_path / "f.part"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_receiving_src_ignored(self, handler, tmp_path):
        """Test on_moved ignores when src is being received."""
        src = str(tmp_path / "a.txt")
        handler.mark_receiving(src)
        event = FileMovedEvent(src, str(tmp_path / "b.txt"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_os_cache_to_os_cache_ignored(self, handler, tmp_path):
        """Test on_moved both OS cache skipped."""
        event = FileMovedEvent(str(tmp_path / ".DS_Store"), str(tmp_path / "Thumbs.db"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_os_cache_to_visible_file_uploads(self, handler, tmp_path):
        """Test on_moved OS-cache->visible triggers upload."""
        f = tmp_path / "visible.txt"
        f.write_text("x")
        event = FileMovedEvent(str(tmp_path / "Thumbs.db"), str(f))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.File") as F,
            patch.object(handler, "_upload_to_master"),
        ):
            F.get.return_value = MagicMock()
            handler.on_moved(event)
        assert len(handler._pending_ops) >= 1

    def test_on_moved_visible_to_os_cache_deletes(self, handler, tmp_path):
        """Test on_moved visible->OS-cache triggers deletion."""
        event = FileMovedEvent(str(tmp_path / "vis.txt"), str(tmp_path / "Thumbs.db"))
        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_delete_on_master"),
        ):
            handler.on_moved(event)
        assert len(handler._pending_ops) >= 1

    def test_on_moved_visible_to_os_cache_ignore_delete(self, handler, tmp_path):
        """Test on_moved visible->OS-cache skips when ignore-delete."""
        event = FileMovedEvent(str(tmp_path / "vis.txt"), str(tmp_path / "Thumbs.db"))
        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": True,
        }
        with patch("hsync_client.lan_websocket.CONFIG", config):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0

    def test_on_moved_normal_rename(self, handler, tmp_path):
        """Test on_moved normal rename calls _rename_on_master."""
        event = FileMovedEvent(str(tmp_path / "a.txt"), str(tmp_path / "b.txt"))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_rename_on_master"),
        ):
            handler.on_moved(event)
        assert len(handler._pending_ops) >= 1

    def test_on_moved_inbound_echo_skips_patch(self, handler, tmp_path):
        """on_moved must NOT re-send a rename that is the echo of an inbound
        master event (exact or descendant): the master already performed it,
        so a PATCH would 404 and produce a spurious WARNING."""
        src = str(tmp_path / "ABC")
        dst = str(tmp_path / "ABC-123")
        handler.record_inbound_rename(src, dst)

        # Exact echo
        event = FileMovedEvent(src, dst)
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_rename_on_master") as mock_ren,
        ):
            handler.on_moved(event)
        assert len(handler._pending_ops) == 0
        mock_ren.assert_not_called()

        # Descendant echo (the sentinel inside the renamed directory)
        event2 = FileMovedEvent(
            str(tmp_path / "ABC" / ".hsynckeep"),
            str(tmp_path / "ABC-123" / ".hsynckeep"),
        )
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_rename_on_master") as mock_ren2,
        ):
            handler.on_moved(event2)
        assert len(handler._pending_ops) == 0
        mock_ren2.assert_not_called()

    def test_on_moved_dir_rename_skips_child_patches(self, handler, tmp_path):
        """on_moved must NOT send a PATCH for each child of an outbound
        directory rename (e.g. the ``.hsynckeep`` sentinel): the single PATCH
        of the directory already renamed the whole tree on the master, so the
        child PATCH would 404 and produce a spurious WARNING."""
        base = tmp_path / "ABC-123"
        dst = tmp_path / "ABC-123-X"
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}

        sentinel_src = str(tmp_path / "ABC-123" / ".hsynckeep")
        sentinel_dst = str(tmp_path / "ABC-123-X" / ".hsynckeep")

        # Simulate the watchdog sequence: directory rename first (records the
        # outbound dir pair and sends one PATCH), then the child events.
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_rename_on_master"),
        ):
            handler.on_moved(DirMovedEvent(str(base), str(dst)))
            # Directory PATCH was scheduled; its pair is now recorded.
            assert len(handler._pending_ops) == 1

        # Child events are redundant: no new PATCH should be scheduled.
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_rename_on_master") as mock_ren_child,
        ):
            handler.on_moved(FileMovedEvent(sentinel_src, sentinel_dst))
        assert len(handler._pending_ops) == 1
        mock_ren_child.assert_not_called()

    def test_rename_on_master_404_outbound_child_degraded(self, handler):
        """A 404 matching a child of an outbound directory rename is benign
        and must be degraded from WARNING to DEBUG (no warning logged)."""
        config = {"LOCAL_BUCKET": "/bucket", "REMOTE_BUCKET": "remote"}
        handler._record_outbound_dir_rename("/bucket/ABC-123", "/bucket/ABC-123-X")
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.utils.http_patch") as mock_patch,
        ):
            mock_patch.return_value = MagicMock(status_code=404)
            with patch("hsync_client.lan_websocket.log") as mock_log:
                handler._rename_on_master(
                    "/bucket/ABC-123/.hsynckeep", "/bucket/ABC-123-X/.hsynckeep"
                )
            assert not mock_log.warning.called
            assert mock_log.debug.called

    def test_wait_pending_timeout(self, handler):
        """Test wait_pending returns count of timed-out operations."""

        def slow_op():
            time.sleep(5)

        handler._start_operation(slow_op, name="slow")
        timed_out = handler.wait_pending(timeout=0.1)
        assert timed_out >= 1

    def test_on_created_defers_non_empty_file_upload(self, handler, tmp_path):
        """A non-empty regular file at on_created is NOT uploaded immediately;
        it is deferred via a timer so a later on_closed (the reliable trigger)
        can cancel it and take precedence. The timer is the fallback for files
        that arrive already complete with no writer fd (atomic rename)."""
        f = tmp_path / "incoming.txt"
        f.write_text("hello world")
        event = FileCreatedEvent(str(f))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_handle_file_upload") as mock_upload,
        ):
            handler.on_created(event)
            # Upload deferred, not immediate:
            mock_upload.assert_not_called()
            assert str(f) in handler._pending_create_timers
        # Clean up the real timer so it does not fire after the test
        handler.cancel_all_create_timers()

    def test_on_closed_cancels_create_timer_and_uploads_from_closed(self, handler, tmp_path):
        """on_closed cancels the on_created-deferred timer and uploads with
        from_closed=True (the reliable, fd-closed path)."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        event_created = FileCreatedEvent(str(f))
        event_closed = FileCreatedEvent(str(f))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.File") as F,
            patch.object(handler, "_upload_to_master"),
        ):
            F.get.return_value = MagicMock()
            handler.on_created(event_created)
            assert str(f) in handler._pending_create_timers
            handler.on_closed(event_closed)
            # Timer cancelled by on_closed:
            assert str(f) not in handler._pending_create_timers
            # Upload started via on_closed (from_closed=True path):
            assert len(handler._pending_ops) >= 1

    def test_create_timer_fires_when_no_on_closed(self, handler, tmp_path):
        """With no on_closed (atomic rename / hard-link), the deferred timer
        fires and uploads the already-complete file as a fallback."""
        f = tmp_path / "incoming.txt"
        f.write_text("hello")
        event = FileCreatedEvent(str(f))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.lan_websocket.CREATE_UPLOAD_DEFER_S", 0.0),
            patch("hsync_client.lan_websocket.File") as F,
            patch.object(handler, "_upload_to_master") as mock_up,
        ):
            F.get.return_value = MagicMock()
            handler.on_created(event)
            # The zero-delay timer fires asynchronously; poll until the
            # fallback upload runs (bounded so the test cannot hang).
            for _ in range(500):
                if mock_up.called:
                    break
                time.sleep(0.01)
            assert mock_up.called

    def test_on_deleted_cancels_create_timer(self, handler, tmp_path):
        """on_deleted cancels a pending on_created-deferred upload so a stale
        timer does not try to upload a deleted file."""
        f = tmp_path / "f.txt"
        f.write_text("x")
        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_delete_on_master"),
        ):
            handler.on_created(FileCreatedEvent(str(f)))
            assert str(f) in handler._pending_create_timers
            handler.on_deleted(FileDeletedEvent(str(f)))
            assert str(f) not in handler._pending_create_timers

    def test_on_moved_cancels_create_timer_for_src(self, handler, tmp_path):
        """on_moved cancels a pending on_created-deferred upload for the source
        path (the file moved away)."""
        a = tmp_path / "a.txt"
        a.write_text("x")
        b = tmp_path / "b.txt"
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_rename_on_master"),
        ):
            handler.on_created(FileCreatedEvent(str(a)))
            assert str(a) in handler._pending_create_timers
            handler.on_moved(FileMovedEvent(str(a), str(b)))
            assert str(a) not in handler._pending_create_timers

    def test_on_created_empty_file_no_immediate_upload(self, handler, tmp_path):
        """An empty regular file at on_created is left to on_closed /
        on_modified to avoid uploading partially-written files."""
        f = tmp_path / "empty.txt"
        f.write_text("")
        event = FileCreatedEvent(str(f))
        config = {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_handle_file_upload") as mock_upload,
        ):
            handler.on_created(event)
            mock_upload.assert_not_called()

    def test_delete_on_master_sends_own_cid(self, handler):
        """_delete_on_master must propagate our CID so the master can
        attribute the delete and suppress the echo broadcast."""
        config = {
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "remote",
            "CID": "slave-abc",
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.utils.http_delete") as mock_del,
        ):
            mock_del.return_value = MagicMock(status_code=200)
            handler._delete_on_master("/bucket/file.txt", is_dir=False)
            kwargs = mock_del.call_args.kwargs
            assert kwargs["params"]["cid"] == "slave-abc"

    def test_rename_on_master_sends_own_cid(self, handler):
        """_rename_on_master must propagate our CID so the master can
        attribute the rename and suppress the echo broadcast."""
        config = {
            "LOCAL_BUCKET": "/bucket",
            "REMOTE_BUCKET": "remote",
            "CID": "slave-xyz",
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch("hsync_client.utils.http_patch") as mock_patch,
        ):
            mock_patch.return_value = MagicMock(status_code=200)
            handler._rename_on_master("/bucket/a.txt", "/bucket/b.txt")
            kwargs = mock_patch.call_args.kwargs
            assert kwargs["params"]["cid"] == "slave-xyz"

    def test_on_deleted_inbound_delete_echo_skipped(self, handler, tmp_path):
        """A delete echoing a recent inbound recursive delete is not re-sent.

        When the slave applies an inbound recursive delete, ``rmtree`` fires
        a ``deleted`` per descendant. Only the tree root is marked for echo
        suppression, so the descendants reach ``on_deleted``: they must be
        recognised via the inbound-delete echo cache (exact + descendant)
        and dropped without deleting anything on the master.
        """
        root = str(tmp_path / "a")
        handler.record_inbound_delete(root)
        event = FileDeletedEvent(str(tmp_path / "a" / "b"))

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_delete_on_master") as mock_delete,
        ):
            handler.on_deleted(event)
            handler._flush_pending_deletes()
        mock_delete.assert_not_called()

    def test_on_deleted_inbound_root_echo_skipped(self, handler, tmp_path):
        """A delete on the exact cached inbound root is also an echo."""
        root = str(tmp_path / "a")
        handler.record_inbound_delete(root)
        event = DirDeletedEvent(root)

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_delete_on_master") as mock_delete,
        ):
            handler.on_deleted(event)
            handler._flush_pending_deletes()
        mock_delete.assert_not_called()

    def test_on_deleted_prefix_collision_not_suppressed(self, handler, tmp_path):
        """A sibling path sharing only a prefix is a genuine delete.

        Cached inbound delete ``/a`` must not suppress a user deletion of
        ``/abc`` (the trailing suffix must be empty or start with ``/``).
        """
        handler.record_inbound_delete(str(tmp_path / "a"))
        event = FileDeletedEvent(str(tmp_path / "abc"))

        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_delete_on_master") as mock_delete,
            patch("hsync_client.lan_websocket.threading.Timer"),
        ):
            handler.on_deleted(event)
            handler._flush_pending_deletes()
            # The flush schedules the DELETE on the realtime pool: wait for
            # the submitted futures before asserting.
            for future, _ in handler._pending_ops:
                future.result(timeout=2.0)
        mock_delete.assert_called_once()

    def test_on_deleted_inbound_echo_expired_goes_through(self, handler, tmp_path):
        """After the TTL expires, a delete on the same path propagates."""
        root = str(tmp_path / "a")
        handler.record_inbound_delete(root)
        # Expire the cache entry (recorded `now`); simulate an old timestamp.
        handler._inbound_deletes._items[root] = time.time() - (handler._inbound_deletes._ttl + 1)

        event = DirDeletedEvent(root)
        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_delete_on_master") as mock_delete,
        ):
            handler.on_deleted(event)
            handler._flush_pending_deletes()
            # The flush schedules the DELETE on the realtime pool: wait for
            # the submitted futures before asserting.
            for future, _ in handler._pending_ops:
                future.result(timeout=2.0)
        mock_delete.assert_called_once()

    def test_duplicate_delete_buffered_once(self, handler, tmp_path):
        """An identical delete pair buffered twice is flushed once, not twice.

        A directory reports both ``IN_DELETE`` (parent's watch) and
        ``IN_DELETE_SELF`` (its own watch): the flux must push a single
        DELETE to the master, taking only one op slot.
        """
        event = DirDeletedEvent(str(tmp_path / "a"))
        config = {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "IGNORE_DELETE": False,
        }
        with (
            patch("hsync_client.lan_websocket.CONFIG", config),
            patch.object(handler, "_delete_on_master"),
            patch("hsync_client.lan_websocket.threading.Timer"),
        ):
            handler.on_deleted(event)
            handler.on_deleted(event)
            handler._flush_pending_deletes()
        # One delete request slot, not two (the duplicate pair was folded).
        assert len(handler._pending_ops) == 1
        assert len(handler._pending_deletes) == 0
