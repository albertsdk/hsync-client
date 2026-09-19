# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for local file operations in sync module."""

import hashlib
import os
import time
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from hsync_common.models.file import File, clear_sha256_cache
from hsync_common.models.sync_action import SyncActionName
from hsync_common.models.tombstone import tombstone_key
from hsync_common.models.version import merge_vclocks

from hsync_client import sync_state as ss_mod
from hsync_client import tombstones as ts_mod
from hsync_client.sync import (
    _mass_deletion_blocked,
    _save_conflict_copy,
    _tombstone_deletes_local,
    get_files_local,
    get_sync_actions,
    push_tombstones_remote,
)


@pytest.fixture(autouse=True)
def _deterministic_umask():
    """Force the canonical umask so test files are born with 0o644.

    The permission-only reconcile compares local file modes against the
    remote metadata; the historical fixtures hardcode ``"644"``. On hosts
    with a more permissive umask (e.g. 0o2) the local files would be 0o664
    and the tests would spuriously classify the otherwise-identical file as
    a permission divergence.
    """
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


class TestEnsureSentinel:
    """Tests for the shared ``ensure_sentinel`` / ``ensure_hsynckeep_files``."""

    def test_creates_sentinel_in_empty_dir(self, tmp_path):
        from hsync_client.sync import ensure_sentinel

        d = tmp_path / "empty"
        d.mkdir()
        assert ensure_sentinel(str(d)) is True
        assert (d / ".hsynckeep").exists()

    def test_no_sentinel_in_dir_with_file(self, tmp_path):
        from hsync_client.sync import ensure_sentinel

        d = tmp_path / "nonempty"
        d.mkdir()
        (d / "f.txt").write_text("x")
        assert ensure_sentinel(str(d)) is False
        assert not (d / ".hsynckeep").exists()

    def test_no_sentinel_in_dir_with_subdir(self, tmp_path):
        from hsync_client.sync import ensure_sentinel

        d = tmp_path / "withsub"
        d.mkdir()
        (d / "sub").mkdir()
        assert ensure_sentinel(str(d)) is False
        assert not (d / ".hsynckeep").exists()

    def test_missing_dir_returns_false(self, tmp_path):
        from hsync_client.sync import ensure_sentinel

        assert ensure_sentinel(str(tmp_path / "nope")) is False

    def test_walk_creates_sentinels_only_in_empty_subdirs(self, tmp_path):
        from hsync_client.sync import ensure_hsynckeep_files

        (tmp_path / "empty_sub").mkdir()
        (tmp_path / "full_sub").mkdir()
        (tmp_path / "full_sub" / "f.txt").write_text("x")
        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ensure_hsynckeep_files()
        assert (tmp_path / "empty_sub" / ".hsynckeep").exists()
        assert not (tmp_path / "full_sub" / ".hsynckeep").exists()


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

    def test_skips_os_cache_files(self, tmp_path):
        """OS cache files (.DS_Store, Thumbs.db) are skipped."""
        (tmp_path / "visible.txt").write_text("visible")
        (tmp_path / ".DS_Store").write_text("cache")
        (tmp_path / "Thumbs.db").write_text("cache")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()
            assert len(result) == 1
            assert "visible.txt" in result[0]["filepath"]

    def test_dotfiles_are_synced(self, tmp_path):
        """Dotfiles like .env, .gitignore are synced normally (industry standard)."""
        (tmp_path / "visible.txt").write_text("visible")
        (tmp_path / ".env").write_text("env")
        (tmp_path / ".gitignore").write_text("git")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()
            assert len(result) == 3

    def test_dotfile_directories_are_synced(self, tmp_path):
        """Dotfile directories like .git/ are synced normally (industry standard)."""
        visible_dir = tmp_path / "visible"
        visible_dir.mkdir()
        (visible_dir / "file.txt").write_text("visible")

        dot_dir = tmp_path / ".config"
        dot_dir.mkdir()
        (dot_dir / "file.txt").write_text("config")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()
            assert len(result) == 2

    def test_handles_nested_directories(self, tmp_path):
        """Files in nested directories are returned."""
        nested = tmp_path / "level1" / "level2"
        nested.mkdir(parents=True)
        (nested / "deep_file.txt").write_text("deep content")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()
            assert len(result) == 1
            assert "deep_file.txt" in result[0]["filepath"]

    def test_excludes_symlink_targeting_outside_bucket(self, tmp_path):
        """A file-symlink whose target escapes the bucket is not followed/exfiltrated."""
        # Real file living OUTSIDE the bucket.
        outside = tmp_path.parent / f"hsync_secret_{tmp_path.name}.txt"
        outside.write_text("top-secret-exfil")
        try:
            (tmp_path / "visible.txt").write_text("ok")
            (tmp_path / "secret").symlink_to(outside)

            with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
                result = get_files_local()

            filepaths = [f["filepath"] for f in result]
            assert any("visible.txt" in fp for fp in filepaths)
            # The escaping symlink must NOT appear in the local listing ...
            assert not any("secret" in fp for fp in filepaths)
            # ... and must certainly not carry the out-of-bucket content hash.
            assert not any(
                f.get("sha256sum") == hashlib.sha256(b"top-secret-exfil").hexdigest()
                for f in result
            )
        finally:
            if outside.exists():
                outside.unlink()

    def test_excludes_relative_symlink_escaping_bucket(self, tmp_path):
        """A relative symlink that resolves outside the bucket is excluded too."""
        outside = tmp_path.parent / f"hsync_relsecret_{tmp_path.name}.txt"
        outside.write_text("top-secret-rel")
        try:
            (tmp_path / "sub").mkdir()
            # Climbs two levels up from sub/ -> out of the bucket root.
            (tmp_path / "sub" / "escape").symlink_to("../../" + outside.name)

            with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
                result = get_files_local()

            filepaths = [f["filepath"] for f in result]
            assert not any("escape" in fp for fp in filepaths)
        finally:
            if outside.exists():
                outside.unlink()

    def test_keeps_symlink_targeting_inside_bucket(self, tmp_path):
        """A symlink whose target stays inside the bucket is still scanned."""
        real = tmp_path / "real.txt"
        real.write_text("inside")
        (tmp_path / "alias").symlink_to(real)

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_files_local()

        filepaths = [f["filepath"] for f in result]
        # The symlink itself is enumerated as a file (target inside the bucket).
        assert any("alias" in fp for fp in filepaths)


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
                "filepath": "remote_file.txt",
                "name": "remote_file.txt",
                "mtime": 1000,
                "ctime": 1000,
                "size": 100,
                "sha256sum": "abc123",
                "permissions": "644",
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
                "permissions": "644",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.nothing

    def test_download_action_for_newer_remote(self, tmp_path):
        """Remote causally ahead (dominant clock) triggers download."""
        local_file = tmp_path / "file.txt"
        local_file.write_text("content")
        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()
        # Remote has a vector clock that dominates the local (empty) clock.
        remote_files = [
            {
                "filepath": "file.txt",
                "name": "file.txt",
                "mtime": 0,
                "ctime": 0,
                "size": 7,
                "sha256sum": "different_sha256",
                "permissions": "644",
                "version": {
                    "vclock": {"TestClientB1234567890": 1},
                    "version_id": "vid",
                    "origin_cid": "TestClientB1234567890",
                    "ts": 0,
                },
            }
        ]

        with patch(
            "hsync_client.sync.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "CID": "TestClientA1234567890"},
        ):
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
                "permissions": "644",
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
                "permissions": "644",
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
                "permissions": "644",
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
                "permissions": "644",
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
                "permissions": "644",
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
                "permissions": "644",
            }
        ]

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.download
            assert result[0].conflict is True

    def test_newer_remote_is_not_flagged_conflict(self, tmp_path):
        """A remote with a dominant vector clock downloads without conflict."""
        local_file = tmp_path / "newer.txt"
        local_file.write_text("local content")

        local_file_obj = File.get(str(local_file))
        assert local_file_obj is not None
        local_file_dict = local_file_obj.to_dict()

        # Remote has a clock that dominates the local (empty) clock.
        remote_files = [
            {
                "filepath": "newer.txt",
                "name": "newer.txt",
                "mtime": 0,
                "ctime": 0,
                "size": 99,
                "sha256sum": "deadbeef" * 8,
                "permissions": "644",
                "version": {
                    "vclock": {"TestClientB1234567890": 1},
                    "version_id": "vid",
                    "origin_cid": "TestClientB1234567890",
                    "ts": 0,
                },
            }
        ]

        with patch(
            "hsync_client.sync.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "CID": "TestClientA1234567890"},
        ):
            result = get_sync_actions(remote_files, [local_file_dict])
            assert len(result) == 1
            assert result[0].name == SyncActionName.download
            assert result[0].conflict is False

    def test_save_conflict_copy_preserves_local_content(self, tmp_path):
        """_save_conflict_copy copies the local file to a _conflicted_copy_* sibling."""
        local_file = tmp_path / "data.txt"
        local_file.write_text("precious local data")

        file_obj = File.get(str(local_file))
        assert file_obj is not None

        conflict_path = _save_conflict_copy(file_obj)
        assert conflict_path is not None
        assert "conflicted_copy" in conflict_path
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

    def test_save_conflict_copy_emits_ui_event(self, tmp_path):
        """_save_conflict_copy emits a conflict_resolved UI event with file and conflict_copy paths."""
        from unittest.mock import patch

        from hsync_common.protocol.events import UiEventName

        local_file = tmp_path / "conflict_target.txt"
        local_file.write_text("local version")

        file_obj = File.get(str(local_file))
        assert file_obj is not None

        with patch("hsync_client.sync.emit_event") as mock_emit:
            conflict_path = _save_conflict_copy(file_obj)
            assert conflict_path is not None
            mock_emit.assert_called_once_with(
                UiEventName.conflict_resolved,
                file=str(local_file),
                conflict_copy=conflict_path,
                winner="remote",
            )

    def test_save_conflict_copy_uses_utc_timestamp(self, tmp_path):
        """The conflicted copy filename uses ISO-8601 UTC, not local time."""
        import re
        from pathlib import Path

        local_file = tmp_path / "tz_test.bin"
        local_file.write_bytes(b"data")

        file_obj = File.get(str(local_file))
        assert file_obj is not None

        conflict_path = _save_conflict_copy(file_obj)
        assert conflict_path is not None
        # Pattern: <stem>_conflicted_copy_<YYYY-MM-DDTHHMMSSZ>_<cid>.<ext>
        name = Path(conflict_path).name
        assert re.match(
            r"tz_test_conflicted_copy_\d{8}T\d{6}Z_[a-zA-Z0-9\-_.]+\.bin",
            name,
        ), f"Filename {name!r} does not match the UTC ISO-8601 pattern"

    def test_preview_does_not_mutate_sync_state(self, tmp_path):
        """get_sync_actions(persist_state=False) must not mutate syncstate.db.

        A dry-run/preview computes the same causal decisions but must never
        persist them: the merge base recorded before the preview must be
        identical after it (ADR-0022 §3.4.7).
        """
        local_file = tmp_path / "kept.txt"
        local_file.write_text("synced content")
        file_obj = File.get(str(local_file))
        assert file_obj is not None

        cfg = {"LOCAL_BUCKET": str(tmp_path), "CID": "PreviewClient12345678"}

        # Record a base so the file is "in sync" — the path that previously
        # mutated the store via merge_and_record.
        with patch("hsync_client.state_backend.CONFIG", cfg):
            ss_mod.record_base(str(local_file), file_obj.sha256sum)
            ss_mod.flush()
            before = dict(ss_mod.get_sync_state_store()._data)
            ss_mod._stores.clear()

        remote_files = [
            {
                "filepath": "kept.txt",
                "name": "kept.txt",
                "path": "",
                "mtime": file_obj.mtime,
                "ctime": file_obj.ctime,
                "size": file_obj.size,
                "sha256sum": file_obj.sha256sum,
                "permissions": file_obj.permissions,
            }
        ]

        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            result = get_sync_actions(remote_files, [file_obj.to_dict()], persist_state=False)
            assert all(a.name == SyncActionName.nothing for a in result)
            after = dict(ss_mod.get_sync_state_store()._data)

        assert after == before
        assert not (tmp_path / ".hsyncstate").exists()


class TestSyncCycleFlag:
    """Unit tests for the sync-cycle coordination flag."""

    def test_flag_clear_when_idle(self):
        """The flag must be clear when no sync is running."""
        from hsync_client.sync import _sync_cycle_active, is_sync_cycle_active

        _sync_cycle_active.clear()
        assert is_sync_cycle_active() is False

    def test_flag_set_during_sync(self, tmp_path):
        """The flag is set while sync() is running and cleared on exit."""
        import asyncio
        from unittest.mock import patch

        from hsync_client.sync import _sync_cycle_active, is_sync_cycle_active, sync

        async def _check_flag_mid_sync():
            """Run sync() with a mocked scan that checks the flag while running."""
            captured = []

            def capturing_get_files_remote():
                captured.append(is_sync_cycle_active())
                return []

            with (
                patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
                patch("hsync_client.sync.get_files_local", return_value=[]),
                patch("hsync_client.sync.get_files_remote", side_effect=capturing_get_files_remote),
                patch("hsync_client.sync.get_tombstones_remote", return_value={}),
                patch("hsync_client.sync.push_tombstones_remote"),
                patch("hsync_client.sync.clear_sha256_cache"),
                patch("hsync_client.sync._cleanup_partial_files"),
                patch("hsync_client.sync.ensure_hsynckeep_files"),
                patch("hsync_client.sync.sync_state"),
            ):
                await sync()

            # The flag was True while sync was running
            assert any(captured), f"Flag was never set during sync: {captured}"
            # The flag is cleared after sync completes
            assert is_sync_cycle_active() is False

        _sync_cycle_active.clear()
        asyncio.run(_check_flag_mid_sync())

    def test_flag_cleared_on_sync_error(self):
        """The flag is cleared even if sync() raises an exception."""
        import asyncio
        from unittest.mock import patch

        from hsync_client.sync import _sync_cycle_active, is_sync_cycle_active, sync

        async def _check_flag_after_error():
            with (
                patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": "/tmp"}),
                patch("hsync_client.sync.get_files_local", return_value=[]),
                patch("hsync_client.sync.get_files_remote", side_effect=RuntimeError("boom")),
                patch("hsync_client.sync.clear_sha256_cache"),
                patch("hsync_client.sync._cleanup_partial_files"),
                patch("hsync_client.sync.ensure_hsynckeep_files"),
            ):
                with pytest.raises(RuntimeError, match="boom"):
                    await sync()
            # Flag must be cleared despite the error
            assert is_sync_cycle_active() is False

        _sync_cycle_active.clear()
        asyncio.run(_check_flag_after_error())


class TestTombstoneDecision:
    """Unit tests for the causal _tombstone_deletes_local rule (ADR-0003)."""

    def test_tombstone_dominates_deletes(self):
        """A tombstone clock dominating the local clock is positive proof the
        local copy is a stale ancestor: delete (wall clock never consulted)."""
        entry = {"vclock": {"A": 3, "B": 1}, "ts": 1, "sha256": "abc"}
        assert _tombstone_deletes_local(entry, {"A": 3}) is True

    def test_equal_clock_keeps(self):
        """An equal clock carries no descent proof: never delete on its say-so."""
        entry = {"vclock": {"A": 3}, "ts": 999999, "sha256": "abc"}
        assert _tombstone_deletes_local(entry, {"A": 3}) is False

    def test_local_concurrent_edit_keeps(self):
        """A local edit concurrent with the deletion is an intentional
        re-creation: keep/upload regardless of timestamps."""
        entry = {"vclock": {"A": 2}, "ts": 5000, "sha256": "abc"}
        assert _tombstone_deletes_local(entry, {"B": 2}) is False

    def test_local_dominates_keeps(self):
        """A local clock dominating the tombstone means the re-creation
        happened after the deletion: keep."""
        entry = {"vclock": {"A": 2}, "ts": 5000, "sha256": "abc"}
        assert _tombstone_deletes_local(entry, {"A": 3, "B": 1}) is False

    def test_missing_vclock_keeps(self):
        """A marker without a valid causal clock carries no proof of deletion
        ancestry — never delete local data on its say-so."""
        entry = {"ts": 999999, "sha256": "abc"}
        assert _tombstone_deletes_local(entry, None) is False

    def test_empty_local_clock_is_dominated(self):
        """A local file with no recorded history is an ancestor of any
        non-empty tombstone clock: delete."""
        entry = {"vclock": {"A": 1}, "ts": 1, "sha256": "abc"}
        assert _tombstone_deletes_local(entry, None) is True


class TestMassDeletionGuard:
    """Unit tests for the _mass_deletion_blocked safety guard."""

    _CONFIG = {
        "MASS_DELETE_GUARD": True,
        "MASS_DELETE_THRESHOLD": 50,
        "MASS_DELETE_PERCENT": 50,
    }

    def test_below_thresholds_allows(self):
        """A handful of deletions on a large bucket proceeds normally."""
        with patch("hsync_client.sync.CONFIG", self._CONFIG):
            blocked, reason = _mass_deletion_blocked(5, local_count=1000, remote_count=995)
        assert blocked is False
        assert reason == ""

    def test_absolute_threshold_trips(self):
        """Reaching the absolute deletion count trips the guard."""
        with patch("hsync_client.sync.CONFIG", self._CONFIG):
            blocked, reason = _mass_deletion_blocked(50, local_count=1000, remote_count=950)
        assert blocked is True
        assert "absolute threshold" in reason

    def test_percentage_threshold_trips(self):
        """Deleting at least the configured percentage of local files trips."""
        with patch("hsync_client.sync.CONFIG", self._CONFIG):
            blocked, reason = _mass_deletion_blocked(10, local_count=20, remote_count=10)
        assert blocked is True
        assert "%" in reason

    def test_empty_master_always_suspect(self):
        """An empty master trips the guard regardless of thresholds."""
        with patch("hsync_client.sync.CONFIG", self._CONFIG):
            blocked, reason = _mass_deletion_blocked(1, local_count=1, remote_count=0)
        assert blocked is True
        assert "empty bucket" in reason

    def test_no_deletions_allows(self):
        """A cycle with no deletions never trips, even with an empty master."""
        with patch("hsync_client.sync.CONFIG", self._CONFIG):
            blocked, _ = _mass_deletion_blocked(0, local_count=0, remote_count=0)
        assert blocked is False

    def test_disabled_guard_allows(self):
        """When the guard is disabled, even an empty master proceeds."""
        config = {**self._CONFIG, "MASS_DELETE_GUARD": False}
        with patch("hsync_client.sync.CONFIG", config):
            blocked, _ = _mass_deletion_blocked(100, local_count=100, remote_count=0)
        assert blocked is False


class TestHsyncignoreDoesNotDelete:
    """Regression: .hsyncignore must never mint offline-deletion tombstones.

    "Exclude protects, it does not delete" (rsync --exclude, Syncthing
    .stignore): a file hidden by .hsyncignore is still on disk, so the sync
    engine must not record it as deleted-while-offline — otherwise every peer
    would remove its copy through the gossiped tombstone.
    """

    def test_ignored_file_does_not_mint_tombstone(self, tmp_path):
        """A merge-base-tracked file hidden by .hsyncignore neither mints a
        tombstone nor loses its merge base through retain."""
        import asyncio
        from unittest.mock import patch

        from hsync_common.ignore import IgnoreMatcher, set_active_matcher

        from hsync_client.sync import sync

        # Track the file in the sync-state store: without the exclusion fix the
        # offline-deletion loop would treat it as deleted because the local
        # listing (filtered by the active matcher) no longer contains it.
        f = tmp_path / "keep.txt"
        f.write_text("content")
        cfg = {"LOCAL_BUCKET": str(tmp_path), "CID": "clientcid000000000000"}
        with patch("hsync_client.state_backend.CONFIG", cfg):
            file_obj = File.get(str(f))
            assert file_obj is not None
            ss_mod.record_base(str(f), file_obj.sha256sum)
            ss_mod.flush()
            ss_mod._stores.clear()

        # Install a real .hsyncignore matcher that hides the tracked file.
        (tmp_path / ".hsyncignore").write_text("*.txt\n")
        matcher = IgnoreMatcher(str(tmp_path))
        set_active_matcher(matcher)
        try:

            async def _run():
                with (
                    patch("hsync_client.sync.CONFIG", cfg),
                    patch("hsync_client.state_backend.CONFIG", cfg),
                    patch("hsync_client.state_backend.CONFIG", cfg),
                    patch("hsync_client.sync.get_files_remote", return_value=[]),
                    patch("hsync_client.sync.get_tombstones_remote", return_value={}),
                    patch("hsync_client.sync.get_dirs_remote", return_value=[]),
                    patch("hsync_client.sync.push_tombstones_remote") as mock_push,
                    patch("hsync_client.sync.get_sync_actions", return_value=[]),
                ):
                    await sync(lan_mode=True)
                    return mock_push

            mock_push = asyncio.run(_run())

            pushed_keys: set[str] = set()
            for call in mock_push.call_args_list:
                if call.args:
                    pushed_keys.update(call.args[0])

            # The hidden file must never mint an offline-deletion tombstone.
            assert tombstone_key("keep.txt") not in pushed_keys, (
                "a file hidden by .hsyncignore minted an offline-deletion tombstone"
            )

            # The deletion store for this peer also never saw the marker.
            with patch("hsync_client.state_backend.CONFIG", cfg):
                ts_mod._stores.clear()
                assert tombstone_key("keep.txt") not in ts_mod.get_payload()

            # The merge base survives retain: re-including the file later
            # reconciles with full causal history instead of a false conflict.
            with patch("hsync_client.state_backend.CONFIG", cfg):
                from hsync_client.sync_state import get_sync_state_store

                ss_mod._stores.clear()
                assert get_sync_state_store().base_hash("keep.txt") is not None
        finally:
            set_active_matcher(None)


class TestGetSyncActionsTombstones:
    """Tests for the tombstone-driven branch in get_sync_actions."""

    @staticmethod
    def _tomb_entry(sha256, vclock):
        """Build a causal tombstone entry for the solo-local tests."""
        return {
            "ts": 1,
            "sha256": sha256,
            "version_id": "v1",
            "origin_cid": _peer_cid("R"),
            "vclock": vclock,
        }

    def test_solo_local_deleted_when_tombstone_dominates(self, tmp_path):
        """A solo-local file whose local clock is causally dominated by a remote
        deletion marker is removed, not uploaded (ADR-0003) — never by timestamps."""
        local_file = tmp_path / "gone.txt"
        local_file.write_text("content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]
        cid_local = _peer_cid("L")
        tombstones = {tombstone_key("gone.txt"): self._tomb_entry(file.sha256sum, {cid_local: 2})}

        cfg = {"LOCAL_BUCKET": str(tmp_path), "CID": cid_local, "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            result = get_sync_actions([], local_files, tombstones=tombstones)
            assert len(result) == 1
            assert result[0].name == SyncActionName.delete

    def test_solo_local_uploaded_when_local_edit_concurrent(self, tmp_path):
        """A tombstone with a clock concurrent to the local edit never deletes:
        the local change is an intentional re-creation (upload)."""
        local_file = tmp_path / "gone.txt"
        local_file.write_text("content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]
        cid_local = _peer_cid("L")
        tombstones = {
            tombstone_key("gone.txt"): self._tomb_entry("deadbeef" * 8, {_peer_cid("R"): 1})
        }

        cfg = {"LOCAL_BUCKET": str(tmp_path), "CID": cid_local, "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            result = get_sync_actions([], local_files, tombstones=tombstones)
            assert len(result) == 1
            assert result[0].name == SyncActionName.upload

    def test_solo_local_uploaded_when_local_dominates(self, tmp_path):
        """A local clock dominating the tombstone means the re-creation happened
        after the deletion: keep/upload regardless of the deletion timestamp."""
        local_file = tmp_path / "gone.txt"
        local_file.write_text("content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]
        cid_local = _peer_cid("L")
        # Record a base so the local effective clock advances past the tombstone.
        cfg = {"LOCAL_BUCKET": str(tmp_path), "CID": cid_local, "SYNC_DIRECTION": "bidirectional"}
        with patch("hsync_client.state_backend.CONFIG", cfg):
            ss_mod.record_base(str(local_file), file.sha256sum or "", vclock={cid_local: 1})
            ss_mod.flush()
            ss_mod._stores.clear()
        tombstones = {tombstone_key("gone.txt"): self._tomb_entry(file.sha256sum, {cid_local: 1})}

        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
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

    def test_tombstone_delete_is_confirmed(self, tmp_path):
        """A causally-confirmed deletion carries ``tombstone_confirmed=True``.

        Regression test: the mass-deletion guard exempts tombstone-confirmed
        deletions from its empty-bucket block, so a solo-local file deleted
        elsewhere while this peer was offline is actually removed instead of
        being stranded by a false "suspect empty master" guard trip. The flag
        is the contract between ``get_sync_actions`` and the guard.
        """
        local_file = tmp_path / "gone.txt"
        local_file.write_text("content")

        file = File.get(str(local_file))
        assert file is not None
        local_files = [file.to_dict()]
        cid_local = _peer_cid("L")
        tombstones = {tombstone_key("gone.txt"): self._tomb_entry(file.sha256sum, {cid_local: 2})}

        cfg = {"LOCAL_BUCKET": str(tmp_path), "CID": cid_local, "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            result = get_sync_actions([], local_files, tombstones=tombstones)
            assert len(result) == 1
            assert result[0].name == SyncActionName.delete
            assert result[0].tombstone_confirmed is True


class TestGetSyncActionsRemoteOnlyTombstones:
    """Tests for the remote-only tombstone branch in get_sync_actions.

    A file that exists only on the remote may be the stale copy of a file
    this peer deleted while the master was unreachable (or between sync
    cycles). The download (resurrection) is suppressed only on positive
    causal proof: the tombstone clock dominating the remote advertised
    version (ADR-0003). Content equality is not a delete proof — an
    identical-content re-creation (an empty file being the trivial case) is
    an intentional re-creation and is downloaded (add-wins semantics).
    """

    @staticmethod
    def _remote_entry(sha256: str, vclock: dict, size: int | None = None):
        """Build a remote-file listing entry advertising a causal version.

        ``size`` defaults to ``len(sha256)`` — a placeholder the tombstone
        decision never reads. Pass an explicit ``size`` (e.g. ``0``) for
        fixtures that must represent a specific file accurately.
        """
        return {
            "filepath": "gone.txt",
            "sha256sum": sha256,
            "mtime": 1,
            "ctime": 1,
            "size": len(sha256) if size is None else size,
            "permissions": "644",
            "version": {
                "vclock": vclock,
                "version_id": "v1",
                "origin_cid": _peer_cid("R"),
                "ts": 1,
                "corrected_mtime": 0,
            },
        }

    @staticmethod
    def _tomb_entry(sha256, vclock):
        return {
            "ts": 1,
            "sha256": sha256,
            "version_id": "v1",
            "origin_cid": _peer_cid("R"),
            "vclock": vclock,
        }

    def test_remote_only_content_match_concurrent_downloads(self, tmp_path):
        """A tombstone whose content hash equals the remote copy but whose clock
        is concurrent with it is a delete-vs-recreate divergence, not a stale
        ancestor: the remote copy is downloaded, never deleted (regression:
        identical content re-created after an offline delete must survive)."""
        remote_hash = _sha("remote-content")
        cid_local = _peer_cid("L")
        # Concurrent clocks + identical content: content equality is not a
        # delete proof, so the intentional re-creation is downloaded.
        tombstones = {tombstone_key("gone.txt"): self._tomb_entry(remote_hash, {cid_local: 1})}
        remote_files = [self._remote_entry(remote_hash, {_peer_cid("R"): 1})]

        cfg = {"LOCAL_BUCKET": str(tmp_path), "CID": cid_local, "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            result = get_sync_actions(remote_files, [], tombstones=tombstones)

        assert len(result) == 1
        assert result[0].name == SyncActionName.download

    def test_remote_only_empty_file_recreation_downloads(self, tmp_path):
        """An empty file re-created after an offline delete is downloaded, not
        deleted: every empty file shares the same SHA-256, so a content match
        alone must never justify deleting the remote copy."""
        empty_sha = _sha("")
        cid_local = _peer_cid("L")
        tombstones = {tombstone_key("gone.txt"): self._tomb_entry(empty_sha, {cid_local: 1})}
        remote_files = [self._remote_entry(empty_sha, {_peer_cid("R"): 1}, size=0)]

        cfg = {"LOCAL_BUCKET": str(tmp_path), "CID": cid_local, "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            result = get_sync_actions(remote_files, [], tombstones=tombstones)

        assert len(result) == 1
        assert result[0].name == SyncActionName.download

    def test_remote_only_causal_dominance_deletes_remote(self, tmp_path):
        """A tombstone causally dominating the remote advertised version is
        positive proof the remote copy is a stale ancestor: delete it
        remotely even when the marker has no content hash (regression:
        deletion while the master was unreachable)."""
        remote_hash = _sha("remote-content")
        cid_local = _peer_cid("L")
        cid_remote = _peer_cid("R")
        # sha256=None: only the causal proof can justify the deletion.
        tombstones = {tombstone_key("gone.txt"): self._tomb_entry(None, {cid_remote: 2})}
        remote_files = [self._remote_entry(remote_hash, {cid_remote: 1})]

        cfg = {"LOCAL_BUCKET": str(tmp_path), "CID": cid_local, "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            result = get_sync_actions(remote_files, [], tombstones=tombstones)

        assert len(result) == 1
        assert result[0].name == SyncActionName.delete_remote

    def test_remote_only_concurrent_tombstone_downloads(self, tmp_path):
        """A tombstone concurrent with the remote edit is a delete-vs-edit
        divergence, not a stale ancestor: the remote copy is downloaded."""
        remote_hash = _sha("remote-content")
        cid_local = _peer_cid("L")
        cid_remote = _peer_cid("R")
        tombstones = {tombstone_key("gone.txt"): self._tomb_entry(None, {cid_local: 1})}
        remote_files = [self._remote_entry(remote_hash, {cid_remote: 2})]

        cfg = {"LOCAL_BUCKET": str(tmp_path), "CID": cid_local, "SYNC_DIRECTION": "bidirectional"}
        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            result = get_sync_actions(remote_files, [], tombstones=tombstones)

        assert len(result) == 1
        assert result[0].name == SyncActionName.download

    def test_remote_only_without_tombstone_downloads(self, tmp_path):
        """With no matching tombstone the remote-only file is downloaded as before."""
        remote_hash = _sha("remote-content")
        remote_files = [self._remote_entry(remote_hash, {_peer_cid("R"): 1})]

        cfg = {
            "LOCAL_BUCKET": str(tmp_path),
            "CID": _peer_cid("L"),
            "SYNC_DIRECTION": "bidirectional",
        }
        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
        ):
            result = get_sync_actions(remote_files, [], tombstones={})

        assert len(result) == 1
        assert result[0].name == SyncActionName.download


class TestClientTombstonesModule:
    """Tests for the per-peer tombstone store (hsync_client.tombstones)."""

    def test_record_then_payload_uses_hashed_key(self, tmp_path):
        """A recorded causal deletion appears in the payload under its hashed key."""
        ts = int(time.time())
        cid = _peer_cid("L")
        with patch(
            "hsync_client.state_backend.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "CID": cid},
        ):
            ts_mod.record_deletion(
                str(tmp_path / "gone.txt"),
                sha256sum="a" * 64,
                origin_cid=cid,
                prev_vclock={_peer_cid("R"): 1},
                timestamp=ts,
            )

            payload = ts_mod.get_payload()
            key = tombstone_key("gone.txt")
            assert key in payload
            entry = payload[key]
            assert entry["ts"] == ts
            assert entry["sha256"] == "a" * 64
            assert entry["origin_cid"] == cid
            assert entry["vclock"] == {_peer_cid("R"): 1, cid: 1}

    def test_record_persists_and_reloads(self, tmp_path):
        """The marker survives a cache reset by being read back from the side-car."""
        ts = int(time.time())
        with patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ts_mod.record_deletion(str(tmp_path / "gone.txt"), sha256sum="a" * 64, timestamp=ts)
            # Drop the in-memory cache so the next access loads from disk.
            ts_mod._stores.clear()
            assert tombstone_key("gone.txt") in ts_mod.get_payload()

    def test_record_by_key_batched_flush_persists(self, tmp_path):
        """``persist=False`` + one ``flush`` persists the markers exactly once."""
        key = tombstone_key("gone.txt")
        with patch(
            "hsync_client.state_backend.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "CID": _peer_cid("L")},
        ):
            ts_mod.record_deletion_by_key(key, origin_cid=_peer_cid("L"), persist=False)
            # Deferred: a fresh store (cache cleared) must not see it yet.
            ts_mod._stores.clear()
            assert key not in ts_mod.get_payload()
            # Record + flush again, then the marker is durable across reload.
            ts_mod.record_deletion_by_key(key, origin_cid=_peer_cid("L"), persist=False)
            ts_mod.flush()
            ts_mod._stores.clear()
            assert key in ts_mod.get_payload()

    def test_clear_removes_marker(self, tmp_path):
        """clear_deletion drops a previously recorded marker."""
        with patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ts_mod.record_deletion(str(tmp_path / "gone.txt"), sha256sum="abc", timestamp=1000)
            ts_mod.clear_deletion(str(tmp_path / "gone.txt"))

            assert ts_mod.get_payload() == {}

    def test_merge_payload_causal(self, tmp_path):
        """merge_payload adopts causally dominant markers and reports the change count."""
        now = int(time.time())
        cid_a = _peer_cid("A")
        cid_b = _peer_cid("B")
        with patch(
            "hsync_client.state_backend.CONFIG",
            {"LOCAL_BUCKET": str(tmp_path), "CID": cid_a},
        ):
            ts_mod.record_deletion(
                str(tmp_path / "a.txt"), sha256sum="a" * 64, origin_cid=cid_a, timestamp=now
            )

            key_a = tombstone_key("a.txt")
            key_b = tombstone_key("b.txt")
            remote = {
                # Dominates the local {A:1}: adopted even with an OLDER ts.
                key_a: {
                    "ts": now - 60,
                    "sha256": "b" * 64,
                    "version_id": "v1",
                    "origin_cid": cid_a,
                    "vclock": {cid_a: 2},
                },
                # New path: adopted.
                key_b: {
                    "ts": now - 1000,
                    "sha256": "c" * 64,
                    "version_id": "v1",
                    "origin_cid": cid_b,
                    "vclock": {cid_b: 1},
                },
            }
            changed = ts_mod.merge_payload(remote)

            payload = ts_mod.get_payload()
            assert changed == 2
            assert payload[key_a]["sha256"] == "b" * 64
            assert payload[key_b]["sha256"] == "c" * 64
            # A stale (dominated) marker is kept out even with a newer ts.
            stale = {
                key_a: {
                    "ts": now + 60,
                    "sha256": "d" * 64,
                    "version_id": "v1",
                    "origin_cid": cid_a,
                    "vclock": {cid_a: 1},
                }
            }
            assert ts_mod.merge_payload(stale) == 0
            assert ts_mod.get_payload()[key_a]["sha256"] == "b" * 64

    def test_record_outside_bucket_is_noop(self, tmp_path):
        """A path that does not resolve inside the bucket records nothing."""
        with patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            # The bucket root itself has an empty relative path.
            ts_mod.record_deletion(str(tmp_path), sha256sum="abc", timestamp=1000)

            assert ts_mod.get_payload() == {}


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
            patch("hsync_client.sync.get_ssl_verify", return_value=True),
            patch("hsync_client.sync.post") as mock_post,
        ):
            push_tombstones_remote(payload)

            assert mock_post.call_count == 1
            args, kwargs = mock_post.call_args
            assert args[0] == "https://master/api/v1/buckets/remote/tombstones"
            assert kwargs["json"] == payload

    def test_request_exception_is_swallowed(self):
        """A transport error never propagates out of the best-effort push."""
        from requests import RequestException

        payload = {tombstone_key("a.txt"): {"ts": 1, "sha256": "a"}}
        with (
            patch("hsync_client.sync.CONFIG", self._CONFIG),
            patch("hsync_client.sync.get_ssl_verify", return_value=True),
            patch("hsync_client.sync.post", side_effect=RequestException("boom")),
        ):
            # Must not raise.
            push_tombstones_remote(payload)


def _sha(text: str) -> str:
    """SHA-256 hex digest of a short text payload (test helper)."""
    return hashlib.sha256(text.encode()).hexdigest()


def _ordered_texts(a: str, b: str) -> tuple[str, str]:
    """Return ``(lower_hash_text, higher_hash_text)`` for two payloads."""
    return (a, b) if _sha(a) < _sha(b) else (b, a)


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
            "permissions": "644",
        }

    def _run(
        self,
        tmp_path,
        *,
        local_text,
        remote_sha,
        base_hash,
        remote_vclock=None,
        local_corrected_mtime=None,
        remote_corrected_mtime=None,
    ):
        """Drive get_sync_actions with equal mtimes so only the base/vclock decides.

        ``local_corrected_mtime``/``remote_corrected_mtime`` inject the ADR-0023
        corrected values (local via the sync_state helper, remote via the
        advertised ``version`` payload) so the chronological tie-break can be
        exercised deterministically without touching the real clock.
        """
        local_file = tmp_path / "f.txt"
        local_file.write_text(local_text)
        lf = File.get(str(local_file))
        assert lf is not None
        remote = self._remote("f.txt", remote_sha, lf.mtime or 0)
        version: dict = {}
        if remote_vclock is not None:
            version["vclock"] = remote_vclock
            version["version_id"] = "vid"
            version["origin_cid"] = "TestClientB1234567890"
            version["ts"] = 0
        if remote_corrected_mtime is not None:
            version["corrected_mtime"] = remote_corrected_mtime
        if version:
            remote["version"] = version
        patches = [
            patch(
                "hsync_client.sync.CONFIG",
                {"LOCAL_BUCKET": str(tmp_path), "CID": "TestClientA1234567890"},
            ),
            patch(
                "hsync_client.state_backend.CONFIG",
                {"LOCAL_BUCKET": str(tmp_path), "CID": "TestClientA1234567890"},
            ),
        ]
        if local_corrected_mtime is not None:
            patches.append(
                patch(
                    "hsync_client.sync_state._corrected_mtime_of",
                    return_value=local_corrected_mtime,
                )
            )
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            if base_hash is not None:
                ss_mod.record_base(str(local_file), base_hash)
            result = get_sync_actions([remote], [lf.to_dict()], tombstones={})
            base_after = ss_mod.get_base_hash(str(local_file))

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
        """Both sides diverged from base with concurrent clocks: tie-break conflict.

        The winner is decided by highest SHA-256; the loser is preserved. With
        concurrent vclocks (neither dominates) this is always a conflict_*.
        """
        _, result, _ = self._run(
            tmp_path,
            local_text="LOCAL",
            remote_sha=_sha("REMOTE"),
            base_hash=_sha("BASE"),
            remote_vclock={"TestClientB1234567890": 1},
        )
        assert len(result) == 1
        # Tie-break by hash: if local hash > remote hash -> upload; else download+conflict.
        local_hash = _sha("LOCAL")
        remote_hash = _sha("REMOTE")
        if local_hash > remote_hash:
            assert result[0].name == SyncActionName.upload
        else:
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
            patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
        ):
            result = get_sync_actions([remote], [lf.to_dict()], tombstones={})
            base_after = ss_mod.get_base_hash(str(local_file))

        assert len(result) == 1
        assert result[0].name == SyncActionName.nothing
        assert base_after == lf.sha256sum

    def test_no_base_no_clocks_different_content_is_tie_break(self, tmp_path):
        """With no recorded base and no clocks, differing content is a tie-break
        (replaces the old legacy mtime tie). The winner is decided by hash."""
        _, result, _ = self._run(
            tmp_path, local_text="LOCAL", remote_sha=_sha("REMOTE"), base_hash=None
        )
        assert len(result) == 1
        # Tie-break by hash: whichever content has the higher SHA-256 wins.
        local_hash = _sha("LOCAL")
        remote_hash = _sha("REMOTE")
        if local_hash > remote_hash:
            assert result[0].name == SyncActionName.upload
        else:
            assert result[0].name == SyncActionName.download
            assert getattr(result[0], "conflict", False) is True

    def test_corrected_mtime_local_newer_wins_despite_lower_hash(self, tmp_path):
        """ADR-0023: a newer corrected mtime beats the hash fallback.

        The local content is chosen so its SHA-256 sorts LOWER than the
        remote's (the hash fallback alone would make the remote win), yet the
        local side wins the original path because its corrected mtime is the
        highest — the matrix never consults the raw wall clock.
        """
        low_text, high_text = _ordered_texts("alpha", "omega")
        _, result, _ = self._run(
            tmp_path,
            local_text=low_text,
            remote_sha=_sha(high_text),
            base_hash=_sha("BASE"),
            remote_vclock={"TestClientB1234567890": 1},
            local_corrected_mtime=200,
            remote_corrected_mtime=100,
        )
        assert len(result) == 1
        assert result[0].name == SyncActionName.upload
        assert getattr(result[0], "conflict", False) is False

    def test_corrected_mtime_remote_newer_wins_despite_lower_hash(self, tmp_path):
        """ADR-0023: the newer remote corrected mtime wins the path (conflict copy).

        The remote content's hash sorts LOWER, so the hash fallback alone
        would make the local side win; the newer remote corrected mtime
        overrides it.
        """
        low_text, high_text = _ordered_texts("alpha", "omega")
        _, result, _ = self._run(
            tmp_path,
            local_text=high_text,
            remote_sha=_sha(low_text),
            base_hash=_sha("BASE"),
            remote_vclock={"TestClientB1234567890": 1},
            local_corrected_mtime=100,
            remote_corrected_mtime=200,
        )
        assert len(result) == 1
        assert result[0].name == SyncActionName.download
        assert getattr(result[0], "conflict", False) is True

    def test_missing_local_corrected_mtime_falls_back_to_hash(self, tmp_path):
        """One side without a corrected mtime degrades to the hash fallback."""
        low_text, high_text = _ordered_texts("alpha", "omega")
        _, result, _ = self._run(
            tmp_path,
            local_text=high_text,  # higher hash ⇒ hash fallback would upload
            remote_sha=_sha(low_text),
            base_hash=_sha("BASE"),
            remote_vclock={"TestClientB1234567890": 1},
            local_corrected_mtime=0,  # known-zero on the wire ⇒ fallback
            remote_corrected_mtime=200,
        )
        assert len(result) == 1
        # The remote corrected mtime is known but the local one is not: the
        # deterministic hash fallback decides, never the partial mtime.
        assert result[0].name == SyncActionName.upload
        assert getattr(result[0], "conflict", False) is False


class TestClientSyncStateModule:
    """Tests for the per-peer sync-state store (hsync_client.sync_state)."""

    def test_record_then_get_base_hash(self, tmp_path):
        with patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ss_mod.record_base(str(tmp_path / "a.txt"), "hh")
            assert ss_mod.get_base_hash(str(tmp_path / "a.txt")) == "hh"

    def test_record_empty_hash_is_noop(self, tmp_path):
        with patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ss_mod.record_base(str(tmp_path / "a.txt"), "")
            assert ss_mod.get_base_hash(str(tmp_path / "a.txt")) is None

    def test_record_outside_bucket_is_noop(self, tmp_path):
        with patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ss_mod.record_base(str(tmp_path), "hh")  # bucket root -> empty relpath
            assert ss_mod.get_base_hash(str(tmp_path)) is None

    def test_clear_base_removes_entry(self, tmp_path):
        with patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ss_mod.record_base(str(tmp_path / "a.txt"), "hh")
            ss_mod.clear_base(str(tmp_path / "a.txt"))
            assert ss_mod.get_base_hash(str(tmp_path / "a.txt")) is None

    def test_retain_drops_absent_paths(self, tmp_path):
        with patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ss_mod.record_base(str(tmp_path / "keep.txt"), "h1")
            ss_mod.record_base(str(tmp_path / "drop.txt"), "h2")
            dropped = ss_mod.retain([str(tmp_path / "keep.txt")])
            assert dropped == 1
            assert ss_mod.get_base_hash(str(tmp_path / "keep.txt")) == "h1"
            assert ss_mod.get_base_hash(str(tmp_path / "drop.txt")) is None

    def test_flush_persists_and_reloads(self, tmp_path):
        with patch("hsync_client.state_backend.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            ss_mod.record_base(str(tmp_path / "a.txt"), "hh")
            ss_mod.flush()
            # Drop the in-memory cache so the next access loads from the side-car.
            ss_mod._stores.clear()
            assert ss_mod.get_base_hash(str(tmp_path / "a.txt")) == "hh"


# Fixed mtime (unix seconds) shared by every peer and the server below, so the
# end-to-end merge can only be driven by content hashes: this proves the
# resolution is clock-independent rather than relying on timestamps.
_E2E_MTIME = 1_000_000


def _peer_cid(letter: str) -> str:
    """Return a valid 21-char cid derived from a peer letter (is_valid_cid)."""
    return f"Peer{letter}1234567890ABCDEF"


class TestThreeWayMergeEndToEnd:
    """Full two-peer round-trip exercising the lifecycle on real on-disk state.

    A shared in-memory ``server`` stands in for the master; two peers each own a
    bucket directory and a persistent ``syncstate.db`` row. Every round runs
    the real :func:`get_sync_actions` and then applies the decided transfers
    exactly as the sync loop does (writing content and recording the synced
    base), flushing and dropping the in-memory cache between rounds to simulate a
    process restart. All mtimes are pinned to ``_E2E_MTIME`` so only the recorded
    base can decide the direction.
    """

    @staticmethod
    def _touch(path, text: str) -> str:
        """Write *text* at *path* with the pinned mtime; return its sha256."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.utime(path, (_E2E_MTIME, _E2E_MTIME))
        return _sha(text)

    @staticmethod
    def _remote_list(server: dict) -> list[dict]:
        """Render the shared server state as a list-files payload.

        Each entry advertises the registered causal version, mirroring the
        real ``version`` JOIN the server/master attaches to listings
        (ADR-0022 §4.2). Unknown "version" keys are ignored by old-style
        callers, so this stays wire-faithful.
        """
        return [
            {
                "filepath": rel,
                "name": rel,
                "sha256sum": entry["sha"],
                "mtime": _E2E_MTIME,
                "ctime": _E2E_MTIME,
                "size": len(entry["content"]),
                "permissions": "644",
                "version": {
                    "vclock": entry.get("vclock", {}),
                    "version_id": "",
                    "origin_cid": entry.get("origin_cid"),
                    "ts": 0,
                },
            }
            for rel, entry in server.items()
        ]

    @staticmethod
    def _apply(action, bucket, server: dict) -> None:
        """Mirror the sync loop's side effects for one decided action.

        Clocks follow the real convergence rules: an upload registers
        ``merge_vclocks(incoming_vclock, stored_vclock)`` on the server (D11),
        a download records
        ``merge_and_record`` locally, and a conflicting download first
        preserves the losing bytes via ``_save_conflict_copy`` (exactly as
        ``sync()`` does before the overwrite).
        """
        # Uploads carry an absolute local path; downloads carry the bucket-
        # relative remote path. Normalize both to an absolute path in the bucket.
        fp = action.file.filepath or ""
        abs_path = fp if os.path.isabs(fp) else str(bucket / fp)
        rel = os.path.relpath(abs_path, str(bucket))
        if action.name == SyncActionName.upload:
            content = (bucket / rel).read_text()
            sha = _sha(content)
            eff = ss_mod.local_version_for_compare(abs_path, sha)
            incoming_vclock = eff["vclock"] if eff else {}
            stored_vclock = server[rel]["vclock"] if rel in server else {}
            merged = merge_vclocks(incoming_vclock, stored_vclock)
            server[rel] = {"content": content, "sha": sha, "vclock": merged}
            ss_mod.record_base(abs_path, sha, vclock=merged)
        elif action.name == SyncActionName.download:
            # On a conflict the real loop first saves a conflict copy of the
            # local file; mirror that so the loser's content is preserved on
            # disk and later propagates as a brand-new file.
            entry = server[rel]
            if getattr(action, "conflict", False):
                _save_conflict_copy(action.file)
            (bucket / rel).parent.mkdir(parents=True, exist_ok=True)
            (bucket / rel).write_text(entry["content"])
            os.utime(bucket / rel, (_E2E_MTIME, _E2E_MTIME))
            ss_mod.merge_and_record(abs_path, entry["sha"], entry.get("vclock"))
        # SyncActionName.nothing: get_sync_actions already refreshed the base.

    def _round(self, bucket, server: dict, cid: str | None = None) -> list:
        """Run one full sync round for the peer rooted at *bucket*."""
        cfg = {"LOCAL_BUCKET": str(bucket)}
        if cid:
            cfg["CID"] = cid
        with (
            patch("hsync_client.sync.CONFIG", cfg),
            patch("hsync_client.state_backend.CONFIG", cfg),
            # _save_conflict_copy resolves relative paths via transfer._local_dest_path,
            # which reads its own CONFIG.
            patch("hsync_client.transfer.CONFIG", cfg),
        ):
            # fresh process: base is loaded from syncstate.db
            clear_sha256_cache()  # mtimes are pinned, so re-hash content each round
            actions = get_sync_actions(self._remote_list(server), get_files_local(), tombstones={})
            for action in actions:
                self._apply(action, bucket, server)
            ss_mod.flush()

        return actions

    @staticmethod
    def _only(actions: list, name):
        """Return the single action with *name* (assert exactly one)."""
        matching = [a for a in actions if a.name == name]
        assert len(matching) == 1, f"expected one {name}, got {[a.name for a in actions]}"
        return matching[0]

    def test_two_peer_lifecycle_is_clock_independent(self, tmp_path):
        """First sync, clean one-sided edits, a true conflict, then convergence."""
        peer_a = tmp_path / "peerA"
        peer_b = tmp_path / "peerB"
        peer_a.mkdir()
        peer_b.mkdir()
        server: dict = {}

        # Round 1 - A authors doc.txt and uploads it; the server starts empty.
        sha_v1 = self._touch(peer_a / "doc.txt", "v1")
        actions = self._round(peer_a, server)
        self._only(actions, SyncActionName.upload)
        assert server["doc.txt"]["sha"] == sha_v1

        # Round 1 - B has nothing locally yet: a clean download (no conflict).
        actions = self._round(peer_b, server)
        assert self._only(actions, SyncActionName.download).conflict is False
        assert (peer_b / "doc.txt").read_text() == "v1"

        # Round 2 - A edits only locally: remote still matches A's base -> upload.
        sha_v2 = self._touch(peer_a / "doc.txt", "v2")
        actions = self._round(peer_a, server)
        self._only(actions, SyncActionName.upload)
        assert server["doc.txt"]["sha"] == sha_v2

        # Round 2 - B untouched since base: clean download of v2 (no conflict).
        actions = self._round(peer_b, server)
        assert self._only(actions, SyncActionName.download).conflict is False
        assert (peer_b / "doc.txt").read_text() == "v2"

        # Round 3 - concurrent divergence from the shared base v2. The winner is
        # decided deterministically by highest SHA-256 (ADR-0022 D3), so pick the
        # two contents and compute which one wins up front.
        content_a, content_b = sorted(("vA", "vB"), key=_sha, reverse=True)
        # (reverse sort ⇒ content_a has the higher hash, i.e. would win a tie-break)
        self._touch(peer_a / "doc.txt", content_a)
        self._touch(peer_b / "doc.txt", content_b)
        # A wins the race and uploads first.
        actions = self._round(peer_a, server)
        self._only(actions, SyncActionName.upload)
        assert server["doc.txt"]["sha"] == _sha(content_a)
        # B now sees both sides changed since base v2. A's content has the
        # higher hash, so B loses the tie-break: a conflicting download (the
        # local copy is preserved by the real loop), then convergence to A's
        # content. Every mtime is equal, so only the recorded base plus the
        # hash tie-break can produce this verdict.
        actions = self._round(peer_b, server)
        assert self._only(actions, SyncActionName.download).conflict is True
        assert (peer_b / "doc.txt").read_text() == content_a

        # Round 4 - both peers now agree on A's content. Only the conflict copy
        # B preserved may still propagate as a brand-new file.
        for peer in (peer_a, peer_b):
            doc_actions = [
                a
                for a in self._round(peer, server)
                if "conflicted_copy" not in (a.file.filepath or "")
            ]
            assert all(a.name == SyncActionName.nothing for a in doc_actions), [
                (a.name, a.file.filepath) for a in doc_actions
            ]
            assert (peer / "doc.txt").read_text() == content_a

    def test_relay_a_b_c_no_false_conflict(self, tmp_path):
        """Relay A→B→C: A's full causal history reaches C through B.

        C is offline while A authors v1/v2 and B builds vB on top of them;
        when C rejoins it sees only the final aggregated state. The clock
        (A's counters included, arrived via B's upload) makes the download
        silent on every leg — no tie-break, no conflict copy (piano §6).
        """
        peer_a = tmp_path / "peerA"
        peer_b = tmp_path / "peerB"
        peer_c = tmp_path / "peerC"
        for peer in (peer_a, peer_b, peer_c):
            peer.mkdir()
        server: dict = {}
        cid_a, cid_b, cid_c = _peer_cid("A"), _peer_cid("B"), _peer_cid("C")

        # A authors v1; B and C download it cleanly (shared base, clock {A:1}).
        self._touch(peer_a / "doc.txt", "v1")
        self._only(self._round(peer_a, server, cid=cid_a), SyncActionName.upload)
        assert server["doc.txt"]["vclock"] == {cid_a: 1}
        for peer, cid in ((peer_b, cid_b), (peer_c, cid_c)):
            actions = self._round(peer, server, cid=cid)
            assert self._only(actions, SyncActionName.download).conflict is False

        # A edits to v2 and syncs; B and C catch up to the new base.
        self._touch(peer_a / "doc.txt", "v2")
        self._round(peer_a, server, cid=cid_a)
        assert server["doc.txt"]["vclock"] == {cid_a: 2}
        self._round(peer_b, server, cid=cid_b)
        self._round(peer_c, server, cid=cid_c)

        # B (now offline, on the v2 base) builds vB on top and uploads: the
        # server holds A's and B's counters merged ({A:2, B:1}).
        self._touch(peer_b / "doc.txt", "vB")
        self._round(peer_b, server, cid=cid_b)
        assert server["doc.txt"]["vclock"] == {cid_a: 2, cid_b: 1}

        # C rejoins: it only ever sees the final aggregated state (it went
        # offline after downloading v2) and never talked to B directly, yet
        # the download is silent (no false conflict).
        actions = self._round(peer_c, server, cid=cid_c)
        assert self._only(actions, SyncActionName.download).conflict is False
        assert (peer_c / "doc.txt").read_text() == "vB"

        # A rejoins: still at its clean base v2, so vB arrives silently too.
        actions = self._round(peer_a, server, cid=cid_a)
        assert self._only(actions, SyncActionName.download).conflict is False
        assert (peer_a / "doc.txt").read_text() == "vB"

        # Full three-peer convergence to vB.
        for peer, cid in ((peer_a, cid_a), (peer_b, cid_b), (peer_c, cid_c)):
            actions = self._round(peer, server, cid=cid)
            assert all(a.name == SyncActionName.nothing for a in actions), [
                (a.name, a.file.filepath) for a in actions
            ]

    def test_offline_partition_tie_break_preserves_loser(self, tmp_path):
        """Offline partition: both peers edit; the deterministic SHA-256
        tie-break elects the same winner on every node and the loser's bytes
        are preserved as a conflict copy (piano §6)."""
        peer_a = tmp_path / "peerA"
        peer_b = tmp_path / "peerB"
        peer_c = tmp_path / "peerC"
        for peer in (peer_a, peer_b, peer_c):
            peer.mkdir()
        server: dict = {}
        cid_a, cid_b, cid_c = _peer_cid("A"), _peer_cid("B"), _peer_cid("C")

        # Common base: A authors v1; everyone downloads it ({A:1} everywhere).
        self._touch(peer_a / "doc.txt", "v1")
        self._round(peer_a, server, cid=cid_a)
        for peer, cid in ((peer_b, cid_b), (peer_c, cid_c)):
            actions = self._round(peer, server, cid=cid)
            assert self._only(actions, SyncActionName.download).conflict is False

        # Decide the deterministic winner up front (highest SHA-256 wins,
        # ADR-0022 D3) so assertions are stable regardless of content.
        win_text, lose_text = sorted(
            ("offline-winner", "offline-loser"),
            key=_sha,
            reverse=True,
        )  # reverse ⇒ win_text has the higher hash (wins the tie-break)

        # Offline partition: A and B diverge from the shared base without syncing.
        self._touch(peer_a / "doc.txt", win_text)
        self._touch(peer_b / "doc.txt", lose_text)

        # A rejoins first: the remote still matches A's base → clean upload.
        actions = self._round(peer_a, server, cid=cid_a)
        self._only(actions, SyncActionName.upload)
        assert server["doc.txt"]["sha"] == _sha(win_text)

        # B rejoins: true divergence. The tie-break elects A's higher hash;
        # B's losing bytes are preserved as a conflict copy (mirroring the
        # real loop) and B converges to the winner. The preserved copy is
        # also uploaded here as a brand-new file (propagation model).
        actions = self._round(peer_b, server, cid=cid_b)
        doc_actions = [a for a in actions if "conflicted_copy" not in (a.file.filepath or "")]
        assert self._only(doc_actions, SyncActionName.download).conflict is True
        assert (peer_b / "doc.txt").read_text() == win_text
        copies = [p for p in peer_b.iterdir() if "conflicted_copy" in p.name]
        assert len(copies) == 1
        assert copies[0].read_text() == lose_text

        # Post-conflict convergence: C (never involved) downloads the winner
        # silently — the merged clock dominates its stored {A:1}.
        actions = self._round(peer_c, server, cid=cid_c)
        assert all(getattr(a, "conflict", False) is False for a in actions)
        assert (peer_c / "doc.txt").read_text() == win_text

        # No node ever re-flags doc.txt as a conflict: only the preserved
        # copy propagates as a new file.
        for peer, cid in ((peer_a, cid_a), (peer_b, cid_b), (peer_c, cid_c)):
            doc_actions = [
                a
                for a in self._round(peer, server, cid=cid)
                if "conflicted_copy" not in (a.file.filepath or "")
            ]
            assert all(a.name == SyncActionName.nothing for a in doc_actions), [
                (a.name, a.file.filepath) for a in doc_actions
            ]
