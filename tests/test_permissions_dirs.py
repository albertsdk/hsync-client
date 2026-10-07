# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the directory permission converge.

``converge_directory_permissions`` applies the authoritative peer's directory
mode when the local mode diverges (the remote mode wins), with the owner rwx
floor preserved. ``update_dir`` applies the same floor for realtime events.
``_dir_converge_enabled`` is the per-cycle gate, and ``sync`` runs the
converge after the transfers so directories created by the download path are
repaired in the same cycle.
"""

import asyncio
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hsync_common.models.file import File
from hsync_common.models.sync_action import SyncAction, SyncActionName

from hsync_client.sync import (
    _dir_converge_enabled,
    converge_directory_permissions,
    sync,
)


class TestConvergeDirectoryPermissions:
    def test_applies_remote_mode_on_divergence(self, tmp_path):
        from hsync_client import sync as sync_mod

        sub = tmp_path / "sub" / "deep"
        sub.mkdir(parents=True)
        os.chmod(sub, 0o755)

        with patch.object(sync_mod, "CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            with patch.object(sync_mod, "Dir") as mock_dir_cls:
                local_dir = MagicMock()
                local_dir.path = str(sub)
                local_dir.permissions = "755"
                mock_dir_cls.get.return_value = local_dir

                converge_directory_permissions([{"path": "sub/deep", "permissions": "700"}])

        # Remote wins: 700 applied, owner-floor preserved (0o700 ∩ floor = 0o700)
        assert oct(Path(sub).stat().st_mode & 0o777) == "0o700"

    def test_skips_matching_permissions(self, tmp_path):
        from hsync_client import sync as sync_mod

        sub = tmp_path / "matching"
        sub.mkdir(parents=True)
        os.chmod(sub, 0o700)

        with patch.object(sync_mod, "CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            with patch.object(sync_mod, "Dir") as mock_dir_cls:
                local_dir = MagicMock()
                local_dir.path = str(sub)
                local_dir.permissions = "700"
                mock_dir_cls.get.return_value = local_dir

                with patch.object(sync_mod, "validate_remote_relpath", return_value="matching"):
                    with patch.object(sync_mod, "is_hidden_dir", return_value=False):
                        with patch.object(sync_mod, "set_permissions_on_dir") as mock_set:
                            converge_directory_permissions(
                                [{"path": "matching", "permissions": "700"}]
                            )

        mock_set.assert_not_called()

    def test_strips_special_bits(self, tmp_path):
        from hsync_client import sync as sync_mod

        sub = tmp_path / "special"
        sub.mkdir(parents=True)
        os.chmod(sub, 0o755)

        with patch.object(sync_mod, "CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            with patch.object(sync_mod, "Dir") as mock_dir_cls:
                local_dir = MagicMock()
                local_dir.path = str(sub)
                local_dir.permissions = "755"
                mock_dir_cls.get.return_value = local_dir

                # Remote declares 4755: the special bit must never be applied.
                converge_directory_permissions([{"path": "special", "permissions": "4755"}])

        assert oct(Path(sub).stat().st_mode & 0o7777) == "0o755"

    def test_missing_local_dir_is_skipped(self, tmp_path):
        from hsync_client import sync as sync_mod

        with patch.object(sync_mod, "CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            with patch.object(sync_mod, "Dir") as mock_dir_cls:
                mock_dir_cls.get.return_value = None
                with patch.object(sync_mod, "set_permissions_on_dir") as mock_set:
                    converge_directory_permissions([{"path": "ghost", "permissions": "700"}])

        mock_set.assert_not_called()

    def test_unsafe_path_entry_is_skipped(self, tmp_path):
        from hsync_client import sync as sync_mod

        with patch.object(sync_mod, "CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            with patch.object(sync_mod, "Dir") as mock_dir_cls:
                mock_dir_cls.get.return_value = None
                with patch.object(sync_mod, "set_permissions_on_dir") as mock_set:
                    converge_directory_permissions([{"path": "../escape", "permissions": "700"}])

        mock_set.assert_not_called()


class TestUpdateDirOwnerFloor:
    def test_update_dir_applies_owner_floor(self, tmp_path):
        """update_dir on an existing directory with divergent permissions must
        apply the mode with the owner rwx floor."""
        from hsync_client import sync as sync_mod

        existing = tmp_path / "d"
        existing.mkdir()
        os.chmod(existing, 0o755)

        payload = {"path": "d", "permissions": "000"}

        with patch.object(sync_mod, "CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = sync_mod.update_dir(payload)

        assert result is not None
        assert oct(Path(existing).stat().st_mode & 0o777) == "0o700"

    def test_update_dir_creation_applies_permissions(self, tmp_path):
        from hsync_client import sync as sync_mod

        payload = {"path": "newdir", "permissions": "750"}

        with patch.object(sync_mod, "CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            result = sync_mod.update_dir(payload)

        assert result is not None
        created = tmp_path / "newdir"
        assert created.is_dir()
        assert oct(created.stat().st_mode & 0o777) == "0o750"


class TestDirConvergeGate:
    """``_dir_converge_enabled`` decides which cycles reconcile remote modes.

    Regression for the 6000-file P2P stress divergence: the converge gate used
    to be ``SYNC_DIRECTION == "download-only"``, so a P2P slave never
    reconciled directory modes from the master. A directory whose live create
    event was dropped while the WSS was down during a burst then kept the
    download path's ``mkdir`` default mode forever.
    """

    @pytest.mark.parametrize(
        ("p2p_mode", "direction", "expected"),
        [
            # P2P: the master owns the whole tree, so its modes always win.
            (True, "bidirectional", True),
            (True, "download-only", True),
            # ... except when this node pushes content and takes nothing back.
            (True, "upload-only", False),
            # Remote server: mirroring is the contract only in download-only.
            (False, "download-only", True),
            (False, "bidirectional", False),
            (False, "upload-only", False),
        ],
    )
    def test_gate(self, p2p_mode, direction, expected):
        from hsync_client import sync as sync_mod

        with patch.object(sync_mod, "CONFIG", {"SYNC_DIRECTION": direction}):
            assert _dir_converge_enabled(p2p_mode) is expected

    def test_missing_direction_defaults_to_bidirectional(self):
        """An unset SYNC_DIRECTION must behave like the explicit default."""
        from hsync_client import sync as sync_mod

        with patch.object(sync_mod, "CONFIG", {}):
            assert _dir_converge_enabled(False) is False
            assert _dir_converge_enabled(True) is True


class TestSyncRunsDirConverge:
    """``sync`` reconciles directory modes *after* the transfers complete.

    The download path creates missing parents with ``mkdir`` defaults, so a
    converge running before the transfers would silently skip exactly the
    directories the current cycle is about to create (the local-dir-missing
    branch of ``converge_directory_permissions``).
    """

    @staticmethod
    def _run_cycle(
        tmp_path: Path, *, p2p_mode: bool, direction: str
    ) -> tuple[list[str], MagicMock]:
        """Run one sync cycle with a single (mocked) download action.

        Returns the observed call order of ``_execute_parallel_transfers`` /
        ``converge_directory_permissions`` plus the converge mock.
        """
        from hsync_client import sync as sync_mod

        order: list[str] = []
        action = SyncAction(File(str(tmp_path / "file.txt")), SyncActionName.download)
        transfers = AsyncMock(side_effect=lambda *a, **k: order.append("transfers"))
        converge = MagicMock(side_effect=lambda *a, **k: order.append("converge"))

        async def _cycle() -> None:
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(
                        sync_mod,
                        "CONFIG",
                        {"LOCAL_BUCKET": str(tmp_path), "SYNC_DIRECTION": direction},
                    )
                )
                stack.enter_context(patch.object(sync_mod, "get_files_local", return_value=[]))
                stack.enter_context(patch.object(sync_mod, "get_files_remote", return_value=[]))
                stack.enter_context(
                    patch.object(sync_mod, "get_tombstones_remote", return_value={})
                )
                stack.enter_context(patch.object(sync_mod, "push_tombstones_remote"))
                stack.enter_context(
                    patch.object(sync_mod, "get_sync_actions", return_value=[action])
                )
                stack.enter_context(patch.object(sync_mod, "_cleanup_partial_files"))
                stack.enter_context(patch.object(sync_mod, "sync_state"))
                stack.enter_context(patch.object(sync_mod, "ensure_hsynckeep_files"))
                stack.enter_context(
                    patch.object(sync_mod, "_execute_parallel_transfers", new=transfers)
                )
                stack.enter_context(
                    patch.object(sync_mod, "converge_directory_permissions", new=converge)
                )
                stack.enter_context(patch.object(sync_mod, "get_dirs_remote", return_value=[]))
                await sync(p2p_mode=p2p_mode)

        asyncio.run(_cycle())
        return order, converge

    def test_p2p_cycle_converges_after_transfers(self, tmp_path):
        order, converge = self._run_cycle(tmp_path, p2p_mode=True, direction="bidirectional")

        converge.assert_called_once()
        assert order == ["transfers", "converge"], (
            f"the P2P converge must run once, after the transfers: {order}"
        )

    def test_remote_download_only_converges_after_transfers(self, tmp_path):
        order, converge = self._run_cycle(tmp_path, p2p_mode=False, direction="download-only")

        converge.assert_called_once()
        assert order == ["transfers", "converge"], order

    def test_remote_bidirectional_never_converges(self, tmp_path):
        order, converge = self._run_cycle(tmp_path, p2p_mode=False, direction="bidirectional")

        converge.assert_not_called()
        assert order == ["transfers"], order
