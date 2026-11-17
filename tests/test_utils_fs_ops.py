# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Alberto Basso

"""Portable replace/remove/symlink helpers (Windows filesystem hazards)."""

from __future__ import annotations

import errno
import os
import stat

import pytest

import hsync_client.utils.fs_ops as fs_ops


class TestClearReadOnly:
    def test_posix_is_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fs_ops, "_IS_WINDOWS", False)
        target = tmp_path / "f.bin"
        target.write_bytes(b"x")
        os.chmod(target, 0o444)

        fs_ops.clear_read_only(str(target))

        assert stat.S_IMODE(target.stat().st_mode) == 0o444

    def test_windows_clears_the_read_only_attribute(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fs_ops, "_IS_WINDOWS", True)
        target = tmp_path / "f.bin"
        target.write_bytes(b"x")
        os.chmod(target, 0o444)

        fs_ops.clear_read_only(str(target))

        assert target.stat().st_mode & stat.S_IWRITE

    def test_missing_path_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fs_ops, "_IS_WINDOWS", True)

        fs_ops.clear_read_only(str(tmp_path / "absent"))


class TestRetries:
    def test_replace_retries_a_transient_sharing_violation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fs_ops, "_IS_WINDOWS", True)
        monkeypatch.setattr(fs_ops.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(fs_ops, "clear_read_only", lambda _path: None)
        source = tmp_path / "a.bin"
        destination = tmp_path / "b.bin"
        source.write_bytes(b"payload")
        real_replace = os.replace
        calls: list[int] = []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise OSError(errno.EACCES, "busy", None, 32)  # WinError 32
            real_replace(src, dst)

        monkeypatch.setattr(fs_ops.os, "replace", flaky)

        fs_ops.replace_with_retry(str(source), str(destination))

        assert len(calls) == 3
        assert destination.read_bytes() == b"payload"

    def test_replace_does_not_retry_a_missing_source(self, monkeypatch):
        monkeypatch.setattr(fs_ops, "_IS_WINDOWS", True)
        calls: list[int] = []

        def missing(_src, _dst):
            calls.append(1)
            raise FileNotFoundError(errno.ENOENT, "nope")

        monkeypatch.setattr(fs_ops.os, "replace", missing)

        with pytest.raises(FileNotFoundError):
            fs_ops.replace_with_retry("a", "b")
        assert len(calls) == 1

    def test_replace_raises_after_the_bounded_attempts(self, monkeypatch):
        monkeypatch.setattr(fs_ops, "_IS_WINDOWS", True)
        monkeypatch.setattr(fs_ops.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(fs_ops, "clear_read_only", lambda _path: None)
        calls: list[int] = []

        def always_busy(_src, _dst):
            calls.append(1)
            raise OSError(errno.EACCES, "busy", None, 32)

        monkeypatch.setattr(fs_ops.os, "replace", always_busy)

        with pytest.raises(OSError):
            fs_ops.replace_with_retry("a", "b")
        assert len(calls) == fs_ops._ATTEMPTS

    def test_remove_retries_then_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fs_ops, "_IS_WINDOWS", True)
        monkeypatch.setattr(fs_ops.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(fs_ops, "clear_read_only", lambda _path: None)
        target = tmp_path / "f.bin"
        target.write_bytes(b"x")
        real_remove = os.remove
        calls: list[int] = []

        def flaky(path):
            calls.append(1)
            if len(calls) < 2:
                raise OSError(errno.EACCES, "busy", None, 32)
            real_remove(path)

        monkeypatch.setattr(fs_ops.os, "remove", flaky)

        fs_ops.remove_with_retry(str(target))

        assert len(calls) == 2
        assert not target.exists()


class TestSymlinkSkip:
    def test_supported_host_creates_the_link(self, tmp_path):
        if os.name == "nt":
            pytest.skip("needs symlink privilege on Windows")
        target = tmp_path / "target.bin"
        target.write_bytes(b"x")
        link = tmp_path / "link.bin"

        assert fs_ops.create_symlink_or_skip(str(target), str(link)) is True
        assert link.is_symlink()

    def test_windows_host_without_privilege_skips_instead_of_failing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fs_ops, "_IS_WINDOWS", True)

        def denied(_target, _link):
            raise OSError(errno.EPERM, "A required privilege is not held", None, 1314)

        monkeypatch.setattr(fs_ops.os, "symlink", denied)

        assert fs_ops.create_symlink_or_skip("t", str(tmp_path / "l")) is False

    def test_posix_permission_error_still_propagates(self, tmp_path, monkeypatch):
        """EACCES/EPERM on POSIX are real failures, not 'no symlink support'."""
        monkeypatch.setattr(fs_ops, "_IS_WINDOWS", False)

        def denied(_target, _link):
            raise OSError(errno.EACCES, "permission denied")

        monkeypatch.setattr(fs_ops.os, "symlink", denied)

        with pytest.raises(OSError):
            fs_ops.create_symlink_or_skip("t", str(tmp_path / "l"))

    def test_posix_filesystem_without_link_support_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fs_ops, "_IS_WINDOWS", False)

        def unsupported(_target, _link):
            raise OSError(errno.ENOTSUP, "operation not supported")

        monkeypatch.setattr(fs_ops.os, "symlink", unsupported)

        assert fs_ops.create_symlink_or_skip("t", str(tmp_path / "l")) is False

    def test_unrelated_errors_still_propagate(self, tmp_path, monkeypatch):
        def broken(_target, _link):
            raise OSError(errno.ENOENT, "missing parent")

        monkeypatch.setattr(fs_ops.os, "symlink", broken)

        with pytest.raises(OSError):
            fs_ops.create_symlink_or_skip("t", str(tmp_path / "l"))
