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

    def test_a_local_winner_keeps_a_remote_counterpart(self, tmp_path):
        """The master lists only the other spelling: the local winner must keep
        a remote counterpart, otherwise it looks local-only and a download-only
        mirror deletes it."""
        local = [{"filepath": str(tmp_path / "a.txt")}]
        remote = [{"filepath": "A.txt", "name": "A.txt", "size": 3}]
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.is_case_insensitive", return_value=True),
        ):
            local_out, remote_out = _drop_case_collisions(local, remote)

        assert [item["filepath"] for item in local_out] == [str(tmp_path / "a.txt")]
        assert [item["filepath"] for item in remote_out] == ["a.txt"]
        assert remote_out[0]["name"] == "a.txt"
        assert remote_out[0]["size"] == 3

    def test_a_persistent_collision_is_logged_once(self, tmp_path, monkeypatch):
        remote = [{"filepath": "A.txt"}, {"filepath": "a.txt"}]
        monkeypatch.setattr("hsync_client.sync._collision_warned", set())
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.is_case_insensitive", return_value=True),
            patch("hsync_client.sync.log") as mock_log,
        ):
            _drop_case_collisions([], remote)
            _drop_case_collisions([], remote)

        assert mock_log.warning.call_count == 1

    def test_case_sensitive_filesystem_is_left_alone(self, tmp_path):
        remote = [{"filepath": "A.txt"}, {"filepath": "a.txt"}]
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.is_case_insensitive", return_value=False),
        ):
            local_out, remote_out = _drop_case_collisions([], remote)

        assert local_out == []
        assert remote_out == remote

    def test_normalization_twins_collide_even_on_a_case_sensitive_filesystem(self, tmp_path):
        """NFC and NFD spellings share one identity: only one can be reconciled."""
        import unicodedata

        nfc = "caf\u00e9.txt"
        nfd = unicodedata.normalize("NFD", nfc)
        assert nfc != nfd
        remote = [{"filepath": nfc}, {"filepath": nfd}]
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.is_case_insensitive", return_value=False),
        ):
            _local, remote_out = _drop_case_collisions([], remote)

        assert [item["filepath"] for item in remote_out] == [min(nfc, nfd)]


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
        assert not (tmp_path / "old.txt.hsync-rename.part").exists()

    def test_local_rename_resolves_a_relative_new_path_inside_the_bucket(
        self, tmp_path, monkeypatch
    ):
        """Production pairs mix forms: the vacated path is absolute, the new one
        is bucket-relative. The move must address the bucket, never the CWD."""
        source = tmp_path / "old.txt"
        source.write_text("content", encoding="utf-8")
        old_file = File.get(str(source))
        assert old_file is not None
        new_file = File(
            filepath="sub/renamed.txt",  # bucket-relative, as in a remote listing
            ctime=old_file.ctime,
            mtime=old_file.mtime,
            size=old_file.size,
            sha256sum=old_file.sha256sum,
            permissions=old_file.permissions,
        )
        recorded: list[str] = []
        monkeypatch.setattr(
            "hsync_client.sync.sync_state.merge_and_record",
            lambda path, *args: recorded.append(path),
        )
        monkeypatch.setattr("hsync_client.sync.sync_state.clear_base", lambda _path: None)
        monkeypatch.setattr("hsync_client.sync.is_case_insensitive", lambda _root: False)

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            assert _apply_local_rename(old_file, new_file) is True

        moved = tmp_path / "sub" / "renamed.txt"
        assert moved.read_text(encoding="utf-8") == "content"
        assert recorded == [str(moved)]


class TestInterruptedRenameRecovery:
    """A crash between the two renames of a case-only move must not lose bytes."""

    def test_the_sweep_restores_a_stranded_rename_marker(self, tmp_path):
        from hsync_client.sync import _cleanup_partial_files

        stranded = tmp_path / "a.txt.hsync-rename.part"
        stranded.write_text("content", encoding="utf-8")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            removed = _cleanup_partial_files()

        assert removed == 0
        assert not stranded.exists()
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "content"

    def test_the_sweep_deletes_a_marker_whose_target_still_exists(self, tmp_path):
        from hsync_client.sync import _cleanup_partial_files

        (tmp_path / "a.txt").write_text("new", encoding="utf-8")
        stranded = tmp_path / "a.txt.hsync-rename.part"
        stranded.write_text("old", encoding="utf-8")

        with patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}):
            removed = _cleanup_partial_files()

        assert removed == 1
        assert not stranded.exists()
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "new"


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

    def test_download_skips_when_the_partial_name_exceeds_the_limit(self, tmp_path):
        """The gate must cover the .part temporary, not just the final path."""
        from hsync_client.transfer import download_file

        file = File(filepath="x.bin", sha256sum="a" * 64, size=3)

        def length_ok(_base, rel):
            return not rel.endswith(PARTIAL_DOWNLOAD_SUFFIX)

        with (
            patch("hsync_client.transfer.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.transfer.local_path_length_ok", side_effect=length_ok),
            patch("hsync_client.transfer.get") as mock_get,
        ):
            assert download_file(file) is True  # skipped, not a failure
        mock_get.assert_not_called()

    def test_download_skips_when_the_parked_payload_name_exceeds_the_limit(self, tmp_path):
        """The longest transient is the parked literal payload."""
        from hsync_client.transfer import download_file

        file = File(filepath="x.bin", sha256sum="a" * 64, size=3)

        def length_ok(_base, rel):
            return not rel.endswith(".delta.part.lit")

        with (
            patch("hsync_client.transfer.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.transfer.local_path_length_ok", side_effect=length_ok),
            patch("hsync_client.transfer.get") as mock_get,
        ):
            assert download_file(file) is True  # skipped, not a failure
        mock_get.assert_not_called()


class TestDirectoryPermissionConverge:
    def test_lossy_host_skips_a_directory_permission_divergence(self, tmp_path):
        """A lossy host cannot represent 555 dirs; converging would ping-pong."""
        from hsync_client.sync import converge_directory_permissions

        (tmp_path / "d").mkdir()
        remote_dirs = [{"path": "d", "permissions": "555"}]
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.local_permissions_are_lossy", return_value=True),
            patch("hsync_client.sync.set_permissions_on_dir") as mock_set,
        ):
            converge_directory_permissions(remote_dirs)

        mock_set.assert_not_called()


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
