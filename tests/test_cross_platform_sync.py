# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Alberto Basso

"""Cross-platform parity: case-insensitive filesystems, long paths, symlinks.

Windows/NTFS and macOS/APFS differ from Linux in ways that can break or thrash a
sync: case-insensitive names, a total path limit that does not exist on POSIX,
and symlink creation requiring a privilege. These tests pin the platform-aware
handling (the OS branch is driven by module constants, so they run anywhere).
"""

from __future__ import annotations

from unittest.mock import patch

from hsync_common.constants import PARTIAL_DOWNLOAD_SUFFIX
from hsync_common.models.file import File
from hsync_common.models.symlink import Symlink

from hsync_client.sync import (
    _apply_local_rename,
    _drop_case_collisions,
    _is_case_only_rename,
)


class TestCaseCollisions:
    """Two remote paths that differ only by case cannot both exist locally."""

    def test_remote_only_collision_keeps_the_smallest_path(self, tmp_path):
        remote = [{"filepath": "A.txt"}, {"filepath": "a.txt"}]
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.is_case_insensitive", return_value=True),
        ):
            local_out, remote_out = _drop_case_collisions([], remote)

        assert local_out == []
        assert [item["filepath"] for item in remote_out] == ["A.txt"]

    def test_an_existing_local_path_wins_over_the_remote(self, tmp_path):
        local = [{"filepath": str(tmp_path / "a.txt")}]
        remote = [{"filepath": "A.txt"}, {"filepath": "a.txt"}]
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.is_case_insensitive", return_value=True),
        ):
            local_out, remote_out = _drop_case_collisions(local, remote)

        # The local copy is never deleted/overwritten because of a collision.
        assert [item["filepath"] for item in local_out] == [str(tmp_path / "a.txt")]
        assert [item["filepath"] for item in remote_out] == ["a.txt"]

    def test_case_sensitive_filesystem_is_left_alone(self, tmp_path):
        remote = [{"filepath": "A.txt"}, {"filepath": "a.txt"}]
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.is_case_insensitive", return_value=False),
        ):
            local_out, remote_out = _drop_case_collisions([], remote)

        assert local_out == []
        assert remote_out == remote


class TestCaseOnlyRename:
    def test_detects_a_case_only_pair(self, monkeypatch):
        monkeypatch.setattr("hsync_client.sync.is_case_insensitive", lambda _root: True)

        assert _is_case_only_rename("/b/Old.txt", "/b/old.txt") is True
        assert _is_case_only_rename("/b/Old.txt", "/b/new.txt") is False
        assert _is_case_only_rename("/b/same.txt", "/b/same.txt") is False

    def test_case_sensitive_filesystem_is_not_two_stepped(self, monkeypatch):
        monkeypatch.setattr("hsync_client.sync.is_case_insensitive", lambda _root: False)

        assert _is_case_only_rename("/b/Old.txt", "/b/old.txt") is False

    def test_local_rename_goes_through_a_skipped_partial(self, tmp_path, monkeypatch):
        """On a case-insensitive FS the two names are the same file to the OS."""
        source = tmp_path / "Old.txt"
        source.write_text("content", encoding="utf-8")
        destination = tmp_path / "old.txt"
        old_file = File.get(str(source))
        assert old_file is not None
        new_file = File(
            filepath=str(destination),
            ctime=old_file.ctime,
            mtime=old_file.mtime,
            size=old_file.size,
            sha256sum=old_file.sha256sum,
            permissions=old_file.permissions,
        )
        monkeypatch.setattr("hsync_client.sync.is_case_insensitive", lambda _root: True)

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            assert _apply_local_rename(old_file, new_file) is True

        assert destination.exists()
        assert destination.read_text(encoding="utf-8") == "content"
        assert not (tmp_path / f"old.txt{PARTIAL_DOWNLOAD_SUFFIX}").exists()


class TestLongPath:
    def test_download_skips_a_path_over_the_platform_limit(self, tmp_path):
        from hsync_client.transfer import download_file

        file = File(filepath="deep/file.bin", sha256sum="a" * 64, size=3)
        with (
            patch("hsync_client.transfer.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.transfer.local_path_length_ok", return_value=False),
            patch("hsync_client.transfer.get") as mock_get,
        ):
            assert download_file(file) is True  # skipped, not a failure
        mock_get.assert_not_called()


class TestSymlinkPrivilege:
    def test_materializing_skips_when_the_host_cannot_create_links(self, tmp_path):
        from hsync_client.transfer import materialize_symlink_local

        target = tmp_path / "target.bin"
        target.write_text("x", encoding="utf-8")
        link = Symlink(filepath="link.bin", target="target.bin")
        with (
            patch("hsync_client.transfer.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch(
                "hsync_client.transfer.create_symlink_or_skip", return_value=False
            ) as mock_create,
        ):
            assert materialize_symlink_local(link) is False
        mock_create.assert_called_once()
        assert not (tmp_path / "link.bin").exists()
