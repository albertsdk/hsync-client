"""Tests for folder-ID lifecycle and the client SQLite state wrappers."""

import shutil
from pathlib import Path
from unittest.mock import patch

from hsync_common.constants import (
    HSYNCFOLDER_FILENAME,
    HSYNCSTATE_FILENAME,
    HSYNCTOMBSTONES_FILENAME,
)
from hsync_common.models.folder import read_folder_marker
from hsync_common.path_validation import is_internal_name

from hsync_client import sync_state, tombstones
from hsync_client.state_backend import resolve_bucket_folder


def _cfg(bucket: Path) -> dict[str, str]:
    return {"LOCAL_BUCKET": str(bucket), "CID": "ClientState12345678901"}


class TestBucketContainsOnlyMarker:
    def test_flush_does_not_write_legacy_sidecars(self, tmp_path):
        bucket = tmp_path / "bucket"
        bucket.mkdir()
        (bucket / "note.txt").write_text("hi")
        with patch("hsync_client.state_backend.CONFIG", _cfg(bucket)):
            sync_state.record_base(str(bucket / "note.txt"), "ab" * 32)
            sync_state.flush()
        with patch("hsync_client.state_backend.CONFIG", _cfg(bucket)):
            tombstones.record_deletion(str(bucket / "gone.txt"), sha256sum="cd" * 32)
        names = {p.name for p in bucket.iterdir()}
        assert HSYNCFOLDER_FILENAME in names
        assert HSYNCSTATE_FILENAME not in names
        assert HSYNCTOMBSTONES_FILENAME not in names
        assert not any(n.startswith(".hsyncstate") for n in names)


class TestFolderMovePreservesState:
    def test_moved_directory_keeps_folder_id_and_base(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        target = src / "keep.txt"
        target.write_text("same")
        with patch("hsync_client.state_backend.CONFIG", _cfg(src)):
            sync_state.record_base(str(target), "ab" * 32)
            sync_state.flush()
            folder_id = read_folder_marker(src)
            sync_state._stores.clear()
        dest = tmp_path / "dest"
        shutil.move(str(src), str(dest))
        with patch("hsync_client.state_backend.CONFIG", _cfg(dest)):
            assert read_folder_marker(dest) == folder_id
            assert sync_state.get_base_hash(str(dest / "keep.txt")) == "ab" * 32


class TestFolderCopyGetsNewId:
    def test_copied_directory_is_a_new_folder(self, tmp_path):
        src = tmp_path / "orig"
        src.mkdir()
        (src / "keep.txt").write_text("same")
        with patch("hsync_client.state_backend.CONFIG", _cfg(src)):
            sync_state.record_base(str(src / "keep.txt"), "ab" * 32)
            sync_state.flush()
            orig_id = read_folder_marker(src)
            assert orig_id is not None
            sync_state._stores.clear()
        copy = tmp_path / "copy"
        shutil.copytree(src, copy)
        with patch("hsync_client.state_backend.CONFIG", _cfg(copy)):
            resolved = resolve_bucket_folder(str(copy))
            assert resolved is not None
            new_id, _ = resolved
            assert new_id != orig_id
            assert read_folder_marker(copy) == new_id
            assert sync_state.get_base_hash(str(copy / "keep.txt")) is None


class TestRetainChunking:
    def test_retain_more_than_1000_paths(self, tmp_path):
        bucket = tmp_path / "big"
        bucket.mkdir()
        keep = []
        with patch("hsync_client.state_backend.CONFIG", _cfg(bucket)):
            for i in range(1200):
                path = bucket / f"keep-{i}.txt"
                path.write_text("x")
                sync_state.record_base(str(path), "ab" * 32)
                keep.append(str(path))
            drop = bucket / "drop.txt"
            drop.write_text("y")
            sync_state.record_base(str(drop), "cd" * 32)
            dropped = sync_state.retain(keep)
            sync_state.flush()
            assert dropped == 1
            assert sync_state.get_base_hash(str(drop)) is None
            assert sync_state.get_base_hash(keep[0]) == "ab" * 32


class TestInternalNameRejectsMarker:
    def test_hsyncfolder_is_internal(self):
        assert is_internal_name(".hsyncfolder") is True
        assert is_internal_name("sub/.hsyncfolder") is True


class TestStateLayoutIsolation:
    def test_per_device_db_paths(self, tmp_path):
        """The same folder resolves to a DB under each client's own device dir."""
        from hsync_common.models.sqlstore import reset_state_db_cache

        from hsync_client.state_backend import reset_bucket_folder_cache

        bucket = tmp_path / "bucket"
        bucket.mkdir()
        (bucket / "a.txt").write_text("x")

        paths = set()
        for device in ("dev-a", "dev-b"):
            reset_bucket_folder_cache()
            reset_state_db_cache()
            with patch(
                "hsync_client.state_backend.default_device_dir",
                return_value=tmp_path / device,
            ):
                _folder_id, db_path = resolve_bucket_folder(str(bucket))
                assert Path(db_path).parent == tmp_path / device / "syncstate"
                paths.add(db_path)
        assert len(paths) == 2

    def test_state_db_override_forces_a_single_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HSYNC_STATE_DB", str(tmp_path / "one.db"))
        bucket = tmp_path / "bucket"
        bucket.mkdir()
        _folder_id, db_path = resolve_bucket_folder(str(bucket))
        assert Path(db_path) == tmp_path / "one.db"
