# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Behaviour of the device-local trash (ADR-0056).

The store is exercised against a real folder with a real ``.hsyncfolder``
marker: moving, listing, restoring, the retention/budget sweep and the guards
that keep a restore from fighting the folder's own rules.
"""

from __future__ import annotations

import json
import os
import time
import uuid

import pytest
from hsync_common.constants import TRASH_ENTRY_FILENAME

from hsync_client.trash import TrashSettings, TrashStore


@pytest.fixture
def folder(tmp_path):
    """A synced folder: a directory carrying its ``.hsyncfolder`` marker."""
    path = tmp_path / "shared"
    path.mkdir()
    (path / ".hsyncfolder").write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
    return path


@pytest.fixture
def store(folder, tmp_path):
    """A store rooted in a temp dir, so no test touches the host state."""
    return TrashStore(
        folder,
        settings=TrashSettings(enabled=True, root=tmp_path / "trash", retention_days=30),
        cid="TestDeviceCid1234",
        bucket="team",
    )


def _write(folder, relpath="docs/report.txt", content=b"hello"):
    path = folder / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _rewrite_entry(item, **overrides):
    """Edit an entry's record on disk (ages it, changes its hash, ...)."""
    record = dict(item.entry)
    record.update(overrides)
    (item.directory / TRASH_ENTRY_FILENAME).write_text(json.dumps(record), encoding="utf-8")


class TestMove:
    def test_the_bytes_and_the_record_move_into_the_store(self, store, folder):
        source = _write(folder)

        result = store.move(source)

        assert result.trashed is True
        assert not source.exists()
        [item] = store.entries()
        assert item.entry["relpath"] == "docs/report.txt"
        assert item.entry["origin"] == "sync"
        assert item.entry["bucket"] == "team"
        assert item.entry["cid"] == "TestDeviceCid1234"
        assert item.entry["size"] == len(b"hello")
        assert item.payload.read_bytes() == b"hello"

    def test_a_disabled_store_moves_nothing(self, folder, tmp_path):
        store = TrashStore(folder, settings=TrashSettings(enabled=False, root=tmp_path / "t"))
        source = _write(folder)

        result = store.move(source)

        assert (result.trashed, result.reason) == (False, "disabled")
        assert source.exists()

    def test_a_folder_without_a_marker_has_no_trash(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        store = TrashStore(plain, settings=TrashSettings(enabled=True, root=tmp_path / "t"))
        source = _write(plain)

        result = store.move(source)

        assert (result.trashed, result.reason) == (False, "no-folder-id")
        assert source.exists()

    def test_a_symlink_is_left_alone(self, store, folder, tmp_path):
        target = tmp_path / "outside.txt"
        target.write_bytes(b"x")
        link = folder / "link.txt"
        link.symlink_to(target)

        result = store.move(link)

        assert (result.trashed, result.reason) == (False, "not-a-regular-file")
        assert link.is_symlink()

    def test_a_path_outside_the_folder_is_refused(self, store, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"x")

        result = store.move(outside)

        assert (result.trashed, result.reason) == (False, "outside-folder")
        assert outside.exists()


class TestListAndRestore:
    def test_listing_is_newest_first(self, store, folder):
        first = store.move(_write(folder, "a.txt"))
        [old] = store.entries()
        _rewrite_entry(old, deleted_at=int(time.time()) - 60)
        second = store.move(_write(folder, "b.txt"))

        assert [item.entry_id for item in store.entries()] == [second.entry_id, first.entry_id]

    def test_restore_puts_the_bytes_back_and_drops_the_entry(self, store, folder):
        store.move(_write(folder, "docs/report.txt", b"payload"))

        report = store.restore("docs/report.txt")

        assert report.status == "restored"
        assert report.size == len(b"payload")
        assert (folder / "docs/report.txt").read_bytes() == b"payload"
        assert store.entries() == []

    def test_restore_keeps_the_recorded_mtime(self, store, folder):
        source = _write(folder)
        stamp = 1_600_000_000_000_000_000
        os.utime(source, ns=(stamp, stamp))
        store.move(source)

        store.restore("docs/report.txt")

        assert (folder / "docs/report.txt").stat().st_mtime_ns == stamp

    def test_restore_refuses_to_overwrite(self, store, folder):
        store.move(_write(folder))
        _write(folder, content=b"someone else")

        report = store.restore("docs/report.txt")

        assert report.status == "destination-exists"
        assert (folder / "docs/report.txt").read_bytes() == b"someone else"
        assert len(store.entries()) == 1

    def test_a_download_only_folder_refuses_the_restore(self, store, folder):
        store.move(_write(folder))

        report = store.restore("docs/report.txt", direction="download-only")

        assert report.status == "download-only"
        assert not (folder / "docs/report.txt").exists()

    def test_an_unknown_path_is_reported(self, store):
        assert store.restore("nope.txt").status == "no-entry"

    def test_a_tampered_payload_is_not_restored(self, store, folder):
        store.move(_write(folder))
        [item] = store.entries()
        item.payload.write_bytes(b"different")

        report = store.restore("docs/report.txt")

        assert report.status == "payload-corrupt"
        assert not (folder / "docs/report.txt").exists()


class TestMaintenance:
    def test_an_expired_entry_is_swept(self, store, folder):
        store.move(_write(folder))
        [item] = store.entries()
        _rewrite_entry(item, deleted_at=int(time.time()) - 40 * 86_400)

        report = store.prune()

        assert report.expired == 1
        assert store.entries() == []

    def test_a_recent_entry_survives(self, store, folder):
        store.move(_write(folder))

        report = store.prune()

        assert (report.expired, report.evicted, report.kept) == (0, 0, 1)

    def test_the_budget_evicts_the_oldest(self, folder, tmp_path):
        store = TrashStore(
            folder,
            settings=TrashSettings(enabled=True, root=tmp_path / "t", max_size=6),
        )
        store.move(_write(folder, "old.txt", b"aaaa"))
        [old] = store.entries()
        # Ages are explicit: ``deleted_at`` is whole seconds, so two moves in the
        # same second would tie.
        _rewrite_entry(old, deleted_at=int(time.time()) - 3600)
        store.move(_write(folder, "new.txt", b"bbbb"))

        report = store.prune()

        assert report.evicted == 1
        assert [item.entry["relpath"] for item in store.entries()] == ["new.txt"]

    def test_an_unreadable_record_is_discarded(self, store, folder):
        store.move(_write(folder))
        [item] = store.entries()
        (item.directory / TRASH_ENTRY_FILENAME).write_text("{not json", encoding="utf-8")

        store.prune()

        assert store.entries() == []
        assert not item.directory.exists()

    def test_empty_removes_every_entry(self, store, folder):
        store.move(_write(folder, "a.txt", b"aa"))
        store.move(_write(folder, "b.txt", b"bbb"))

        report = store.empty()

        assert (report.removed, report.freed) == (2, 5)
        assert store.entries() == []

    def test_empty_can_keep_the_recent_entries(self, store, folder):
        store.move(_write(folder, "old.txt", b"aa"))
        [item] = store.entries()
        _rewrite_entry(item, deleted_at=int(time.time()) - 5 * 86_400)
        store.move(_write(folder, "new.txt", b"bb"))

        report = store.empty(older_than_days=2)

        assert report.removed == 1
        assert [item.entry["relpath"] for item in store.entries()] == ["new.txt"]


class TestAdd:
    """A user deletion parks the bytes with origin ``user``."""

    def test_a_user_file_moves_into_the_store(self, store, folder):
        from hsync_common.models.trash import TRASH_ORIGIN_USER

        _write(folder, "docs/report.txt", b"bye")

        result = store.move(folder / "docs/report.txt", origin=TRASH_ORIGIN_USER)

        assert result.trashed is True
        assert not (folder / "docs/report.txt").exists()
        [item] = store.entries()
        assert item.entry["origin"] == TRASH_ORIGIN_USER
        assert item.payload.read_bytes() == b"bye"

    def test_the_restore_works_the_same_way(self, store, folder):
        from hsync_common.models.trash import TRASH_ORIGIN_USER

        store.move(_write(folder, "docs/report.txt", b"bye"), origin=TRASH_ORIGIN_USER)

        assert store.restore("docs/report.txt").status == "restored"
        assert (folder / "docs/report.txt").read_bytes() == b"bye"

    def test_a_missing_file_is_refused(self, store, folder):
        result = store.move(folder / "absent.txt")

        assert (result.trashed, result.reason) == (False, "not-a-regular-file")

    def test_a_read_only_source_is_retried_after_clearing_the_flag(
        self, store, folder, monkeypatch
    ):
        """Windows refuses to move a read-only file; POSIX never takes the retry."""
        import hsync_client.trash as trash_mod

        source = _write(folder, "docs/report.txt", b"bye")
        real_replace = trash_mod.os.replace
        failed: set[str] = set()

        def flaky_replace(src, dst):
            if str(src) == str(source) and str(src) not in failed:
                failed.add(str(src))
                raise PermissionError(13, "read-only")
            return real_replace(src, dst)

        cleared: list[str] = []
        monkeypatch.setattr(trash_mod.os, "replace", flaky_replace)
        monkeypatch.setattr(trash_mod, "clear_read_only", lambda path: cleared.append(path))

        result = store.move(source)

        assert result.trashed is True
        assert cleared == [str(source)]
        assert [item.entry["relpath"] for item in store.entries()] == ["docs/report.txt"]


class TestAddCommand:
    """``hsync trash add`` is the front-end entry point to the same store."""

    @staticmethod
    def _run(monkeypatch, tmp_path, folder, *args):
        from typer.testing import CliRunner

        from hsync_client.cli import app

        monkeypatch.setenv("HSYNC_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("HSYNC_TRASH_DIR", str(tmp_path / "trash"))
        return CliRunner().invoke(app, ["trash", "add", "--folder", str(folder), *args])

    def test_it_moves_the_file_and_reports_it(self, monkeypatch, tmp_path):
        folder = tmp_path / "shared"
        folder.mkdir()
        (folder / ".hsyncfolder").write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
        _write(folder, "docs/report.txt", b"bye")

        result = self._run(monkeypatch, tmp_path, folder, "docs/report.txt")

        assert result.exit_code == 0, result.output
        assert "trash" in result.output
        assert not (folder / "docs/report.txt").exists()
        store = TrashStore(folder, settings=TrashSettings(enabled=True, root=tmp_path / "trash"))
        assert [item.entry["relpath"] for item in store.entries()] == ["docs/report.txt"]

    def test_a_path_outside_the_folder_fails(self, monkeypatch, tmp_path):
        folder = tmp_path / "shared"
        folder.mkdir()
        (folder / ".hsyncfolder").write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"x")

        result = self._run(monkeypatch, tmp_path, folder, str(outside))

        assert result.exit_code == 1
        assert "not inside the folder" in result.output
        assert outside.exists()


class TestSyncCycle:
    """The write path: a deletion the cycle applies parks the bytes first."""

    @staticmethod
    def _run(monkeypatch, tmp_path, folder, files, *, config=None):
        from unittest.mock import patch

        from hsync_client import config as config_mod
        from hsync_client import sync as sync_mod

        class _Observer:
            def remove_file(self, _path):
                pass

        monkeypatch.setenv("HSYNC_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("HSYNC_TRASH_DIR", str(tmp_path / "trash"))
        active = {
            "LOCAL_BUCKET": str(folder),
            "CID": "TestDeviceCid1234",
            "DELETION_POLICY": "propagate",
        }
        active.update(config or {})
        with (
            patch.object(sync_mod, "CONFIG", active),
            # The store reads the shared CONFIG object (``set_config`` updates it
            # in place), so the settings must be visible there too.
            patch.dict(config_mod.CONFIG, active, clear=True),
            patch.object(sync_mod.sync_state, "clear_base", lambda *a, **k: None),
        ):
            return sync_mod._apply_deletions(list(files), [], [], [], [], _Observer())

    def test_the_deleted_bytes_reach_the_store(self, monkeypatch, tmp_path):
        folder = tmp_path / "shared"
        folder.mkdir()
        (folder / ".hsyncfolder").write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
        target = _write(folder, "gone.txt", b"bye")
        from hsync_common.models.file import File

        deleted, _guard = self._run(monkeypatch, tmp_path, folder, [File(str(target))])

        assert deleted == 1
        assert not target.exists()
        store = TrashStore(folder, settings=TrashSettings(enabled=True, root=tmp_path / "trash"))
        assert [item.entry["relpath"] for item in store.entries()] == ["gone.txt"]

    def test_with_the_setting_off_the_file_is_only_unlinked(self, monkeypatch, tmp_path):
        folder = tmp_path / "shared"
        folder.mkdir()
        (folder / ".hsyncfolder").write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
        target = _write(folder, "gone.txt", b"bye")
        from hsync_common.models.file import File

        self._run(
            monkeypatch,
            tmp_path,
            folder,
            [File(str(target))],
            config={"TRASH_ENABLED": False},
        )

        assert not target.exists()
        store = TrashStore(folder, settings=TrashSettings(enabled=True, root=tmp_path / "trash"))
        assert store.entries() == []
