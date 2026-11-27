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

from hsync_client.trash import (
    TrashSettings,
    TrashStore,
    delete_local_tree,
    empty_store,
    stats_store,
)


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


class _StepClock:
    """A deterministic ``time`` stand-in that advances by *step_ns* per read.

    Lets a test place two deletions in the same wall-clock second while their
    nanosecond identity still orders them, which is what the entry id relies on.
    """

    def __init__(self, start_ns, step_ns=1):
        self._ns = start_ns
        self._step = step_ns

    def time_ns(self):
        current = self._ns
        self._ns += self._step
        return current

    def time(self):
        return self.time_ns() / 1_000_000_000


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

    def test_two_deletions_in_one_second_restore_the_newest(self, store, folder, monkeypatch):
        """A re-created path deleted again must come back as its newest copy.

        Both deletions land in the same wall-clock second, so only the entry id's
        nanosecond field can order them: a bare restore must not depend on
        chance.
        """
        import hsync_client.trash as trash_mod

        monkeypatch.setattr(
            trash_mod, "time", _StepClock(1_700_000_000_000_000_000, step_ns=1_000_000)
        )
        store.move(_write(folder, "x.txt", b"V1"))
        store.move(_write(folder, "x.txt", b"V2"))

        assert len(store.entries()) == 2
        report = store.restore("x.txt")

        assert report.status == "restored"
        assert (folder / "x.txt").read_bytes() == b"V2"
        # The older copy is left in the store, never silently dropped.
        assert [item.entry["relpath"] for item in store.entries()] == ["x.txt"]

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

    def test_force_replaces_the_file_and_keeps_the_previous_copy(self, store, folder):
        """A forced restore never dead-ends: the copy it replaces is parked."""
        store.move(_write(folder))
        _write(folder, content=b"someone else")

        report = store.restore("docs/report.txt", force=True)

        assert (report.status, report.replaced) == ("restored", True)
        assert (folder / "docs/report.txt").read_bytes() == b"hello"
        [replaced] = store.entries()
        assert replaced.entry["origin"] == "user"
        assert replaced.payload.read_bytes() == b"someone else"

    def test_force_cannot_replace_a_directory(self, store, folder):
        """Nothing of a directory could be parked under one file entry."""
        store.move(_write(folder, "docs/report.txt", b"payload"))
        (folder / "docs/report.txt").mkdir()
        (folder / "docs/report.txt" / "inside.txt").write_bytes(b"x")

        report = store.restore("docs/report.txt", force=True)

        assert report.status == "destination-exists"
        assert report.detail == "not-a-regular-file"
        assert (folder / "docs/report.txt" / "inside.txt").exists()

    def test_a_corrupt_entry_leaves_a_forced_destination_untouched(self, store, folder):
        """The bytes are checked before anything is parked under --force."""
        store.move(_write(folder))
        [item] = store.entries()
        item.payload.write_bytes(b"different")
        _write(folder, content=b"someone else")

        report = store.restore("docs/report.txt", force=True)

        assert report.status == "payload-corrupt"
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

    def test_an_entry_without_a_payload_is_discarded(self, store, folder):
        """A crash between the record and the move must not keep a dead entry."""
        store.move(_write(folder))
        [item] = store.entries()
        item.payload.unlink()

        report = store.prune()

        assert (report.discarded, report.kept) == (1, 0)
        assert store.entries() == []
        assert not item.directory.exists()

    def test_an_entry_with_a_truncated_payload_is_discarded(self, store, folder):
        store.move(_write(folder, "docs/report.txt", b"hello"))
        [item] = store.entries()
        item.payload.write_bytes(b"he")

        report = store.prune()

        assert report.discarded == 1
        assert store.entries() == []

    def test_the_budget_is_shared_by_every_folder(self, folder, tmp_path):
        """One volume, one budget: a second share must not get its own allowance."""
        other = tmp_path / "other"
        other.mkdir()
        (other / ".hsyncfolder").write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
        settings = TrashSettings(enabled=True, root=tmp_path / "t", max_size=6)
        first = TrashStore(folder, settings=settings)
        second = TrashStore(other, settings=settings)
        first.move(_write(folder, "old.txt", b"aaaa"))
        [old] = first.entries()
        _rewrite_entry(old, deleted_at=int(time.time()) - 3600)
        second.move(_write(other, "new.txt", b"bbbb"))

        report = first.prune()

        assert report.evicted == 1
        assert first.entries() == []
        assert [item.entry["relpath"] for item in second.entries()] == ["new.txt"]

    def test_empty_store_clears_every_folder(self, folder, tmp_path):
        """--all empties every share of the device, and the budget is shared too."""
        other = tmp_path / "other"
        other.mkdir()
        (other / ".hsyncfolder").write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
        settings = TrashSettings(enabled=True, root=tmp_path / "t")
        TrashStore(folder, settings=settings).move(_write(folder, "a.txt", b"aa"))
        TrashStore(other, settings=settings).move(_write(other, "b.txt", b"bbb"))

        report = empty_store(settings)

        assert (report.removed, report.freed) == (2, 5)
        assert TrashStore(folder, settings=settings).entries() == []
        assert TrashStore(other, settings=settings).entries() == []

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


class TestRestoreAll:
    """One call puts every entry back, which is what a bulk deletion needs."""

    def test_every_entry_comes_back(self, store, folder):
        store.move(_write(folder, "docs/a.txt", b"aa"))
        store.move(_write(folder, "docs/b.txt", b"bbb"))

        report = store.restore_all()

        assert (report.restored, report.skipped, report.failed) == (2, 0, 0)
        assert report.bytes == 5
        assert (folder / "docs/a.txt").read_bytes() == b"aa"
        assert (folder / "docs/b.txt").read_bytes() == b"bbb"
        assert store.entries() == []

    def test_an_existing_path_stays_in_the_store(self, store, folder):
        store.move(_write(folder, "docs/a.txt", b"aa"))
        _write(folder, "docs/a.txt", b"newer")

        report = store.restore_all()

        assert (report.restored, report.skipped) == (0, 1)
        assert report.problems == {"docs/a.txt": "destination-exists"}
        assert (folder / "docs/a.txt").read_bytes() == b"newer"
        assert len(store.entries()) == 1

    def test_a_corrupt_entry_is_reported_as_failed(self, store, folder):
        store.move(_write(folder, "docs/a.txt", b"aa"))
        store.move(_write(folder, "docs/b.txt", b"bb"))
        item = next(one for one in store.entries() if one.entry["relpath"] == "docs/a.txt")
        item.payload.write_bytes(b"x")

        report = store.restore_all()

        assert (report.restored, report.failed) == (1, 1)
        assert report.problems == {"docs/a.txt": "payload-corrupt"}
        assert [one.entry["relpath"] for one in store.entries()] == ["docs/a.txt"]

    def test_a_dry_run_reports_and_writes_nothing(self, store, folder):
        store.move(_write(folder, "docs/a.txt", b"aa"))
        store.move(_write(folder, "docs/b.txt", b"bb"))
        _write(folder, "docs/b.txt", b"again")

        report = store.restore_all(dry_run=True)

        assert (report.restored, report.skipped, report.dry_run) == (1, 1, True)
        assert report.bytes == 2
        assert not (folder / "docs/a.txt").exists()
        assert len(store.entries()) == 2

    def test_a_download_only_folder_refuses_them_all(self, store, folder):
        store.move(_write(folder, "docs/a.txt", b"aa"))

        report = store.restore_all(direction="download-only")

        assert (report.restored, report.skipped) == (0, 1)
        assert report.problems == {"docs/a.txt": "download-only"}

    def test_duplicate_paths_restore_only_the_newest(self, store, folder, monkeypatch):
        """One attempt per path, so the counters match ``problems`` exactly."""
        import hsync_client.trash as trash_mod

        monkeypatch.setattr(
            trash_mod, "time", _StepClock(1_700_000_000_000_000_000, step_ns=1_000_000)
        )
        store.move(_write(folder, "x.txt", b"V1"))
        store.move(_write(folder, "x.txt", b"V2"))

        report = store.restore_all()

        assert (report.restored, report.skipped, report.failed) == (1, 0, 0)
        assert report.problems == {}
        assert (folder / "x.txt").read_bytes() == b"V2"
        # The older copy is left in the store, untouched.
        assert [item.entry["relpath"] for item in store.entries()] == ["x.txt"]

    def test_force_never_lets_an_older_duplicate_win(self, store, folder, monkeypatch):
        """``--force`` replaces an existing file, not the freshly restored newest."""
        import hsync_client.trash as trash_mod

        monkeypatch.setattr(
            trash_mod, "time", _StepClock(1_700_000_000_000_000_000, step_ns=1_000_000)
        )
        store.move(_write(folder, "x.txt", b"V1"))
        store.move(_write(folder, "x.txt", b"V2"))

        report = store.restore_all(force=True)

        assert report.restored == 1
        assert (folder / "x.txt").read_bytes() == b"V2"

    def test_a_prefix_restores_only_its_subtree(self, store, folder):
        """A deleted folder comes back without restoring the whole share."""
        store.move(_write(folder, "photos/2024/a.jpg", b"a"))
        store.move(_write(folder, "photos/b.jpg", b"b"))
        store.move(_write(folder, "notes/c.txt", b"c"))

        report = store.restore_all(prefix="photos")

        assert report.restored == 2
        assert (folder / "photos/2024/a.jpg").read_bytes() == b"a"
        assert (folder / "photos/b.jpg").read_bytes() == b"b"
        assert not (folder / "notes/c.txt").exists()
        assert [item.entry["relpath"] for item in store.entries()] == ["notes/c.txt"]

    def test_a_prefix_tolerates_a_trailing_slash(self, store, folder):
        store.move(_write(folder, "photos/a.jpg", b"a"))

        report = store.restore_all(prefix="/photos/")

        assert report.restored == 1
        assert (folder / "photos/a.jpg").read_bytes() == b"a"


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

    def test_the_entry_records_the_share_name(self, monkeypatch, tmp_path):
        """A front-end reads the share from the entry, so the cycle fills it in."""
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
            config={"REMOTE_BUCKET": "team"},
        )

        store = TrashStore(folder, settings=TrashSettings(enabled=True, root=tmp_path / "trash"))
        [item] = store.entries()
        assert (item.entry["relpath"], item.entry["bucket"]) == ("gone.txt", "team")

    def test_a_store_inside_the_folder_keeps_nothing(self, monkeypatch, tmp_path):
        """The env can point the store into the shared tree; the cycle refuses it."""
        folder = tmp_path / "shared"
        folder.mkdir()
        (folder / ".hsyncfolder").write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
        target = _write(folder, "gone.txt", b"bye")
        from hsync_common.models.file import File

        deleted, _guard = self._run(
            monkeypatch,
            tmp_path,
            folder,
            [File(str(target))],
            config={"TRASH_DIR": str(folder / "trash")},
        )

        assert deleted == 1
        assert not target.exists()
        assert not (folder / "trash").exists()


class TestTreeDelete:
    """A removed folder keeps the bytes of the files it held (ADR-0056)."""

    def test_the_files_of_the_tree_are_parked(self, store, folder):
        _write(folder, "photos/2024/a.jpg", b"aaaa")
        _write(folder, "photos/2024/b.jpg", b"bb")

        kept, left = delete_local_tree(store, folder / "photos")

        assert (kept, left) == (2, 0)
        assert sorted(item.entry["relpath"] for item in store.entries()) == [
            "photos/2024/a.jpg",
            "photos/2024/b.jpg",
        ]
        # The bytes are parked; the directories are left for the caller to remove.
        assert (folder / "photos" / "2024").is_dir()
        assert not (folder / "photos" / "2024" / "a.jpg").exists()

    def test_the_file_cap_leaves_the_rest_and_reports_it(self, store, folder, monkeypatch):
        import hsync_client.trash as trash_mod

        monkeypatch.setattr(trash_mod, "TREE_TRASH_MAX_FILES", 2)
        for name in ("a.txt", "b.txt", "c.txt"):
            _write(folder, f"tree/{name}", b"x")

        kept, left = delete_local_tree(store, folder / "tree")

        assert (kept, left) == (2, 1)
        assert len(store.entries()) == 2

    def test_a_symlink_is_left_to_the_removal(self, store, folder, tmp_path):
        target = tmp_path / "outside.txt"
        target.write_bytes(b"x")
        _write(folder, "tree/real.txt", b"y")
        (folder / "tree" / "link.txt").symlink_to(target)

        kept, left = delete_local_tree(store, folder / "tree")

        assert (kept, left) == (1, 1)
        assert [item.entry["relpath"] for item in store.entries()] == ["tree/real.txt"]

    def test_a_symlinked_directory_is_counted_as_left(self, store, folder, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "x").write_bytes(b"x")
        _write(folder, "tree/real.txt", b"y")
        (folder / "tree" / "linkdir").symlink_to(outside)

        kept, left = delete_local_tree(store, folder / "tree")

        assert (kept, left) == (1, 1)
        assert [item.entry["relpath"] for item in store.entries()] == ["tree/real.txt"]


class TestStats:
    """The store summary a front-end shows as "X of Y" (ADR-0056)."""

    def test_an_empty_store_reports_zeros(self, tmp_path):
        settings = TrashSettings(enabled=True, root=tmp_path / "trash")

        report = stats_store(settings)

        assert (report.entries, report.bytes, report.oldest, report.folders) == (0, 0, 0, 0)
        assert report.budget > 0, "the auto budget always has a floor"

    def test_the_totals_cover_every_folder(self, folder, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        (other / ".hsyncfolder").write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
        settings = TrashSettings(enabled=True, root=tmp_path / "t")
        TrashStore(folder, settings=settings).move(_write(folder, "a.txt", b"aa"))
        second = TrashStore(other, settings=settings)
        second.move(_write(other, "b.txt", b"bbb"))
        [older] = [item for item in second.entries() if item.entry["relpath"] == "b.txt"]
        _rewrite_entry(older, deleted_at=int(time.time()) - 3600)

        report = stats_store(settings)

        assert (report.entries, report.bytes, report.folders) == (2, 5, 2)
        assert report.oldest == int(time.time()) - 3600
        assert report.root == str(settings.root)


class TestCommands:
    """The CLI surface a front-end consumes: every verb can answer in JSON."""

    @staticmethod
    def _run(monkeypatch, tmp_path, *args):
        from typer.testing import CliRunner

        from hsync_client.cli import app

        monkeypatch.setenv("HSYNC_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("HSYNC_TRASH_DIR", str(tmp_path / "trash"))
        return CliRunner().invoke(app, ["trash", *args])

    @staticmethod
    def _folder(tmp_path):
        folder = tmp_path / "shared"
        folder.mkdir()
        (folder / ".hsyncfolder").write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
        return folder

    def _park(self, monkeypatch, tmp_path, folder, relpath="gone.txt", content=b"bye"):
        _write(folder, relpath, content)
        result = self._run(monkeypatch, tmp_path, "add", "--folder", str(folder), relpath)
        assert result.exit_code == 0, result.output

    def _store(self, folder, tmp_path):
        return TrashStore(folder, settings=TrashSettings(enabled=True, root=tmp_path / "trash"))

    def test_add_parks_a_folder_and_removes_it(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        _write(folder, "photos/2024/a.txt", b"aa")
        _write(folder, "photos/b.txt", b"bb")
        (folder / "photos" / "empty").mkdir()

        result = self._run(monkeypatch, tmp_path, "add", "--folder", str(folder), "photos")

        assert result.exit_code == 0, result.output
        assert not (folder / "photos").exists()
        assert sorted(
            item.entry["relpath"] for item in self._store(folder, tmp_path).entries()
        ) == [
            "photos/2024/a.txt",
            "photos/b.txt",
        ]

    def test_add_removes_a_symlink_without_following_it(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        target = tmp_path / "outside"
        target.mkdir()
        (target / "x.txt").write_bytes(b"x")
        (folder / "linkdir").symlink_to(target)

        result = self._run(monkeypatch, tmp_path, "add", "--folder", str(folder), "linkdir")

        assert result.exit_code == 0, result.output
        assert not (folder / "linkdir").exists()
        assert (target / "x.txt").exists()
        assert self._store(folder, tmp_path).entries() == []

    def test_list_json_flags_a_missing_payload(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        self._park(monkeypatch, tmp_path, folder)
        self._store(folder, tmp_path).entries()[0].payload.unlink()

        result = self._run(monkeypatch, tmp_path, "list", "--folder", str(folder), "--json")

        assert result.exit_code == 0, result.output
        [entry] = json.loads(result.output)
        assert (entry["path"], entry["payload_present"]) == ("gone.txt", False)

    def test_restore_json_reports_the_outcome(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        self._park(monkeypatch, tmp_path, folder)

        result = self._run(
            monkeypatch, tmp_path, "restore", "--folder", str(folder), "gone.txt", "--json"
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["status"] == "restored"
        assert (folder / "gone.txt").read_bytes() == b"bye"

    def test_restore_json_keeps_a_failure_non_zero(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)

        result = self._run(
            monkeypatch, tmp_path, "restore", "--folder", str(folder), "missing.txt", "--json"
        )

        assert result.exit_code == 1
        assert json.loads(result.output)["status"] == "no-entry"

    def test_restore_all_puts_every_entry_back(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        self._park(monkeypatch, tmp_path, folder, "a.txt", b"aa")
        self._park(monkeypatch, tmp_path, folder, "b.txt", b"bb")

        result = self._run(
            monkeypatch, tmp_path, "restore", "--folder", str(folder), "--all", "--json"
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert (payload["restored"], payload["skipped"], payload["problems"]) == (2, 0, {})
        assert (folder / "a.txt").read_bytes() == b"aa"
        assert (folder / "b.txt").read_bytes() == b"bb"

    def test_restore_prefix_brings_back_one_folder(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        self._park(monkeypatch, tmp_path, folder, "photos/a.txt", b"aa")
        self._park(monkeypatch, tmp_path, folder, "notes/b.txt", b"bb")

        result = self._run(
            monkeypatch,
            tmp_path,
            "restore",
            "--folder",
            str(folder),
            "--prefix",
            "photos",
            "--json",
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["restored"] == 1
        assert (folder / "photos/a.txt").read_bytes() == b"aa"
        assert not (folder / "notes/b.txt").exists()

    def test_restore_all_can_report_without_writing(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        self._park(monkeypatch, tmp_path, folder)

        result = self._run(
            monkeypatch, tmp_path, "restore", "--folder", str(folder), "--all", "--dry-run"
        )

        assert result.exit_code == 0, result.output
        assert "Would restore 1 of 1 entries" in result.output
        assert not (folder / "gone.txt").exists()

    def test_restore_all_names_the_entries_it_leaves(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        self._park(monkeypatch, tmp_path, folder)
        (folder / "gone.txt").write_bytes(b"newer")

        result = self._run(monkeypatch, tmp_path, "restore", "--folder", str(folder), "--all")

        assert result.exit_code == 0, result.output
        assert "left in the trash: gone.txt (destination-exists)" in result.output

    def test_restore_needs_exactly_one_target(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)

        neither = self._run(monkeypatch, tmp_path, "restore", "--folder", str(folder))
        both = self._run(
            monkeypatch, tmp_path, "restore", "--folder", str(folder), "gone.txt", "--all"
        )
        all_and_prefix = self._run(
            monkeypatch, tmp_path, "restore", "--folder", str(folder), "--all", "--prefix", "docs"
        )

        assert neither.exit_code != 0, neither.output
        assert both.exit_code != 0, both.output
        assert all_and_prefix.exit_code != 0, all_and_prefix.output

    def test_restore_all_json_reports_the_outcome(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        self._park(monkeypatch, tmp_path, folder)

        result = self._run(
            monkeypatch, tmp_path, "restore", "--folder", str(folder), "--all", "--json"
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["status"] == "ok"

    def test_empty_json_reports_the_count(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        self._park(monkeypatch, tmp_path, folder)

        result = self._run(
            monkeypatch, tmp_path, "empty", "--folder", str(folder), "--yes", "--json"
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"dry_run": False, "freed": 3, "removed": 1}

    def test_prune_applies_the_window_without_a_folder(self, monkeypatch, tmp_path):
        """A stopped or ``--once`` client never reaches the hourly gate."""
        folder = self._folder(tmp_path)
        self._park(monkeypatch, tmp_path, folder)
        store = self._store(folder, tmp_path)
        _rewrite_entry(store.entries()[0], deleted_at=int(time.time()) - 40 * 86_400)

        result = self._run(monkeypatch, tmp_path, "prune", "--json")

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["expired"] == 1
        assert store.entries() == []

    def test_prune_reports_its_verdict_for_a_person(self, monkeypatch, tmp_path):
        result = self._run(monkeypatch, tmp_path, "prune")

        assert result.exit_code == 0, result.output
        assert "Removed: expired 0" in result.output

    def test_prune_can_report_without_deleting(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        self._park(monkeypatch, tmp_path, folder)
        store = self._store(folder, tmp_path)
        _rewrite_entry(store.entries()[0], deleted_at=int(time.time()) - 40 * 86_400)

        result = self._run(monkeypatch, tmp_path, "prune", "--dry-run", "--json")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert (payload["dry_run"], payload["expired"]) == (True, 1)
        assert len(store.entries()) == 1, "a dry run must not delete anything"

    def test_empty_can_report_without_asking_or_deleting(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        self._park(monkeypatch, tmp_path, folder)
        store = self._store(folder, tmp_path)

        result = self._run(monkeypatch, tmp_path, "empty", "--folder", str(folder), "--dry-run")

        assert result.exit_code == 0, result.output
        assert "Would remove 1 entry" in result.output
        assert len(store.entries()) == 1, "a dry run must not delete anything"

    def test_stats_reports_the_store_against_its_budget(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        self._park(monkeypatch, tmp_path, folder)

        result = self._run(monkeypatch, tmp_path, "stats", "--json")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert (payload["entries"], payload["bytes"], payload["folders"]) == (1, 3, 1)
        assert payload["budget"] > 0
        assert payload["root"] == str(tmp_path / "trash")

    def test_stats_can_answer_for_a_person(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)
        self._park(monkeypatch, tmp_path, folder)

        result = self._run(monkeypatch, tmp_path, "stats")

        assert result.exit_code == 0, result.output
        assert "1 entry, 3 B" in result.output

    def test_empty_all_clears_every_folder(self, monkeypatch, tmp_path):
        first = self._folder(tmp_path)
        second = tmp_path / "other"
        second.mkdir()
        (second / ".hsyncfolder").write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
        self._park(monkeypatch, tmp_path, first)
        self._park(monkeypatch, tmp_path, second, "gone2.txt", b"b")

        result = self._run(monkeypatch, tmp_path, "empty", "--all", "--yes", "--json")

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"dry_run": False, "freed": 4, "removed": 2}

    def test_empty_needs_exactly_one_scope(self, monkeypatch, tmp_path):
        folder = self._folder(tmp_path)

        neither = self._run(monkeypatch, tmp_path, "empty", "--yes")
        both = self._run(monkeypatch, tmp_path, "empty", "--folder", str(folder), "--all", "--yes")

        assert (neither.exit_code, both.exit_code) == (2, 2)
        assert "choose one of --folder DIR or --all" in neither.output


class TestStoreLocation:
    """The store never lands inside the shared folder (ADR-0056)."""

    @staticmethod
    def _frame(folder, trash_dir):
        """A ``p2p``/``server`` option frame with everything validation reads."""
        from hsync_client.cli_options import _SHARED_VALIDATION_KEYS

        frame = dict.fromkeys(_SHARED_VALIDATION_KEYS)
        frame.update(
            max_wait_timeout=120,
            sync_direction="bidirectional",
            max_inbound_size=10240,
            trash_dir=trash_dir,
            local_path=str(folder),
        )
        return frame

    @staticmethod
    def _validate(frame):
        from hsync_client.cli_options import _validate_shared_options

        return _validate_shared_options(frame)

    def test_the_flag_inside_the_folder_is_refused(self, tmp_path, monkeypatch):
        import typer

        monkeypatch.delenv("HSYNC_TRASH_DIR", raising=False)
        monkeypatch.setenv("HSYNC_STATE_DIR", str(tmp_path / "state"))
        folder = tmp_path / "shared"
        folder.mkdir()

        with pytest.raises(typer.BadParameter, match="must not live inside the synced folder"):
            self._validate(self._frame(folder, str(folder / "trash")))

    def test_the_environment_directory_inside_the_folder_is_refused(self, tmp_path, monkeypatch):
        """The same rule holds without a flag: HSYNC_TRASH_DIR cannot bypass it."""
        import typer

        monkeypatch.setenv("HSYNC_STATE_DIR", str(tmp_path / "state"))
        folder = tmp_path / "shared"
        folder.mkdir()
        monkeypatch.setenv("HSYNC_TRASH_DIR", str(folder / ".trash"))

        with pytest.raises(typer.BadParameter, match="must not live inside the synced folder"):
            self._validate(self._frame(folder, None))

    def test_a_state_dir_inside_the_folder_is_refused(self, tmp_path, monkeypatch):
        """The default store follows the state dir, so relocating it is covered too."""
        import typer

        monkeypatch.delenv("HSYNC_TRASH_DIR", raising=False)
        folder = tmp_path / "shared"
        folder.mkdir()
        monkeypatch.setenv("HSYNC_STATE_DIR", str(folder / ".state"))

        with pytest.raises(typer.BadParameter, match="must not live inside the synced folder"):
            self._validate(self._frame(folder, None))

    def test_a_store_outside_the_folder_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HSYNC_TRASH_DIR", raising=False)
        monkeypatch.setenv("HSYNC_STATE_DIR", str(tmp_path / "state"))
        folder = tmp_path / "shared"
        folder.mkdir()

        self._validate(self._frame(folder, str(tmp_path / "trash")))
