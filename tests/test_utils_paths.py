# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Alberto Basso

"""Tests for hsync_client.utils.paths."""

import os

from hsync_client.utils.paths import normalize_bucket_relpath


class TestNormalizeBucketRelpath:
    def test_absolute_path_is_relativised(self, tmp_path):
        target = tmp_path / "sub" / "f.txt"
        assert normalize_bucket_relpath(str(target), str(tmp_path)) == "sub/f.txt"

    def test_relative_path_is_kept(self, tmp_path):
        assert normalize_bucket_relpath("sub/f.txt", str(tmp_path)) == "sub/f.txt"

    def test_sibling_prefix_is_not_stripped(self, tmp_path):
        """Component-aware: ``/base2`` is not under ``/base``."""
        sibling = tmp_path / "base2" / "x"
        assert normalize_bucket_relpath(str(sibling), str(tmp_path / "base")) is None

    def test_path_outside_the_bucket_is_rejected(self, tmp_path):
        outside = tmp_path.parent / "elsewhere" / "f.txt"
        assert normalize_bucket_relpath(str(outside), str(tmp_path)) is None

    def test_windows_mixed_separators_become_posix(self, monkeypatch):
        """A nested Windows path is valid on the wire only after folding ``\\``.

        Regression: ``relpath_in_bucket`` rejoins with ``os.sep`` and
        ``validate_remote_relpath`` rejects backslashes, so the old code returned
        ``None`` and the upload path stayed absolute (HSYNC-1002 on the server).
        """
        monkeypatch.setattr(os, "sep", "\\")
        root = "C:/Users/test-win\\HSYNC"
        assert normalize_bucket_relpath(f"{root}\\sub\\f.txt", root) == "sub/f.txt"


class TestCaseSensitivityProbe:
    """The case-insensitivity probe must reflect the filesystem, not the OS."""

    def test_probe_matches_the_filesystem_behavior(self, tmp_path):
        import hsync_client.utils.paths as paths

        (tmp_path / "ProbeCase").write_text("", encoding="utf-8")
        expected = (tmp_path / "probecase").exists()
        paths._case_sensitivity_cache.clear()

        assert paths.is_case_insensitive(str(tmp_path)) is expected

    def test_probe_is_cached_per_root(self, tmp_path, monkeypatch):
        import hsync_client.utils.paths as paths

        paths._case_sensitivity_cache.clear()
        first = paths.is_case_insensitive(str(tmp_path))

        def _boom(*_args, **_kwargs):
            raise AssertionError("the probe must not run twice for the same root")

        monkeypatch.setattr(paths.Path, "write_text", _boom)
        assert paths.is_case_insensitive(str(tmp_path)) is first


class TestPathLengthLimit:
    def test_posix_has_no_total_path_limit(self, monkeypatch):
        import hsync_client.utils.paths as paths

        monkeypatch.setattr(paths, "_IS_WINDOWS", False)
        assert paths.absolute_path_length_ok("a" * 5000) is True

    def test_windows_boundary(self, monkeypatch):
        import hsync_client.utils.paths as paths

        monkeypatch.setattr(paths, "_IS_WINDOWS", True)
        assert paths.absolute_path_length_ok("a" * paths._WINDOWS_MAX_PATH) is True
        assert paths.absolute_path_length_ok("a" * (paths._WINDOWS_MAX_PATH + 1)) is False

    def test_local_path_length_uses_the_absolute_form(self, monkeypatch):
        import hsync_client.utils.paths as paths

        monkeypatch.setattr(paths, "_IS_WINDOWS", True)
        base = "C:/bucket"
        fits = "d/" + "f" * (paths._WINDOWS_MAX_PATH - len(base) - 3)
        assert paths.local_path_length_ok(base, fits) is True
        assert paths.local_path_length_ok(base, fits + "f") is False
