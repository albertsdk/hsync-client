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
