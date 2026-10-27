"""Empty-directory sweep: a folder removed on the peer must not linger here.

Directories carry no tombstones, so after the last file of a folder is deleted
on another device the local tree keeps an empty shell. The sweep reconciles it
against a *successful* ``/dirs`` listing (fail-closed) and is suspended wherever
deletions are not applied.
"""

import asyncio
from unittest.mock import patch

import pytest

from hsync_client import sync as sync_module


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    """Point the module CONFIG at an empty local bucket (bidirectional)."""
    monkeypatch.setattr(
        sync_module,
        "CONFIG",
        {
            "LOCAL_BUCKET": str(tmp_path),
            "SYNC_DIRECTION": "bidirectional",
            "DELETION_POLICY": "propagate",
        },
    )
    return tmp_path


def _listing(*paths: str) -> list[dict]:
    return [{"path": path, "permissions": "0755"} for path in paths]


class TestSweepEmptyLocalDirs:
    def test_removes_an_empty_dir_missing_remotely(self, bucket):
        (bucket / "gone").mkdir()

        assert sync_module.sweep_empty_local_dirs(_listing("kept")) == 1
        assert not (bucket / "gone").exists()

    def test_never_removes_a_dir_that_still_holds_content(self, bucket):
        (bucket / "gone").mkdir()
        (bucket / "gone" / "file.txt").write_text("x")

        assert sync_module.sweep_empty_local_dirs([]) == 0
        assert (bucket / "gone" / "file.txt").exists()

    def test_keeps_a_dir_that_still_exists_remotely(self, bucket):
        (bucket / "kept").mkdir()

        assert sync_module.sweep_empty_local_dirs(_listing("kept")) == 0
        assert (bucket / "kept").is_dir()

    def test_removes_nested_empty_dirs_bottom_up(self, bucket):
        (bucket / "a" / "b" / "c").mkdir(parents=True)

        assert sync_module.sweep_empty_local_dirs([]) == 3
        assert not (bucket / "a").exists()

    def test_never_removes_the_sync_root(self, bucket):
        assert sync_module.sweep_empty_local_dirs([]) == 0
        assert bucket.is_dir()

    def test_keeps_hidden_dirs(self, bucket):
        (bucket / ".hidden").mkdir()

        assert sync_module.sweep_empty_local_dirs([]) == 0
        assert (bucket / ".hidden").is_dir()

    def test_keep_policy_suspends_the_sweep(self, bucket, monkeypatch):
        monkeypatch.setitem(sync_module.CONFIG, "DELETION_POLICY", "keep")
        (bucket / "gone").mkdir()

        assert sync_module.sweep_empty_local_dirs([]) == 0
        assert (bucket / "gone").is_dir()

    def test_upload_only_suspends_the_sweep(self, bucket, monkeypatch):
        monkeypatch.setitem(sync_module.CONFIG, "SYNC_DIRECTION", "upload-only")
        (bucket / "gone").mkdir()

        assert sync_module.sweep_empty_local_dirs([]) == 0
        assert (bucket / "gone").is_dir()

    def test_dry_run_never_removes(self, bucket, monkeypatch):
        monkeypatch.setitem(sync_module.CONFIG, "DRYRUN", True)
        (bucket / "gone").mkdir()

        assert sync_module.sweep_empty_local_dirs([]) == 0
        assert (bucket / "gone").is_dir()

    def test_filter_path_limits_the_sweep(self, bucket, monkeypatch):
        monkeypatch.setitem(sync_module.CONFIG, "FILTER_PATH", "inside")
        (bucket / "inside" / "gone").mkdir(parents=True)
        (bucket / "inside" / "kept.txt").write_text("x")
        (bucket / "outside").mkdir()

        assert sync_module.sweep_empty_local_dirs([]) == 1
        assert not (bucket / "inside" / "gone").exists()
        assert (bucket / "outside").is_dir()

    def test_unsafe_remote_entries_are_ignored(self, bucket):
        """A traversal-shaped entry never protects a local directory."""
        (bucket / "gone").mkdir()

        assert sync_module.sweep_empty_local_dirs(_listing("../gone")) == 1
        assert not (bucket / "gone").exists()


class TestSweepHook:
    """The cycle only sweeps on a successful listing (fail-closed)."""

    @staticmethod
    def _cycle(tmp_path, listing, sweep):
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.get_files_local", return_value=[]),
            patch("hsync_client.sync.get_files_remote", return_value=[]),
            patch("hsync_client.sync.get_tombstones_remote", return_value={}),
            patch("hsync_client.sync.push_tombstones_remote"),
            patch("hsync_client.sync._cleanup_partial_files"),
            patch("hsync_client.sync.ensure_hsynckeep_files"),
            patch("hsync_client.sync.sync_state"),
            patch("hsync_client.sync._dir_converge_enabled", return_value=True),
            patch("hsync_client.sync.get_dirs_remote", return_value=listing),
            patch("hsync_client.sync.converge_directory_permissions"),
            patch("hsync_client.sync.sweep_empty_local_dirs", sweep),
        ):
            asyncio.run(sync_module.sync())

    def test_unavailable_listing_never_sweeps(self, tmp_path):
        from unittest.mock import MagicMock

        sweep = MagicMock()
        self._cycle(tmp_path, None, sweep)

        sweep.assert_not_called()

    def test_sweep_runs_before_the_directory_reconcile(self, tmp_path):
        """The listing predates the cycle's pushes, so the sweep comes first.

        Otherwise a shell removed here would be re-published to the peer by
        the create-only reconcile, and a folder the peer no longer has would
        be re-created on it instead of being swept.
        """
        order: list[str] = []
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.get_files_local", return_value=[]),
            patch("hsync_client.sync.get_files_remote", return_value=[]),
            patch("hsync_client.sync.get_tombstones_remote", return_value={}),
            patch("hsync_client.sync.push_tombstones_remote"),
            patch("hsync_client.sync._cleanup_partial_files"),
            patch("hsync_client.sync.ensure_hsynckeep_files"),
            patch("hsync_client.sync.sync_state"),
            patch("hsync_client.sync._dir_converge_enabled", return_value=True),
            patch("hsync_client.sync.get_dirs_remote", return_value=[]),
            patch("hsync_client.sync.converge_directory_permissions"),
            patch(
                "hsync_client.sync.sweep_empty_local_dirs",
                lambda *a, **k: order.append("sweep"),
            ),
            patch(
                "hsync_client.sync.reconcile_directories",
                lambda *a, **k: order.append("reconcile"),
            ),
        ):
            asyncio.run(sync_module.sync(reconcile_dirs=True))

        assert order == ["sweep", "reconcile"]

    def test_cycle_sweeps_with_a_successful_listing(self, tmp_path):
        from unittest.mock import MagicMock

        sweep = MagicMock()
        self._cycle(tmp_path, [], sweep)

        sweep.assert_called_once()
