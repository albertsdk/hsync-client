# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for local file operations in sync module."""

import os
from unittest.mock import patch

from hsync_common.models.tombstone import tombstone_key

from hsync_client.models.file import File
from hsync_client.models.sync_action import SyncActionName
from hsync_client.sync import (
    _save_conflict_copy,
    _tombstone_deletes_local,
    get_files_local,
    get_sync_actions,
)


class TestGetFilesLocal:
    """Tests for get_files_local function."""

    def test_returns_empty_list_for_empty_directory(self, tmp_path):
        """Empty directory returns empty list."""
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()
            assert result == []

    def test_returns_files_in_directory(self, tmp_path):
        """Files in directory are returned."""
        # Create test files
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "file2.txt").write_text("content2")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()
            assert len(result) == 2
            filepaths = [f["filepath"] for f in result]
            assert any("file1.txt" in fp for fp in filepaths)
            assert any("file2.txt" in fp for fp in filepaths)

    def test_skips_hidden_files(self, tmp_path):
        """Hidden files are skipped."""
        (tmp_path / "visible.txt").write_text("visible")
        (tmp_path / ".hidden").write_text("hidden")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()
            assert len(result) == 1
            assert "visible.txt" in result[0]["filepath"]

    def test_skips_hidden_directories(self, tmp_path):
        """Hidden directories are skipped."""
        visible_dir = tmp_path / "visible"
        visible_dir.mkdir()
        (visible_dir / "file.txt").write_text("visible")

        hidden_dir = tmp_path / ".hidden"
        hidden_dir.mkdir()
        (hidden_dir / "file.txt").write_text("hidden")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()
            assert len(result) == 1
            assert "visible" in result[0]["filepath"]

    def test_handles_nested_directories(self, tmp_path):
        """Files in nested directories are returned."""
        nested = tmp_path / "level1" / "level2"
        nested.mkdir(parents=True)
        (nested / "deep_file.txt").write_text("deep content")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()
            assert len(result) == 1
            assert "deep_file.txt" in result[0]["filepath"]


class TestGetSyncActions:
    """Tests for get_sync_actions function."""

    def test_returns_empty_list_for_no_files(self):
        """No files returns empty list."""
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": "/tmp"}):
            result = get_sync_actions([], [])
            assert result == []

    def test_returns_empty_list_for_none_inputs(self):
        """None inputs returns empty list."""
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": "/tmp"}):
            result = get_sync_actions(None, None)
            assert result == []

    def test_download_action_for_remote_only_file(self, tmp_path):
        """File on remote only triggers download."""
        remote_files = [
            {
                "filepath": str(tmp_path / "remote_file.txt"),
                "name": "remote_file.txt",
                "mtime": 1000,
                "ctime": 1000,
                "size": 100,
                "sha256sum": "abc123",
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [])
            assert len(result) == 1
            assert result[0].name == SyncActionName.download

    def test_upload_action_for_local_only_file(self, tmp_path):
        """File on local only triggers upload."""
        # Create local file
        local_file = tmp_path / "local_file.txt"
        local_file.write_text("local content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions([], local_files)
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload

    def test_nothing_action_for_same_mtime(self, tmp_path):
        """Same mtime triggers no action."""
        local_file = tmp_path / "same_file.txt"
        local_file.write_text("content")
        mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()
        # Use relative filepath - the function will join with LOCAL_BUCKET
        remote_files = [
            {
                "filepath": "same_file.txt",
                "name": "same_file.txt",
                "mtime": mtime,
                "ctime": mtime,
                "size": 7,
                "sha256sum": local_file_dict["sha256sum"],
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.nothing

    def test_download_action_for_newer_remote(self, tmp_path):
        """Newer remote file triggers download."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("content")
        local_mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()
        remote_files = [
            {
                "filepath": "file.txt",
                "name": "file.txt",
                "mtime": local_mtime + 1000,  # Remote is newer
                "ctime": local_mtime + 1000,
                "size": 7,
                "sha256sum": "different_sha256",
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.download

    def test_upload_action_for_newer_local(self, tmp_path):
        """Newer local file triggers upload."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("content")
        local_mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()
        remote_files = [
            {
                "filepath": "file.txt",
                "name": "file.txt",
                "mtime": local_mtime - 1000,  # Remote is older
                "ctime": local_mtime - 1000,
                "size": 7,
                "sha256sum": "different_sha256",
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload

    def test_hsynckeep_never_in_sync_actions(self, tmp_path):
        """.hsynckeep is a per-peer local sentinel and must never appear in sync actions.

        Regression test for the bug where deleting a remote .hsynckeep also wiped
        the local bucket parent directory (cascading into the watchdog crash).
        """
        from hsync_client.constants import HSYNCKEEP_FILENAME

        # Create a local .hsynckeep so File.get returns a real object
        local_keep = tmp_path / HSYNCKEEP_FILENAME
        local_keep.write_text("")
        local_file_obj = File.get(str(local_keep))
        assert local_file_obj is not None
        local_files = [local_file_obj.to_dict()]

        # Remote also reports a .hsynckeep (different peer, same sentinel name)
        remote_files = [
            {
                "filepath": HSYNCKEEP_FILENAME,
                "name": HSYNCKEEP_FILENAME,
                "mtime": 1000,
                "ctime": 1000,
                "size": 0,
                "sha256sum": "bb25b1c2b2a7427401c823a90904a759e2bd7425205b0702bca5e27e937e7cff",
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            # Server mode and LAN mode must both ignore the sentinel completely
            for lan_mode in (False, True):
                result = get_sync_actions(remote_files, local_files, lan_mode=lan_mode)
                assert result == [], (
                    f"sync_actions must be empty for .hsynckeep "
                    f"(lan_mode={lan_mode}), got: {result}"
                )


class TestGetSyncActionsLanMode:
    """Tests for get_sync_actions function in LAN mode."""

    def test_delete_action_for_local_only_file_in_lan_download_only(self, tmp_path):
        """Local-only file triggers delete only in LAN download-only mode (mirror master)."""
        local_file = tmp_path / "local_only.txt"
        local_file.write_text("local content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]

        with patch(
            "hsync_client.sync.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "download-only"},
        ):
            result = get_sync_actions([], local_files, lan_mode=True)
            assert len(result) == 1
            assert result[0].name == SyncActionName.delete

    def test_upload_action_for_local_only_file_in_lan_bidirectional(self, tmp_path):
        """Local-only file is uploaded to master in LAN bidirectional mode."""
        local_file = tmp_path / "local_only.txt"
        local_file.write_text("local content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]

        with patch(
            "hsync_client.sync.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "bidirectional"},
        ):
            result = get_sync_actions([], local_files, lan_mode=True)
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload

    def test_upload_action_for_local_only_file_in_lan_upload_only(self, tmp_path):
        """Local-only file is uploaded to master in LAN upload-only mode."""
        local_file = tmp_path / "local_only.txt"
        local_file.write_text("local content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]

        with patch(
            "hsync_client.sync.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": "upload-only"},
        ):
            result = get_sync_actions([], local_files, lan_mode=True)
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload

    def test_upload_action_for_local_only_file_in_server_mode(self, tmp_path):
        """Local-only file triggers upload action in server mode (default)."""
        local_file = tmp_path / "local_only.txt"
        local_file.write_text("local content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions([], local_files, lan_mode=False)
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload

    def test_download_action_for_remote_only_file_in_lan_mode(self, tmp_path):
        """Remote-only file triggers download action in LAN mode."""
        remote_files = [
            {
                "filepath": "remote_only.txt",
                "name": "remote_only.txt",
                "mtime": 1000000,
                "ctime": 1000000,
                "size": 10,
                "sha256sum": "abc123",
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [], lan_mode=True)
            assert len(result) == 1
            assert result[0].name == SyncActionName.download

    def test_lan_mode_does_not_affect_existing_files(self, tmp_path):
        """LAN mode doesn't change behavior for files that exist on both sides."""
        local_file = tmp_path / "both_sides.txt"
        local_file.write_text("content")
        mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()

        # Remote file with same mtime
        remote_files = [
            {
                "filepath": "both_sides.txt",
                "name": "both_sides.txt",
                "mtime": mtime,
                "ctime": mtime,
                "size": 7,
                "sha256sum": local_file_dict["sha256sum"],
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [local_file_dict], lan_mode=True)
            assert len(result) == 1
            assert result[0].name == SyncActionName.nothing

    def test_lan_mode_download_newer_remote(self, tmp_path):
        """LAN mode downloads newer remote files."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("old content")
        local_mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()

        # Remote file is newer
        remote_files = [
            {
                "filepath": "file.txt",
                "name": "file.txt",
                "mtime": local_mtime + 100,
                "ctime": local_mtime + 100,
                "size": 11,
                "sha256sum": "different",
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [local_file_dict], lan_mode=True)
            assert len(result) == 1
            assert result[0].name == SyncActionName.download


class TestConflictResolution:
    """Tests for same-mtime conflict handling (analisi.md point 2.7)."""

    def test_same_mtime_different_content_flags_conflict(self, tmp_path):
        """Same mtime but different content downloads remote and flags conflict."""
        local_file = tmp_path / "conflict.txt"
        local_file.write_text("local content")
        mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()

        # Remote has the SAME mtime but a DIFFERENT checksum (unresolvable tie)
        remote_files = [
            {
                "filepath": "conflict.txt",
                "name": "conflict.txt",
                "mtime": mtime,
                "ctime": mtime,
                "size": 99,
                "sha256sum": "deadbeef" * 8,
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.download
            assert result[0].conflict is True

    def test_newer_remote_is_not_flagged_conflict(self, tmp_path):
        """A clearly newer remote downloads without being flagged as conflict."""
        local_file = tmp_path / "newer.txt"
        local_file.write_text("local content")
        mtime = int(os.path.getmtime(str(local_file)))

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()

        remote_files = [
            {
                "filepath": "newer.txt",
                "name": "newer.txt",
                "mtime": mtime + 100,
                "ctime": mtime + 100,
                "size": 99,
                "sha256sum": "deadbeef" * 8,
                "permissions": "-rw-r--r--",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.download
            assert result[0].conflict is False

    def test_save_conflict_copy_preserves_local_content(self, tmp_path):
        """_save_conflict_copy copies the local file to a .conflict-* sibling."""
        local_file = tmp_path / "data.txt"
        local_file.write_text("precious local data")

        file_obj = File.get(str(local_file))
        assert file_obj is not None

        conflict_path = _save_conflict_copy(file_obj)
        assert conflict_path is not None
        assert ".conflict-" in conflict_path
        # Original is untouched and the conflict copy holds the same content
        assert local_file.read_text() == "precious local data"
        from pathlib import Path

        assert Path(conflict_path).read_text() == "precious local data"

    def test_save_conflict_copy_missing_file_returns_none(self, tmp_path):
        """_save_conflict_copy is a no-op when the local file is absent."""
        file_obj = File.from_dict(
            {"filepath": "ghost.txt", "name": "ghost.txt"},
            str(tmp_path),
            False,
        )
        assert _save_conflict_copy(file_obj) is None


class TestTombstoneDecision:
    """Unit tests for the _tombstone_deletes_local data-safe rule."""

    def test_matching_content_and_older_local_deletes(self):
        """Same content and a deletion newer than the local file: delete."""
        entry = {"ts": 1000, "sha256": "abc"}
        assert _tombstone_deletes_local(entry, local_mtime=900, local_sha256="abc") is True

    def test_matching_content_but_recreated_after_keeps(self):
        """Same content but re-created clearly after the deletion: keep (upload)."""
        entry = {"ts": 1000, "sha256": "abc"}
        assert _tombstone_deletes_local(entry, local_mtime=2000, local_sha256="abc") is False

    def test_different_content_keeps(self):
        """Different content is treated as a local edit/re-creation: keep."""
        entry = {"ts": 5000, "sha256": "abc"}
        assert _tombstone_deletes_local(entry, local_mtime=100, local_sha256="xyz") is False

    def test_legacy_marker_newer_deletion_deletes(self):
        """No recorded hash falls back to timestamps: clearly newer deletion deletes."""
        entry = {"ts": 1000, "sha256": None}
        assert _tombstone_deletes_local(entry, local_mtime=900, local_sha256="abc") is True

    def test_legacy_marker_older_deletion_keeps(self):
        """No recorded hash and an older deletion than the local file: keep."""
        entry = {"ts": 800, "sha256": None}
        assert _tombstone_deletes_local(entry, local_mtime=900, local_sha256="abc") is False


class TestGetSyncActionsTombstones:
    """Tests for the tombstone-driven branch in get_sync_actions."""

    def test_solo_local_deleted_when_tombstone_matches(self, tmp_path):
        """A solo-local file unchanged since a remote deletion is removed, not uploaded."""
        local_file = tmp_path / "gone.txt"
        local_file.write_text("content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]
        tombstones = {
            tombstone_key("gone.txt"): {
                "ts": (file.mtime or 0) + 100,
                "sha256": file.sha256sum,
            }
        }

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions([], local_files, tombstones=tombstones)
            assert len(result) == 1
            assert result[0].name == SyncActionName.delete

    def test_solo_local_uploaded_when_content_differs(self, tmp_path):
        """A tombstone for a different content hash never deletes: the file is uploaded."""
        local_file = tmp_path / "gone.txt"
        local_file.write_text("content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]
        tombstones = {
            tombstone_key("gone.txt"): {
                "ts": (file.mtime or 0) + 100,
                "sha256": "deadbeef" * 8,
            }
        }

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions([], local_files, tombstones=tombstones)
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload

    def test_solo_local_uploaded_when_recreated_after_deletion(self, tmp_path):
        """A matching-content file re-created after the deletion is kept (uploaded)."""
        local_file = tmp_path / "gone.txt"
        local_file.write_text("content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]
        tombstones = {
            tombstone_key("gone.txt"): {
                "ts": (file.mtime or 0) - 100,
                "sha256": file.sha256sum,
            }
        }

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions([], local_files, tombstones=tombstones)
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload

    def test_solo_local_uploaded_without_tombstone(self, tmp_path):
        """With no matching tombstone the solo-local file is uploaded as before."""
        local_file = tmp_path / "fresh.txt"
        local_file.write_text("content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions([], local_files, tombstones={})
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload
