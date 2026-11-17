# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for rename handling in the unified ``event_dispatch`` module."""

import os
from unittest.mock import patch

from hsync_client.event_dispatch import _same_local_entry, apply_inbound_event
from hsync_client.events import UiEventName
from hsync_client.remote_websocket import REMOTE_DISPATCH_CTX


class TestApplyInboundRenameEvent:
    """Tests for the ``renamed`` branch of :func:`apply_inbound_event`."""

    async def test_rename_rejected_through_in_bucket_symlink(self, tmp_path):
        """A rename whose dst crosses an in-bucket symlink must not move
        content outside the bucket."""
        outside = tmp_path / "outside"
        outside.mkdir()
        bucket = tmp_path / "bucket"
        bucket.mkdir()
        (bucket / "docs").symlink_to(outside)
        src_file = bucket / "a.txt"
        src_file.write_text("content")

        event = {
            "name": "renamed",
            "src": {"filepath": "a.txt", "sha256sum": "abc", "size": 7},
            "dst": {"filepath": "docs/evil.txt", "sha256sum": "abc", "size": 7},
        }
        config = {"LOCAL_BUCKET": str(bucket)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert src_file.exists()
        assert not (outside / "evil.txt").exists()

    async def test_rename_directory_success(self, tmp_path):
        """Successfully renames directory when source exists."""
        src_dir = tmp_path / "source"
        src_dir.mkdir()
        dst_dir = tmp_path / "dest"

        event = {
            "name": "renamed",
            "src": {"path": "source", "permissions": "755"},
            "dst": {"path": "dest", "permissions": "755"},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert dst_dir.exists()
        assert not src_dir.exists()

    async def test_rename_directory_source_not_found(self, tmp_path):
        """Recovers a missing source directory by re-fetching the destination."""
        event = {
            "name": "renamed",
            "src": {"path": "missing", "permissions": "755"},
            "dst": {"path": "dest", "permissions": "755"},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.update_dir") as mock_update,
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)
            mock_update.assert_called_once_with(event["dst"])

    async def test_rename_file_success(self, tmp_path):
        """Successfully renames file when source exists."""
        src_file = tmp_path / "source.txt"
        src_file.write_text("content")
        dst_file = tmp_path / "dest.txt"

        file_attrs = {
            "sha256sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "644",
        }
        event = {
            "name": "renamed",
            "src": {"filepath": "source.txt", **file_attrs},
            "dst": {"filepath": "dest.txt", **file_attrs},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert dst_file.exists()
        assert not src_file.exists()

    async def test_rename_event_is_emitted_after_the_local_rename(self, tmp_path):
        """Consumers observing the filesystem see the destination on emit."""
        src_file = tmp_path / "source.txt"
        src_file.write_text("content")
        file_attrs = {
            "sha256sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "644",
        }
        event = {
            "name": "renamed",
            "src": {"filepath": "source.txt", **file_attrs},
            "dst": {"filepath": "dest.txt", **file_attrs},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}
        seen: dict[str, bool] = {}

        def _capture(name, **kw):
            if name == UiEventName.rename:
                seen["dst_exists"] = (tmp_path / "dest.txt").exists()

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.emit_event", side_effect=_capture),
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert seen == {"dst_exists": True}

    async def test_rename_file_source_not_found(self, tmp_path):
        """Recovers a missing source file by downloading the destination."""
        file_attrs = {
            "sha256sum": "abc123",
            "mtime": 1000,
            "ctime": 1000,
            "size": 7,
            "permissions": "644",
        }
        event = {
            "name": "renamed",
            "src": {"filepath": "missing.txt", **file_attrs},
            "dst": {"filepath": "dest.txt", **file_attrs},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.download_file") as mock_dl,
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)
            mock_dl.assert_called_once()


class TestRenameDestinationGuard:
    """An inbound rename must never clobber existing local content."""

    async def test_existing_destination_is_left_to_the_reconcile(self, tmp_path):
        bucket = tmp_path / "bucket"
        bucket.mkdir()
        (bucket / "a.txt").write_text("local a")
        (bucket / "b.txt").write_text("local b")

        event = {
            "name": "renamed",
            "src": {"filepath": "a.txt", "sha256sum": "x", "size": 7},
            "dst": {"filepath": "b.txt", "sha256sum": "x", "size": 7},
        }
        config = {"LOCAL_BUCKET": str(bucket)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert (bucket / "a.txt").read_text() == "local a"
        assert (bucket / "b.txt").read_text() == "local b"


class TestSameLocalEntry:
    """The guard's entry comparison (case-only renames address one file)."""

    def test_same_path_is_the_same_entry(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")

        assert _same_local_entry(str(f), str(f)) is True

    def test_hardlink_is_the_same_entry(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        link = tmp_path / "g.txt"
        os.link(f, link)

        assert _same_local_entry(str(f), str(link)) is True

    def test_distinct_files_are_not_the_same_entry(self, tmp_path):
        a = tmp_path / "a.txt"
        a.write_text("a")
        b = tmp_path / "b.txt"
        b.write_text("b")

        assert _same_local_entry(str(a), str(b)) is False

    def test_a_missing_path_is_not_the_same_entry(self, tmp_path):
        a = tmp_path / "a.txt"
        a.write_text("a")

        assert _same_local_entry(str(a), str(tmp_path / "missing.txt")) is False
