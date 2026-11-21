# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""A directory ``modified`` event must never create the directory.

Regression for the deletion that never converged: a delete storm emits an
``IN_ATTRIB`` (``modified``) on every directory it empties, and that event can
reach a peer *after* it removed the same tree. Applying ``modified`` with a
mkdir resurrected the tree, the peer's recursive delete then failed with
``ENOTEMPTY`` and the folder came back on both sides.

The contract: ``modified`` presupposes the directory exists locally, so only
``added`` materialises. A subtree an in-flight recursive delete owns is closed
to materialisation entirely.
"""

from unittest.mock import patch

import pytest

from hsync_client import deletions
from hsync_client.event_dispatch import apply_inbound_event
from hsync_client.remote_websocket import REMOTE_DISPATCH_CTX


@pytest.fixture(autouse=True)
def _clean_tree_delete_registry():
    """Every test starts and ends with an empty in-flight-delete registry."""
    deletions.reset_tree_deletes()
    yield
    deletions.reset_tree_deletes()


def _config(bucket):
    return {"LOCAL_BUCKET": str(bucket), "SYNC_DIRECTION": "bidirectional"}


class TestDirModifiedNeverCreates:
    async def test_modified_on_an_absent_directory_does_not_recreate_it(self, tmp_path):
        """The stale IN_ATTRIB of a deleted tree must not bring it back."""
        event = {"name": "modified", "src": {"path": "Music/Peter Gabriel", "permissions": "755"}}
        config = _config(tmp_path)

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.sync.CONFIG", config),
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not (tmp_path / "Music" / "Peter Gabriel").exists()

    async def test_modified_on_a_nested_absent_directory_does_not_create_parents(self, tmp_path):
        """The parents of a vanished path must not be materialised either."""
        event = {"name": "modified", "src": {"path": "a/b/c", "permissions": "755"}}
        config = _config(tmp_path)

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.sync.CONFIG", config),
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not (tmp_path / "a").exists()

    async def test_added_still_materialises_the_directory(self, tmp_path):
        """A genuine create keeps working, parents included."""
        event = {"name": "added", "src": {"path": "Music/Crono", "permissions": "755"}}
        config = _config(tmp_path)

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.sync.CONFIG", config),
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert (tmp_path / "Music" / "Crono").is_dir()

    async def test_modified_on_an_existing_directory_still_applies_permissions(self, tmp_path):
        """The event is not dropped: an existing directory keeps its metadata path."""
        target = tmp_path / "Music" / "Crono"
        target.mkdir(parents=True)
        target.chmod(0o700)
        event = {"name": "modified", "src": {"path": "Music/Crono", "permissions": "755"}}
        config = _config(tmp_path)

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.sync.CONFIG", config),
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert (target.stat().st_mode & 0o777) == 0o755


class TestMaterialisationInsideAnInFlightDelete:
    async def test_added_inside_the_deleted_tree_is_dropped(self, tmp_path):
        """A create inside a subtree being removed would defeat the rmtree."""
        deletions.begin_tree_delete(str(tmp_path / "Music"))
        event = {"name": "added", "src": {"path": "Music/Crono", "permissions": "755"}}
        config = _config(tmp_path)

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.sync.CONFIG", config),
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not (tmp_path / "Music").exists()

    async def test_a_sibling_path_is_not_blocked(self, tmp_path):
        """Only the removed subtree is closed, never its siblings."""
        deletions.begin_tree_delete(str(tmp_path / "Music"))
        event = {"name": "added", "src": {"path": "Pictures/Crono", "permissions": "755"}}
        config = _config(tmp_path)

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.sync.CONFIG", config),
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert (tmp_path / "Pictures" / "Crono").is_dir()

    async def test_a_delete_inside_the_deleted_tree_is_still_applied(self, tmp_path):
        """Deleting inside a tree being removed stays legal (and idempotent)."""
        target = tmp_path / "Music" / "Crono"
        target.mkdir(parents=True)
        deletions.begin_tree_delete(str(tmp_path / "Music"))
        event = {"name": "deleted", "src": {"path": "Music/Crono"}}
        config = _config(tmp_path)

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.sync.CONFIG", config),
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not target.exists()

    async def test_a_rename_into_the_deleted_tree_is_dropped(self, tmp_path):
        """The destination of a rename is a materialisation too."""
        source = tmp_path / "track.mp3"
        source.write_text("x")
        deletions.begin_tree_delete(str(tmp_path / "Music"))
        event = {
            "name": "renamed",
            "src": {"path": "track.mp3"},
            "dst": {"path": "Music/track.mp3"},
        }
        config = _config(tmp_path)

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.sync.CONFIG", config),
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert source.exists()
        assert not (tmp_path / "Music").exists()

    async def test_a_symlink_inside_the_deleted_tree_is_dropped(self, tmp_path):
        """A link inside the removed tree must not be planted either."""
        deletions.begin_tree_delete(str(tmp_path / "Music"))
        event = {
            "name": "symlink",
            "src": {"type": "symlink", "filepath": "Music/link", "target": "track.mp3"},
        }
        config = _config(tmp_path)

        with (
            patch("hsync_client.event_dispatch.CONFIG", config),
            patch("hsync_client.sync.CONFIG", config),
        ):
            await apply_inbound_event(event, REMOTE_DISPATCH_CTX)

        assert not (tmp_path / "Music").exists()
