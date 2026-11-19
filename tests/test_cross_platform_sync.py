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
    """The index is case-sensitive; a folding filesystem is not.

    One remote spelling converges the local spelling (a case-only rename of the
    same file); real twins are left alone and reported.
    """

    @staticmethod
    def _paths(items: list[dict]) -> list[str]:
        return [item["filepath"] for item in items]

    def test_one_remote_spelling_converges_the_local_case(self, tmp_path):
        local_file = tmp_path / "foo.txt"
        local_file.write_text("content", encoding="utf-8")

        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.is_case_insensitive", return_value=True),
            patch("hsync_client.sync.emit_warning") as warn,
        ):
            local_out, remote_out = _drop_case_collisions(
                [{"filepath": str(local_file), "name": "foo.txt"}],
                [{"filepath": "Foo.txt", "name": "Foo.txt", "size": 7}],
            )

        # The deterministic winner is "Foo.txt"; the file is renamed in place, so
        # every peer ends up with the same name and the reconcile continues.
        assert not local_file.exists()
        assert (tmp_path / "Foo.txt").read_text(encoding="utf-8") == "content"
        assert self._paths(local_out) == [str(tmp_path / "Foo.txt")]
        assert self._paths(remote_out) == ["Foo.txt"]
        warn.assert_not_called()

    def test_the_winner_does_not_depend_on_which_side_is_local(self, tmp_path):
        for local_name, remote_name in (("foo.txt", "Foo.txt"), ("Foo.txt", "foo.txt")):
            local_file = tmp_path / local_name
            local_file.write_text("x", encoding="utf-8")
            with (
                patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
                patch("hsync_client.sync.is_case_insensitive", return_value=True),
            ):
                local_out, _remote_out = _drop_case_collisions(
                    [{"filepath": str(local_file)}], [{"filepath": remote_name}]
                )
            assert self._paths(local_out) == [str(tmp_path / "Foo.txt")]
            (tmp_path / "Foo.txt").unlink()

    def test_real_twins_are_left_alone_and_reported(self, tmp_path):
        local_file = tmp_path / "a.txt"
        local_file.write_text("local", encoding="utf-8")

        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.is_case_insensitive", return_value=True),
            patch("hsync_client.sync.emit_warning") as warn,
        ):
            local_out, remote_out = _drop_case_collisions(
                [{"filepath": str(local_file)}],
                [{"filepath": "A.txt"}, {"filepath": "a.txt"}],
            )

        # Nothing is renamed, overwritten or deleted: the group is reported.
        assert local_out == []
        assert remote_out == []
        assert local_file.read_text(encoding="utf-8") == "local"
        warn.assert_called_once()
        assert warn.call_args.kwargs["source"] == "case-conflict"

    def test_a_conflict_free_cycle_retracts_the_report(self, tmp_path):
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.is_case_insensitive", return_value=True),
            patch("hsync_client.sync.emit_warning") as warn,
            patch("hsync_client.sync.emit_warning_cleared") as cleared,
        ):
            _drop_case_collisions([{"filepath": str(tmp_path / "a.txt")}], [])

        warn.assert_not_called()
        cleared.assert_called_once_with("case-conflict")

    def test_case_sensitive_filesystem_is_left_alone(self, tmp_path):
        remote = [{"filepath": "A.txt"}, {"filepath": "a.txt"}]
        with (
            patch("hsync_client.sync.CONFIG", {"LOCAL_BUCKET": str(tmp_path)}),
            patch("hsync_client.sync.is_case_insensitive", return_value=False),
        ):
            local_out, remote_out = _drop_case_collisions([], remote)

        assert local_out == []
        # Distinct files on a case-sensitive filesystem: no collision grouping.
        assert remote_out == remote

    def test_normalization_twins_prefer_the_nfc_spelling(self, tmp_path):
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

        assert [item["filepath"] for item in remote_out] == [nfc]


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
