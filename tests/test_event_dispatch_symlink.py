# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for symlink handling in the unified ``event_dispatch`` module."""

from unittest.mock import patch

from hsync_client.event_dispatch import EventDispatchContext, apply_inbound_event
from hsync_client.remote_websocket import REMOTE_DISPATCH_CTX


class TestApplyInboundSymlinkEvent:
    """Tests for the ``symlink`` branch of :func:`apply_inbound_event`."""

    async def test_symlink_location_rejected_through_in_bucket_symlink(self, tmp_path):
        """The link location is confined too: an in-bucket symlink must not
        redirect the create/unlink outside the bucket."""
        outside = tmp_path / "outside"
        outside.mkdir()
        bucket = tmp_path / "bucket"
        bucket.mkdir()
        (bucket / "docs").symlink_to(outside)
        target = bucket / "target.txt"
        target.write_text("content")

        event = {
            "name": "symlink",
            "src": {
                "type": "symlink",
                "filepath": "docs/evil-link.txt",
                "target": str(target),
            },
        }
        config = {"LOCAL_BUCKET": str(bucket)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not (outside / "evil-link.txt").exists()

    async def test_symlink_creation_success(self, tmp_path):
        """Successfully creates symlink at the requested path."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link_path = "link.txt"

        event = {
            "name": "symlink",
            "src": {
                "type": "symlink",
                "filepath": link_path,
                "target": str(target),
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert (tmp_path / link_path).is_symlink()

    async def test_symlink_ignores_non_symlink_events(self, tmp_path):
        """Ignores symlink events whose payload is not a symlink."""
        event = {
            "name": "symlink",
            "src": {"type": "file", "filepath": "file.txt"},
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.event_dispatch.os_symlink") as mock_symlink,
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)
            mock_symlink.assert_not_called()

    async def test_symlink_marked_receiving_during_creation(self, tmp_path):
        """The link path is marked as receiving while it is created locally.

        Regression: applying an inbound ``symlink`` event mutated the local
        filesystem (unlink + symlink) WITHOUT marking the path via the
        dispatch context, so the P2P slave's own watcher saw the mutation as a
        fresh local change and re-uploaded it to the master — which broadcast
        it back, producing an infinite ping-pong loop between peers. The
        marking must cover the whole mutation (cleanup + creation), mirroring
        ``_apply_rename``/``_apply_delete``.
        """
        target = tmp_path / "target.txt"
        target.write_text("content")
        link_path = str(tmp_path / "link.txt")

        marks: list[str] = []
        unmarks: list[str] = []
        ctx = EventDispatchContext(
            mark_receiving=marks.append,
            unmark_receiving=unmarks.append,
        )

        event = {
            "name": "symlink",
            "src": {
                "type": "symlink",
                "filepath": "link.txt",
                "target": str(target),
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, ctx)

        assert (tmp_path / "link.txt").is_symlink()
        assert marks == [link_path]
        assert unmarks == [link_path]

    async def test_symlink_unsafe_target_not_marked(self, tmp_path):
        """A symlink whose target is rejected must never be marked.

        Validation errors must not record a mark: an unmatched (or wrongly
        persisted) mark would suppress a genuine later local event on the same
        path.
        """
        marks: list[str] = []
        unmarks: list[str] = []
        ctx = EventDispatchContext(
            mark_receiving=marks.append,
            unmark_receiving=unmarks.append,
        )

        event = {
            "name": "symlink",
            "src": {
                "type": "symlink",
                "filepath": "link.txt",
                "target": "/etc/passwd",
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, ctx)

        assert not (tmp_path / "link.txt").is_symlink()
        assert marks == []
        assert unmarks == []

    async def test_symlink_idempotent_same_target(self, tmp_path):
        """An inbound event for a link that already exists with the SAME target
        is a no-op: no unlink, no re-symlink, no mark.

        Every mutation fires a watcher deleted+created pair on this node; when
        the watchdog echoes deliver after the mark/unmark window, the only
        thing that stops the echo from re-uploading to the master is this
        no-op shortcut. The link inode must survive untouched.
        """
        import os
        from pathlib import Path

        target = tmp_path / "target.txt"
        target.write_text("content")
        link_path = str(tmp_path / "link.txt")
        Path(link_path).symlink_to("target.txt")
        inode_before = os.stat(link_path, follow_symlinks=False).st_ino

        marks: list[str] = []
        ctx = EventDispatchContext(mark_receiving=marks.append)

        event = {
            "name": "symlink",
            "src": {
                "type": "symlink",
                "filepath": "link.txt",
                "target": "target.txt",
            },
        }
        config = {"LOCAL_BUCKET": str(tmp_path)}

        with patch("hsync_client.event_dispatch.CONFIG", config):
            await apply_inbound_event(event, ctx)

        assert Path(link_path).is_symlink()
        assert os.stat(link_path, follow_symlinks=False).st_ino == inode_before
        assert marks == []
