# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for ``hsync state reset`` (client-side sync-state reset)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from hsync_common.models.folder import client_state_db_path, client_state_index_db_path
from hsync_common.models.sqlstore import StateDb, get_state_db
from typer.testing import CliRunner

from hsync_client.identity import InstanceLockError, default_device_dir
from hsync_client.state_reset import reset_folder_state, resolve_state_db

FOLDER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_FOLDER_ID = "22222222-2222-4222-8222-222222222222"


def _write_marker(folder: Path, folder_id: str = FOLDER_ID) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".hsyncfolder").write_text(f"{folder_id}\n", encoding="utf-8")


def _db_path(folder_id: str = FOLDER_ID) -> Path:
    return client_state_db_path(folder_id, Path(default_device_dir()))


@pytest.fixture
def direct_state_db():
    """Open directly-created ``StateDb`` handles closed after the test.

    Only handles obtained through ``get_state_db`` live in the module cache the
    conftest fixture resets; a ``StateDb(...)`` built here is not cached, so it
    must be closed explicitly or its connection is collected (and reported as
    ``ResourceWarning: unclosed database``) during a later test.
    """
    opened: list[StateDb] = []

    def _open(path: Path) -> StateDb:
        db = StateDb(path)
        opened.append(db)
        return db

    yield _open
    for db in opened:
        db.close()


def _seed_db(folder_id: str = FOLDER_ID, *, versions: int = 2, tombstones: int = 1) -> Path:
    """Create the per-folder DB with *versions* bases and *tombstones* markers."""
    db_path = _db_path(folder_id)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = StateDb(db_path)
    try:
        db.ensure_folder(folder_id, f"/bucket-{folder_id}")
        for index in range(versions):
            db.upsert_version(
                folder_id,
                f"k{index}",
                base_hash="h",
                version_id="v",
                origin_cid=None,
                ts=1.0,
                vclock="{}",
                corrected_mtime=0.0,
            )
        for index in range(tombstones):
            db.upsert_tombstone(
                folder_id,
                f"gone{index}",
                sha256sum="h",
                version_id="v",
                origin_cid=None,
                ts=1.0,
                vclock="{}",
            )
    finally:
        db.close()
    return db_path


class TestResolveStateDb:
    def test_resolves_the_db_named_by_the_marker(self, tmp_path):
        folder = tmp_path / "bucket"
        _write_marker(folder)
        db_path = _seed_db()

        assert resolve_state_db(folder) == (FOLDER_ID, db_path)

    def test_folder_without_marker_or_index_has_no_state(self, tmp_path):
        folder = tmp_path / "bucket"
        folder.mkdir()

        assert resolve_state_db(folder) is None
        # A maintenance command must never write into the user's folder.
        assert not (folder / ".hsyncfolder").exists()
        assert not client_state_index_db_path(Path(default_device_dir())).exists()

    def test_marker_less_folder_reanchors_through_the_index(self, tmp_path):
        folder = tmp_path / "bucket"
        folder.mkdir()
        db_path = _seed_db()
        index = get_state_db(client_state_index_db_path(Path(default_device_dir())))
        index.ensure_folder(FOLDER_ID, os.path.realpath(folder))

        assert resolve_state_db(folder) == (FOLDER_ID, db_path)
        assert not (folder / ".hsyncfolder").exists()

    def test_duplicate_marker_of_a_live_folder_has_no_state(self, tmp_path):
        original = tmp_path / "original"
        copy = tmp_path / "copy"
        _write_marker(original)
        _write_marker(copy)
        _seed_db()
        index = get_state_db(client_state_index_db_path(Path(default_device_dir())))
        index.ensure_folder(FOLDER_ID, os.path.realpath(original))

        # The engine mints a fresh UUID for the copy, so the copy has nothing to
        # reset and the original's state must stay untouched.
        assert resolve_state_db(copy) is None
        assert resolve_state_db(original) == (FOLDER_ID, _db_path())

    def test_moved_folder_keeps_the_marker_uuid(self, tmp_path):
        moved = tmp_path / "moved"
        _write_marker(moved)
        db_path = _seed_db()
        index = get_state_db(client_state_index_db_path(Path(default_device_dir())))
        index.ensure_folder(FOLDER_ID, str(tmp_path / "gone"))

        assert resolve_state_db(moved) == (FOLDER_ID, db_path)


class TestResetFolderState:
    def test_clears_versions_and_tombstones_only_for_that_folder(self, tmp_path, direct_state_db):
        folder = tmp_path / "bucket"
        _write_marker(folder)
        _seed_db(versions=3, tombstones=2)
        other_db = _seed_db(OTHER_FOLDER_ID, versions=1, tombstones=1)
        user_file = folder / "notes.txt"
        user_file.write_text("payload", encoding="utf-8")

        report = reset_folder_state(folder)

        assert report.found is True
        assert report.folder_id == FOLDER_ID
        assert (report.versions_deleted, report.tombstones_deleted) == (3, 2)
        assert report.db_path == str(_db_path())
        assert report.quarantined == ""

        db = direct_state_db(_db_path())
        assert db.count_versions(FOLDER_ID) == 0
        assert db.iter_tombstones(FOLDER_ID) == []
        # The folder row survives so the next cycle re-opens the same DB.
        assert db.folder_canonical_path(FOLDER_ID) is not None
        # The other folder's own DB is untouched.
        other = direct_state_db(other_db)
        assert other.count_versions(OTHER_FOLDER_ID) == 1
        assert len(other.iter_tombstones(OTHER_FOLDER_ID)) == 1
        # No synced file and no marker is touched.
        assert user_file.read_text(encoding="utf-8") == "payload"
        assert (folder / ".hsyncfolder").read_text(encoding="utf-8").strip() == FOLDER_ID

    def test_shared_state_db_only_drops_the_target_folder(
        self, tmp_path, monkeypatch, direct_state_db
    ):
        shared = tmp_path / "shared.db"
        monkeypatch.setenv("HSYNC_STATE_DB", str(shared))
        folder = tmp_path / "bucket"
        _write_marker(folder)
        db = direct_state_db(shared)
        for folder_id in (FOLDER_ID, OTHER_FOLDER_ID):
            db.ensure_folder(folder_id, f"/bucket-{folder_id}")
            db.upsert_version(
                folder_id,
                "k",
                base_hash="h",
                version_id="v",
                origin_cid=None,
                ts=1.0,
                vclock="{}",
                corrected_mtime=0.0,
            )

        report = reset_folder_state(folder)

        assert report.db_path == str(shared)
        assert report.versions_deleted == 1
        assert db.count_versions(FOLDER_ID) == 0
        assert db.count_versions(OTHER_FOLDER_ID) == 1

    def test_marker_less_folder_reanchors_through_the_shared_state_db(
        self, tmp_path, monkeypatch, direct_state_db
    ):
        """With HSYNC_STATE_DB the engine resolves folders from that one file."""
        shared = tmp_path / "shared.db"
        monkeypatch.setenv("HSYNC_STATE_DB", str(shared))
        folder = tmp_path / "bucket"
        folder.mkdir()
        db = direct_state_db(shared)
        db.ensure_folder(FOLDER_ID, os.path.realpath(folder))
        db.upsert_version(
            FOLDER_ID,
            "k",
            base_hash="h",
            version_id="v",
            origin_cid=None,
            ts=1.0,
            vclock="{}",
            corrected_mtime=0.0,
        )

        report = reset_folder_state(folder)

        assert report.found is True
        assert report.db_path == str(shared)
        assert report.versions_deleted == 1
        assert not (folder / ".hsyncfolder").exists()

    def test_is_idempotent(self, tmp_path):
        folder = tmp_path / "bucket"
        _write_marker(folder)
        _seed_db()

        assert reset_folder_state(folder).versions_deleted == 2
        second = reset_folder_state(folder)
        assert second.found is True
        assert (second.versions_deleted, second.tombstones_deleted) == (0, 0)

    def test_folder_without_state_is_a_successful_noop(self, tmp_path):
        folder = tmp_path / "bucket"
        folder.mkdir()

        report = reset_folder_state(folder)

        assert report.found is False
        assert report.db_path == ""
        assert not (folder / ".hsyncfolder").exists()

    def test_refuses_when_another_process_holds_the_lock(
        self, tmp_path, monkeypatch, direct_state_db
    ):
        folder = tmp_path / "bucket"
        _write_marker(folder)
        _seed_db()

        def _busy(_path: str):
            raise InstanceLockError("another hsync process is syncing this folder")

        monkeypatch.setattr("hsync_client.state_reset.InstanceLock.acquire", _busy)

        with pytest.raises(InstanceLockError):
            reset_folder_state(folder)
        # Nothing was deleted before the lock check.
        assert direct_state_db(_db_path()).count_versions(FOLDER_ID) == 2

    def test_unreadable_db_fails_without_quarantine(self, tmp_path):
        folder = tmp_path / "bucket"
        _write_marker(folder)
        db_path = _db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(b"not a sqlite database")

        with pytest.raises(sqlite3.DatabaseError):
            reset_folder_state(folder)
        assert db_path.exists()

    def test_quarantine_moves_the_unreadable_db_aside(self, tmp_path):
        folder = tmp_path / "bucket"
        _write_marker(folder)
        db_path = _db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(b"not a sqlite database")

        report = reset_folder_state(folder, quarantine=True)

        assert report.found is True
        assert report.quarantined
        quarantine_path = Path(report.quarantined)
        assert quarantine_path.exists()
        assert quarantine_path.read_bytes() == b"not a sqlite database"
        assert not db_path.exists()
        assert quarantine_path.name.startswith(db_path.name + ".corrupt-")


class TestStateResetCli:
    def _run(self, *args: str):
        from hsync_client.cli import app

        return CliRunner().invoke(app, ["state", "reset", *args])

    def test_json_summary(self, tmp_path):
        folder = tmp_path / "bucket"
        _write_marker(folder)
        _seed_db(versions=3, tombstones=1)

        result = self._run("--folder", str(folder), "--json")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # The JSON field names are the desktop's contract (engine/cli.py parses
        # them); pin the exact key set so a rename cannot silently zero the GUI.
        assert set(payload) == {
            "folder",
            "folder_id",
            "db_path",
            "found",
            "versions_deleted",
            "tombstones_deleted",
            "quarantined",
        }
        assert payload["folder_id"] == FOLDER_ID
        assert payload["found"] is True
        assert payload["versions_deleted"] == 3
        assert payload["tombstones_deleted"] == 1
        assert payload["quarantined"] == ""

    def test_human_summary(self, tmp_path):
        folder = tmp_path / "bucket"
        _write_marker(folder)
        _seed_db()

        first = self._run("--folder", str(folder))
        assert first.exit_code == 0, first.output
        assert "2 version entries and 1 deletion markers removed" in first.output

        empty = self._run("--folder", str(folder))
        assert empty.exit_code == 0, empty.output
        assert "0 version entries and 0 deletion markers removed" in empty.output

    def test_empty_folder_is_rejected(self):
        result = self._run("--folder", "")

        assert result.exit_code == 1
        assert "requires a directory path" in result.output

    def test_missing_folder_exits_with_a_fatal_code(self, tmp_path):
        result = self._run("--folder", str(tmp_path / "nope"))

        assert result.exit_code == 1
        assert "Not a directory" in result.output

    def test_lock_conflict_exits_with_a_fatal_code(self, tmp_path, monkeypatch):
        folder = tmp_path / "bucket"
        _write_marker(folder)
        _seed_db()

        def _busy(_path: str):
            raise InstanceLockError("another hsync process is syncing this folder")

        monkeypatch.setattr("hsync_client.state_reset.InstanceLock.acquire", _busy)

        result = self._run("--folder", str(folder))

        assert result.exit_code == 1
        assert "another hsync process is syncing this folder" in result.output
