# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for local file operations in sync module."""

import hashlib
import os
import time
from unittest.mock import patch

from hsync_common.models.tombstone import tombstone_key

from hsync_client import sync_state as ss_mod
from hsync_client import tombstones as ts_mod
from hsync_client.models.file import File
from hsync_client.models.sync_action import SyncActionName
from hsync_client.sync import (
    _save_conflict_copy,
    _tombstone_deletes_local,
    get_files_local,
    get_sync_actions,
    push_tombstones_remote,
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
    """Tests for same-mtime conflict handling."""

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


class TestClientTombstonesModule:
    """Tests for the per-peer tombstone store (hsync_client.tombstones)."""

    def test_record_then_payload_uses_hashed_key(self, tmp_path):
        """A recorded deletion appears in the payload under its hashed key."""
        ts = int(time.time())
        with patch("hsync_client.tombstones.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ts_mod.reset_cache()
            ts_mod.record_deletion(str(tmp_path / "gone.txt"), sha256sum="abc", timestamp=ts)

            payload = ts_mod.get_payload()
            key = tombstone_key("gone.txt")
            assert key in payload
            assert payload[key] == {"ts": ts, "sha256": "abc"}
            ts_mod.reset_cache()

    def test_record_persists_and_reloads(self, tmp_path):
        """The marker survives a cache reset by being read back from the side-car."""
        ts = int(time.time())
        with patch("hsync_client.tombstones.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ts_mod.reset_cache()
            ts_mod.record_deletion(str(tmp_path / "gone.txt"), sha256sum="abc", timestamp=ts)
            # Drop the in-memory cache so the next access loads from disk.
            ts_mod.reset_cache()

            assert tombstone_key("gone.txt") in ts_mod.get_payload()
            ts_mod.reset_cache()

    def test_clear_removes_marker(self, tmp_path):
        """clear_deletion drops a previously recorded marker."""
        with patch("hsync_client.tombstones.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ts_mod.reset_cache()
            ts_mod.record_deletion(str(tmp_path / "gone.txt"), sha256sum="abc", timestamp=1000)
            ts_mod.clear_deletion(str(tmp_path / "gone.txt"))

            assert ts_mod.get_payload() == {}
            ts_mod.reset_cache()

    def test_merge_payload_lww(self, tmp_path):
        """merge_payload adds newer remote markers and reports the change count."""
        now = int(time.time())
        with patch("hsync_client.tombstones.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ts_mod.reset_cache()
            ts_mod.record_deletion(str(tmp_path / "a.txt"), sha256sum="a", timestamp=now)

            key_a = tombstone_key("a.txt")
            key_b = tombstone_key("b.txt")
            remote = {
                key_a: {"ts": now + 1000, "sha256": "a2"},  # newer wins
                key_b: {"ts": now - 1000, "sha256": "b"},  # new entry
            }
            changed = ts_mod.merge_payload(remote)

            payload = ts_mod.get_payload()
            assert changed == 2
            assert payload[key_a] == {"ts": now + 1000, "sha256": "a2"}
            assert payload[key_b] == {"ts": now - 1000, "sha256": "b"}
            ts_mod.reset_cache()

    def test_record_outside_bucket_is_noop(self, tmp_path):
        """A path that does not resolve inside the bucket records nothing."""
        with patch("hsync_client.tombstones.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ts_mod.reset_cache()
            # The bucket root itself has an empty relative path.
            ts_mod.record_deletion(str(tmp_path), sha256sum="abc", timestamp=1000)

            assert ts_mod.get_payload() == {}
            ts_mod.reset_cache()


class TestPushTombstonesRemote:
    """Tests for the best-effort gossip-up push helper."""

    _CONFIG = {
        "API": {"buckets": "https://master/api/v1/buckets"},
        "REMOTE_BUCKET": "remote",
        "REQUEST_HEADERS": {"Authorization": "Bearer x"},
    }

    def test_empty_payload_does_not_post(self):
        """An empty payload short-circuits without any network call."""
        with (
            patch("hsync_client.sync.CONFIG", self._CONFIG),
            patch("hsync_client.sync.post") as mock_post,
        ):
            push_tombstones_remote({})
            mock_post.assert_not_called()

    def test_non_empty_payload_posts_to_master(self):
        """A non-empty payload is POSTed to the master tombstones endpoint."""
        payload = {tombstone_key("a.txt"): {"ts": 1, "sha256": "a"}}
        with (
            patch("hsync_client.sync.CONFIG", self._CONFIG),
            patch("hsync_client.sync.verify_master_fingerprint", return_value=True),
            patch("hsync_client.sync.get_ssl_verify", return_value=True),
            patch("hsync_client.sync.post") as mock_post,
        ):
            push_tombstones_remote(payload)

            assert mock_post.call_count == 1
            args, kwargs = mock_post.call_args
            assert args[0] == "https://master/api/v1/buckets/remote/tombstones"
            assert kwargs["json"] == payload

    def test_fingerprint_failure_skips_post(self):
        """A failed fingerprint check prevents the push without raising."""
        payload = {tombstone_key("a.txt"): {"ts": 1, "sha256": "a"}}
        with (
            patch("hsync_client.sync.CONFIG", self._CONFIG),
            patch("hsync_client.sync.verify_master_fingerprint", return_value=False),
            patch("hsync_client.sync.post") as mock_post,
        ):
            push_tombstones_remote(payload)
            mock_post.assert_not_called()

    def test_request_exception_is_swallowed(self):
        """A transport error never propagates out of the best-effort push."""
        from requests import RequestException

        payload = {tombstone_key("a.txt"): {"ts": 1, "sha256": "a"}}
        with (
            patch("hsync_client.sync.CONFIG", self._CONFIG),
            patch("hsync_client.sync.verify_master_fingerprint", return_value=True),
            patch("hsync_client.sync.get_ssl_verify", return_value=True),
            patch("hsync_client.sync.post", side_effect=RequestException("boom")),
        ):
            # Must not raise.
            push_tombstones_remote(payload)


def _sha(text: str) -> str:
    """SHA-256 hex digest of a short text payload (test helper)."""
    return hashlib.sha256(text.encode()).hexdigest()


class TestThreeWayMerge:
    """get_sync_actions resolves direction from a recorded base, clock-independently."""

    @staticmethod
    def _remote(relpath: str, sha256: str, mtime: int) -> dict:
        # Bucket-relative filepath: get_sync_actions joins it with LOCAL_BUCKET.
        return {
            "filepath": relpath,
            "name": relpath,
            "sha256sum": sha256,
            "mtime": mtime,
            "ctime": mtime,
            "size": 1,
            "permissions": "-rw-r--r--",
        }

    def _run(self, tmp_path, *, local_text, remote_sha, base_hash):
        """Drive get_sync_actions with equal mtimes so only the base decides."""
        local_file = tmp_path / "f.txt"
        local_file.write_text(local_text)
        lf = File.get(str(local_file))
        assert lf is not None
        remote = self._remote("f.txt", remote_sha, lf.mtime or 0)
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync_state.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
        ):
            ss_mod.reset_cache()
            if base_hash is not None:
                ss_mod.record_base(str(local_file), base_hash)
            result = get_sync_actions([remote], [lf.to_dict()], tombstones={})
            base_after = ss_mod.get_base_hash(str(local_file))
            ss_mod.reset_cache()
        return lf, result, base_after

    def test_only_remote_changed_downloads_despite_equal_mtime(self, tmp_path):
        """Local matches base, remote moved on: clean download (no conflict)."""
        # base == local content hash; remote diverged -> clean download.
        _, result, _ = self._run(
            tmp_path, local_text="LOCAL", remote_sha=_sha("REMOTE"), base_hash=_sha("LOCAL")
        )
        assert len(result) == 1
        assert result[0].name == SyncActionName.download
        assert getattr(result[0], "conflict", False) is False

    def test_only_local_changed_uploads_despite_equal_mtime(self, tmp_path):
        """Remote matches base, local moved on: clean upload (no conflict)."""
        base = _sha("BASE")
        _, result, _ = self._run(tmp_path, local_text="LOCAL", remote_sha=base, base_hash=base)
        assert len(result) == 1
        assert result[0].name == SyncActionName.upload
        assert getattr(result[0], "conflict", False) is False

    def test_both_changed_is_conflict(self, tmp_path):
        """Both sides diverged from base: remote wins but local is preserved."""
        _, result, _ = self._run(
            tmp_path, local_text="LOCAL", remote_sha=_sha("REMOTE"), base_hash=_sha("BASE")
        )
        assert len(result) == 1
        assert result[0].name == SyncActionName.download
        assert getattr(result[0], "conflict", False) is True

    def test_identical_content_is_in_sync_and_refreshes_base(self, tmp_path):
        """Equal content needs no action and refreshes the recorded base."""
        local_file = tmp_path / "f.txt"
        local_file.write_text("SAME")
        lf = File.get(str(local_file))
        assert lf is not None
        remote = self._remote("f.txt", lf.sha256sum or "", lf.mtime or 0)
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync_state.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
        ):
            ss_mod.reset_cache()
            result = get_sync_actions([remote], [lf.to_dict()], tombstones={})
            base_after = ss_mod.get_base_hash(str(local_file))
            ss_mod.reset_cache()
        assert len(result) == 1
        assert result[0].name == SyncActionName.nothing
        assert base_after == lf.sha256sum

    def test_unknown_base_falls_back_to_legacy_tie(self, tmp_path):
        """With no recorded base, an equal-mtime content clash uses the legacy tie."""
        _, result, _ = self._run(
            tmp_path, local_text="LOCAL", remote_sha=_sha("REMOTE"), base_hash=None
        )
        # Legacy: same mtime, different content -> remote wins, marked conflict.
        assert len(result) == 1
        assert result[0].name == SyncActionName.download
        assert getattr(result[0], "conflict", False) is True


class TestClientSyncStateModule:
    """Tests for the per-peer sync-state store (hsync_client.sync_state)."""

    def test_record_then_get_base_hash(self, tmp_path):
        with patch("hsync_client.sync_state.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ss_mod.reset_cache()
            ss_mod.record_base(str(tmp_path / "a.txt"), "hh")
            assert ss_mod.get_base_hash(str(tmp_path / "a.txt")) == "hh"
            ss_mod.reset_cache()

    def test_record_empty_hash_is_noop(self, tmp_path):
        with patch("hsync_client.sync_state.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ss_mod.reset_cache()
            ss_mod.record_base(str(tmp_path / "a.txt"), "")
            assert ss_mod.get_base_hash(str(tmp_path / "a.txt")) is None
            ss_mod.reset_cache()

    def test_record_outside_bucket_is_noop(self, tmp_path):
        with patch("hsync_client.sync_state.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ss_mod.reset_cache()
            ss_mod.record_base(str(tmp_path), "hh")  # bucket root -> empty relpath
            assert ss_mod.get_base_hash(str(tmp_path)) is None
            ss_mod.reset_cache()

    def test_clear_base_removes_entry(self, tmp_path):
        with patch("hsync_client.sync_state.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ss_mod.reset_cache()
            ss_mod.record_base(str(tmp_path / "a.txt"), "hh")
            ss_mod.clear_base(str(tmp_path / "a.txt"))
            assert ss_mod.get_base_hash(str(tmp_path / "a.txt")) is None
            ss_mod.reset_cache()

    def test_retain_drops_absent_paths(self, tmp_path):
        with patch("hsync_client.sync_state.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ss_mod.reset_cache()
            ss_mod.record_base(str(tmp_path / "keep.txt"), "h1")
            ss_mod.record_base(str(tmp_path / "drop.txt"), "h2")
            dropped = ss_mod.retain([str(tmp_path / "keep.txt")])
            assert dropped == 1
            assert ss_mod.get_base_hash(str(tmp_path / "keep.txt")) == "h1"
            assert ss_mod.get_base_hash(str(tmp_path / "drop.txt")) is None
            ss_mod.reset_cache()

    def test_flush_persists_and_reloads(self, tmp_path):
        with patch("hsync_client.sync_state.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ss_mod.reset_cache()
            ss_mod.record_base(str(tmp_path / "a.txt"), "hh")
            ss_mod.flush()
            # Drop the in-memory cache so the next access loads from the side-car.
            ss_mod.reset_cache()
            assert ss_mod.get_base_hash(str(tmp_path / "a.txt")) == "hh"
            ss_mod.reset_cache()
