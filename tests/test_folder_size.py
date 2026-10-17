# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the aggregate folder-size event (local + sync-set total)."""

from __future__ import annotations

import hsync_client.sync as sync_mod
from hsync_client.sync import emit_folder_size, folder_size_totals


class TestFolderSizeTotals:
    def test_union_adds_local_only_files(self):
        local = [
            {"filepath": "a.bin", "size": 10},
            {"filepath": "b.bin", "size": 20},
        ]
        remote = [
            {"filepath": "a.bin", "size": 15},
            {"filepath": "c.bin", "size": 5},
        ]

        local_bytes, total_bytes = folder_size_totals(local, remote)

        assert local_bytes == 30
        # Remote size wins for "a.bin"; local-only "b.bin" is pending upload.
        assert total_bytes == 15 + 5 + 20

    def test_symlinks_and_sentinels_are_ignored(self):
        local = [
            {"filepath": "link", "type": "symlink"},
            {"filepath": ".hsynckeep", "size": 4},
            {"filepath": "a.bin", "size": 3},
        ]
        remote = [
            {"filepath": "remote-link", "type": "symlink"},
            {"filepath": ".hsynckeep", "size": 4},
        ]

        assert folder_size_totals(local, remote) == (3, 3)

    def test_missing_sizes_are_zero(self):
        assert folder_size_totals([{"filepath": "a"}], []) == (0, 0)

    def test_empty_listings(self):
        assert folder_size_totals([], []) == (0, 0)


class TestEmitFolderSize:
    def test_emits_the_pair(self, monkeypatch):
        monkeypatch.delitem(sync_mod.CONFIG, "FILTER_PATH", raising=False)
        monkeypatch.setattr(sync_mod, "_last_folder_size", None)
        calls: list[dict] = []
        monkeypatch.setattr(sync_mod, "emit_event", lambda *a, **k: calls.append(k))

        emit_folder_size([{"filepath": "a", "size": 10}], [{"filepath": "b", "size": 5}])

        assert calls == [{"local_bytes": 10, "total_bytes": 15}]

    def test_identical_values_are_deduplicated(self, monkeypatch):
        monkeypatch.delitem(sync_mod.CONFIG, "FILTER_PATH", raising=False)
        monkeypatch.setattr(sync_mod, "_last_folder_size", None)
        calls: list[dict] = []
        monkeypatch.setattr(sync_mod, "emit_event", lambda *a, **k: calls.append(k))
        local = [{"filepath": "a", "size": 10}]

        emit_folder_size(local, [])
        emit_folder_size(local, [])
        emit_folder_size([*local, {"filepath": "b", "size": 1}], [])

        assert len(calls) == 2
        assert calls[1] == {"local_bytes": 11, "total_bytes": 11}

    def test_scoped_runs_do_not_emit(self, monkeypatch):
        monkeypatch.setitem(sync_mod.CONFIG, "FILTER_PATH", "sub")
        calls: list[dict] = []
        monkeypatch.setattr(sync_mod, "emit_event", lambda *a, **k: calls.append(k))

        emit_folder_size([{"filepath": "a", "size": 10}], [])

        assert calls == []
