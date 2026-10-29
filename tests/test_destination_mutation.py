# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the deletion-marker snapshot and the commit gate built on it.

``deletion_marker_snapshot`` / ``destination_mutation_since`` guard a transfer's
final move against a destination that was deleted, renamed away or re-created
while the transfer was running.
"""

from pathlib import Path

from hsync_client import config as client_config
from hsync_client.tombstones import (
    clear_deletion,
    deletion_marker_snapshot,
    destination_mutation_since,
    record_deletion,
)


def _use_bucket(tmp_path, monkeypatch) -> Path:
    """Point the runtime bucket at a temp dir and return it."""
    bucket = tmp_path / "bucket"
    bucket.mkdir()
    monkeypatch.setitem(client_config.CONFIG, "LOCAL_BUCKET", str(bucket))
    return bucket


class TestDeletionMarkerSnapshot:
    """The snapshot is None without a marker and stable until it changes."""

    def test_absent_marker_returns_none(self, tmp_path, monkeypatch):
        bucket = _use_bucket(tmp_path, monkeypatch)
        assert deletion_marker_snapshot(str(bucket / "a.bin")) is None

    def test_marker_is_stable_and_cleared(self, tmp_path, monkeypatch):
        bucket = _use_bucket(tmp_path, monkeypatch)
        target = bucket / "a.bin"
        target.write_bytes(b"x")

        record_deletion(str(target), origin_cid="testcid")
        first = deletion_marker_snapshot(str(target))
        second = deletion_marker_snapshot(str(target))
        assert first is not None
        assert first == second

        clear_deletion(str(target))
        assert deletion_marker_snapshot(str(target)) is None


class TestDestinationMutationSince:
    """The gate fires exactly when the destination changed under the transfer."""

    def test_unchanged_destination_is_not_a_mutation(self, tmp_path, monkeypatch):
        bucket = _use_bucket(tmp_path, monkeypatch)
        target = bucket / "a.bin"
        target.write_bytes(b"x")
        snapshot = deletion_marker_snapshot(str(target))
        assert not destination_mutation_since(
            str(target), existed_at_start=True, marker_snapshot=snapshot
        )

    def test_new_download_of_an_untouched_path_is_not_a_mutation(self, tmp_path, monkeypatch):
        bucket = _use_bucket(tmp_path, monkeypatch)
        target = bucket / "a.bin"
        assert not destination_mutation_since(
            str(target), existed_at_start=False, marker_snapshot=None
        )

    def test_a_deleted_destination_is_a_mutation(self, tmp_path, monkeypatch):
        bucket = _use_bucket(tmp_path, monkeypatch)
        target = bucket / "a.bin"
        target.write_bytes(b"x")
        target.unlink()
        assert destination_mutation_since(str(target), existed_at_start=True, marker_snapshot=None)

    def test_delete_and_recreate_is_a_mutation(self, tmp_path, monkeypatch):
        bucket = _use_bucket(tmp_path, monkeypatch)
        target = bucket / "a.bin"
        target.write_bytes(b"old")
        snapshot = deletion_marker_snapshot(str(target))  # None
        target.unlink()
        target.write_bytes(b"user replaced")
        record_deletion(str(target), origin_cid="testcid")
        assert destination_mutation_since(
            str(target), existed_at_start=True, marker_snapshot=snapshot
        )

    def test_a_pre_existing_marker_left_unchanged_is_not_a_mutation(self, tmp_path, monkeypatch):
        bucket = _use_bucket(tmp_path, monkeypatch)
        target = bucket / "a.bin"
        record_deletion(str(target), origin_cid="testcid")
        snapshot = deletion_marker_snapshot(str(target))  # not None
        # The plan could have overridden this marker (a peer re-created the
        # file): as long as it did not change, the commit may proceed.
        target.write_bytes(b"x")
        assert not destination_mutation_since(
            str(target), existed_at_start=True, marker_snapshot=snapshot
        )
