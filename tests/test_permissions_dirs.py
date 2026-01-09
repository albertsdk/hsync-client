# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for the download-only directory permission converge.

``converge_directory_permissions`` applies the remote directory mode when
the local mode diverges (download-only: remote wins), with the owner rwx
floor preserved. ``update_dir`` applies the same floor for realtime events.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from hsync_client.sync import converge_directory_permissions


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
