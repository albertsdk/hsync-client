# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for delete handling in the unified ``event_dispatch`` module."""

import uuid
from unittest.mock import patch

from hsync_client.event_dispatch import apply_inbound_event
from hsync_client.remote_websocket import REMOTE_DISPATCH_CTX


class TestApplyInboundDeleteEvent:
    """Tests for the ``deleted`` branch of :func:`apply_inbound_event`."""

    async def test_delete_rejected_when_second_path_key_is_malicious(self, tmp_path):
        """A payload with a valid ``filepath`` AND a malicious ``path`` is
        rejected outright: every present path key must validate, not just the
        first one the gate happens to read."""
        file_to_keep = tmp_path / "keep.txt"
        file_to_keep.write_text("content")

        event = {
            "name": "deleted",
            "src": {
                "filepath": "keep.txt",
                "path": "../outside.txt",  # smuggled malicious second key
                "sha256sum": "abc123",
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert file_to_keep.exists()

    async def test_delete_rejected_when_only_malicious_path_key(self, tmp_path):
        """A malicious ``path`` is rejected even when ``filepath`` is absent."""
        file_to_keep = tmp_path / "keep.txt"
        file_to_keep.write_text("content")

        event = {
            "name": "deleted",
            "src": {"path": "../outside.txt"},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert file_to_keep.exists()

    async def test_delete_rejected_through_in_bucket_symlink(self, tmp_path):
        """Lexical validation alone cannot stop an in-bucket symlink escape:
        a bucket containing ``docs -> outside`` must not let a peer delete
        ``docs/secret.txt`` (outside the bucket)."""
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("secret")
        bucket = tmp_path / "bucket"
        bucket.mkdir()
        (bucket / "docs").symlink_to(outside)

        event = {
            "name": "deleted",
            "src": {"filepath": "docs/secret.txt", "sha256sum": "abc123"},
        }
        config = {"LOCAL_BUCKET": str(bucket)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert secret.exists()

    async def test_delete_directory_rejected_through_in_bucket_symlink(self, tmp_path):
        """Directory deletes (rmtree) are confined the same way."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "data.txt").write_text("data")
        bucket = tmp_path / "bucket"
        bucket.mkdir()
        (bucket / "link").symlink_to(outside)

        event = {
            "name": "deleted",
            "src": {"path": "link", "permissions": "755"},
        }
        config = {"LOCAL_BUCKET": str(bucket)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert (outside / "data.txt").exists()

    async def test_delete_directory_success(self, tmp_path):
        """Successfully deletes existing directory."""
        dir_to_delete = tmp_path / "to_delete"
        dir_to_delete.mkdir()
        (dir_to_delete / "file.txt").write_text("content")

        event = {
            "name": "deleted",
            "src": {"path": "to_delete", "permissions": "755"},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not dir_to_delete.exists()

    async def test_delete_directory_not_found(self, tmp_path):
        """A missing directory is a benign echo, logged at DEBUG (not WARNING).

        Regression: a slave that already removed the path locally receives
        the master's broadcast ``deleted`` echo for the same path. The delete
        already succeeded, so an absent path must degrade WARNING->DEBUG
        (mirrors the rename 404-echo degradation and the master's
        "File already deleted" success branch).
        """
        event = {
            "name": "deleted",
            "src": {"path": "missing", "permissions": "755"},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.log") as mock_log,
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert mock_log.debug.called
        assert not mock_log.warning.called

    async def test_delete_file_success(self, tmp_path):
        """Successfully deletes existing file."""
        file_to_delete = tmp_path / "to_delete.txt"
        file_to_delete.write_text("content")

        event = {
            "name": "deleted",
            "src": {
                "filepath": "to_delete.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "644",
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not file_to_delete.exists()

    async def test_delete_file_not_found(self, tmp_path):
        """A missing file is a benign echo, logged at DEBUG (not WARNING).

        Regression: see ``test_delete_directory_not_found`` — same echo
        behaviour for the file branch.
        """
        event = {
            "name": "deleted",
            "src": {
                "filepath": "missing.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "644",
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.log") as mock_log,
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert mock_log.debug.called
        assert not mock_log.warning.called

    async def test_delete_keep_policy(self, tmp_path):
        """Skips deletion when the policy is keep."""
        file_to_keep = tmp_path / "keep_me.txt"
        file_to_keep.write_text("content")

        event = {
            "name": "deleted",
            "src": {
                "filepath": "keep_me.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 7,
                "permissions": "644",
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path), "DELETION_POLICY": "keep"}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert file_to_keep.exists()

    async def test_delete_hsynckeep_cleans_empty_parent(self, tmp_path):
        """Deleting ``.hsynckeep`` removes the now-empty parent directory."""
        subdir = tmp_path / "emptydir"
        subdir.mkdir()
        sentinel = subdir / ".hsynckeep"
        sentinel.write_text("")

        event = {
            "name": "deleted",
            "src": {
                "filepath": "emptydir/.hsynckeep",
                "sha256sum": "bb25b1c2b2a7427401c823a90904a759e2bd7425205b0702bca5e27e937e7cff",
                "mtime": 1000,
                "ctime": 1000,
                "size": 0,
                "permissions": "644",
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path), "DELETION_POLICY": "propagate"}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not sentinel.exists()
        assert not subdir.exists()


class TestApplyInboundDeleteKeepsTheBytes:
    """A deletion applied in real time is as recoverable as a cycle's (ADR-0056)."""

    @staticmethod
    def _bucket(tmp_path):
        bucket = tmp_path / "bucket"
        bucket.mkdir()
        (bucket / ".hsyncfolder").write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
        return bucket

    @staticmethod
    def _store(bucket, tmp_path):
        from hsync_client.trash import TrashSettings, TrashStore

        return TrashStore(bucket, settings=TrashSettings(enabled=True, root=tmp_path / "trash"))

    async def test_the_bytes_reach_the_trash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HSYNC_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("HSYNC_TRASH_DIR", str(tmp_path / "trash"))
        bucket = self._bucket(tmp_path)
        target = bucket / "gone.txt"
        target.write_text("bye", encoding="utf-8")
        event = {
            "name": "deleted",
            "src": {
                "filepath": "gone.txt",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 3,
                "permissions": "644",
            },
        }

        with patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(bucket)}):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not target.exists()
        assert [item.entry["relpath"] for item in self._store(bucket, tmp_path).entries()] == [
            "gone.txt"
        ]

    async def test_the_sentinel_is_unlinked_without_an_entry(self, tmp_path, monkeypatch):
        """``.hsynckeep`` carries no user bytes: the store must not grow for it."""
        monkeypatch.setenv("HSYNC_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("HSYNC_TRASH_DIR", str(tmp_path / "trash"))
        bucket = self._bucket(tmp_path)
        subdir = bucket / "emptydir"
        subdir.mkdir()
        (subdir / ".hsynckeep").write_text("", encoding="utf-8")
        event = {
            "name": "deleted",
            "src": {
                "filepath": "emptydir/.hsynckeep",
                "sha256sum": "abc123",
                "mtime": 1000,
                "ctime": 1000,
                "size": 0,
                "permissions": "644",
            },
        }

        with patch("hsync_client.event_dispatch.CONFIG", {"LOCAL_BUCKET": str(bucket)}):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not subdir.exists()
        assert self._store(bucket, tmp_path).entries() == []
