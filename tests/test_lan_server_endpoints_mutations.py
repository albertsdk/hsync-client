"""
Tests for LAN embedded HTTP server endpoints.
"""

import time
from unittest.mock import patch

import pytest

from hsync_client.lan import get_free_port
from hsync_client.lan_server import LANServer

# A well-formed CID per hsync_common.models.event.CID_PATTERN (21 alphanumeric).
VALID_CID = "a" * 21


class TestLANRequestHandlerCreateDir:
    """Tests for the POST /dirs endpoint."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""

        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_create_nested_dir_marks_missing_parents_inbound(self, server, tmp_path):
        """Regression: implicitly created parents must carry the origin CID.

        ``mkdir(parents=True)`` creates every missing ancestor, and the
        master's watcher fires a create event for each. Those events must be
        attributed to the requesting slave (via ``mark_inbound_origin``),
        otherwise they are logged/broadcast as local creates and the slaves
        cannot echo-suppress them.
        """
        import requests

        from hsync_client.lan_websocket import (
            _inbound_origin,
            peek_inbound_origin,
        )

        _inbound_origin.clear()
        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/dirs"
            response = requests.post(url, params={"path": "a/b/c", "cid": VALID_CID}, timeout=5)
            assert response.status_code == 201

        # The leaf itself is marked...
        assert peek_inbound_origin(str(tmp_path / "a" / "b" / "c")) == VALID_CID
        # ...and so are the missing parents created by the same request.
        assert peek_inbound_origin(str(tmp_path / "a")) == VALID_CID
        assert peek_inbound_origin(str(tmp_path / "a" / "b")) == VALID_CID
        # The bucket root itself is never a candidate.
        assert peek_inbound_origin(str(tmp_path)) is None
        # Exactly the three created directories were marked (peek does not
        # consume, so the map still holds them).
        assert set(_inbound_origin) == {
            str(tmp_path / "a"),
            str(tmp_path / "a" / "b"),
            str(tmp_path / "a" / "b" / "c"),
        }
        _inbound_origin.clear()

    def test_create_dir_invalid_cid_marks_nothing(self, server, tmp_path):
        """A malformed cid must not reach the inbound-origin map."""
        import requests

        from hsync_client.lan_websocket import _inbound_origin

        _inbound_origin.clear()
        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/dirs"
            response = requests.post(url, params={"path": "x", "cid": "not-a-valid-cid"}, timeout=5)
            assert response.status_code == 201

        assert (tmp_path / "x").exists()
        assert _inbound_origin == {}

    def test_create_dir_existing_dir_not_remarked(self, server, tmp_path):
        """An already-existing directory is an echo: no mark is recorded."""
        import requests

        from hsync_client.lan_websocket import _inbound_origin

        _inbound_origin.clear()
        target = tmp_path / "existing"
        target.mkdir()

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/dirs"
            response = requests.post(url, params={"path": "existing", "cid": VALID_CID}, timeout=5)
            assert response.status_code == 201

        assert _inbound_origin == {}


class TestLANRequestHandlerDELETE:
    """Tests for DELETE endpoint."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""

        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_delete_file(self, server, tmp_path):
        """Test deleting a file via DELETE endpoint."""
        import requests

        srv, port = server

        # Create test file
        test_file = tmp_path / "to_delete.txt"
        test_file.write_text("content to delete")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.delete(url, params={"path": "to_delete.txt"}, timeout=5)

            assert response.status_code == 200
            assert not test_file.exists()

    def test_delete_directory(self, server, tmp_path):
        """Test deleting a directory via DELETE endpoint."""
        import requests

        srv, port = server

        # Create test directory with file
        test_dir = tmp_path / "to_delete_dir"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("content")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.delete(
                url, params={"path": "to_delete_dir", "is_dir": "true"}, timeout=5
            )

            assert response.status_code == 200
            assert not test_dir.exists()

    def test_delete_nonexistent_file(self, server, tmp_path):
        """Test deleting a non-existent file returns success."""
        import requests

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.delete(url, params={"path": "nonexistent.txt"}, timeout=5)

            assert response.status_code == 200

    def test_delete_without_path_param(self, server, tmp_path):
        """Test DELETE without path parameter returns error."""
        import requests

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.delete(url, timeout=5)

            assert response.status_code == 400


class TestLANRequestHandlerPATCH:
    """Tests for PATCH (rename) endpoint."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""

        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_rename_file(self, server, tmp_path):
        """Test renaming a file via PATCH endpoint."""
        import requests

        srv, port = server

        # Create test file
        test_file = tmp_path / "old_name.txt"
        test_file.write_text("content")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.patch(
                url, params={"src": "old_name.txt", "dst": "new_name.txt"}, timeout=5
            )

            assert response.status_code == 200
            assert not test_file.exists()
            assert (tmp_path / "new_name.txt").exists()

    def test_rename_directory(self, server, tmp_path):
        """Test renaming a directory via PATCH endpoint."""
        import requests

        srv, port = server

        # Create test directory
        test_dir = tmp_path / "old_dir"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("content")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.patch(url, params={"src": "old_dir", "dst": "new_dir"}, timeout=5)

            assert response.status_code == 200
            assert not test_dir.exists()
            assert (tmp_path / "new_dir").exists()

    def test_rename_nonexistent_file(self, server, tmp_path):
        """Test renaming a non-existent file returns error."""
        import requests

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.patch(
                url, params={"src": "nonexistent.txt", "dst": "new.txt"}, timeout=5
            )

            assert response.status_code == 404

    def test_rename_without_params(self, server, tmp_path):
        """Test rename without src/dst params returns error."""
        import requests

        srv, port = server

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.patch(url, timeout=5)

            assert response.status_code == 400

    def test_rename_symlink_moves_link_not_target(self, server, tmp_path):
        """PATCH on a symlink moves the LINK entry, never the target file.

        Regression: ``resolve_within`` with the default ``resolve_final=True``
        dereferenced the link, so renaming a link renamed the TARGET instead,
        losing the link and moving the pointed file away from its path on the
        master (and then on every peer via the event broadcast).
        """
        import requests

        srv, port = server

        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to("target.txt")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.patch(
                url, params={"src": "link.txt", "dst": "renamed-link.txt"}, timeout=5
            )

            assert response.status_code == 200
            assert not link.is_symlink()
            assert (tmp_path / "renamed-link.txt").is_symlink()
            # The target file must have stayed at its original path.
            assert target.exists()
            assert target.read_text() == "content"


class TestLANRequestHandlerSymlink:
    """Tests for the POST /symlinks endpoint (structural replication)."""

    @pytest.fixture
    def server(self):
        """Create and start a test server."""
        port = get_free_port()
        server = LANServer(port=port)
        server.start()
        time.sleep(0.1)
        yield server, port
        server.stop()

    def test_delete_symlink_keeps_target(self, server, tmp_path):
        """DELETE on a symlink removes the LINK, never the target file.

        Regression: ``_handle_delete`` resolved the incoming path with the
        default ``resolve_final=True``, which dereferenced the link and
        deleted the TARGET file — the data loss observed on every peer after
        a slave deleted a synced link.
        """
        import requests

        srv, port = server

        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to("target.txt")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.delete(url, params={"path": "link.txt"}, timeout=5)

            assert response.status_code == 200
            assert not link.is_symlink()
            # The target file must have survived.
            assert target.exists()
            assert target.read_text() == "content"

    def test_delete_dir_symlink_keeps_target_tree(self, server, tmp_path):
        """DELETE is_dir=true on a dir-symlink removes the LINK, never rmtree's
        the pointed directory."""
        import requests

        srv, port = server

        target_dir = tmp_path / "real-dir"
        target_dir.mkdir()
        (target_dir / "file.txt").write_text("content")
        link = tmp_path / "dir-link"
        link.symlink_to("real-dir", target_is_directory=True)

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/files"
            response = requests.delete(
                url, params={"path": "dir-link", "is_dir": "true"}, timeout=5
            )

            assert response.status_code == 200
            assert not link.is_symlink()
            assert target_dir.exists()
            assert (target_dir / "file.txt").exists()

    def test_symlink_idempotent_same_target(self, server, tmp_path):
        """POST /symlinks with an already-existing identical link is a NO-OP.

        Regression: unconditional unlink+recreate made the master's watcher
        emit a deleted+created pair on every harmless echo; the single
        inbound-origin mark was consumed by the first event and the second
        was rebroadcast with the master CID, re-triggering the whole
        SYMLINK/DELETE oscillation on every peer forever.
        """
        import os

        import requests

        srv, port = server

        link = tmp_path / "link.txt"
        link.symlink_to("target.txt")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/symlinks"
            inode_before = os.stat(link, follow_symlinks=False).st_ino
            response = requests.post(
                url, params={"path": "link.txt", "target": "target.txt"}, timeout=5
            )

            assert response.status_code == 200
            # The link entry must not have been recreated: same inode.
            assert link.is_symlink()
            assert os.stat(link, follow_symlinks=False).st_ino == inode_before

    def test_symlink_replaces_link_with_different_target(self, server, tmp_path):
        """POST /symlinks with a DIFFERENT target is a real change: replaced."""
        import os

        import requests

        srv, port = server

        link = tmp_path / "link.txt"
        link.symlink_to("old-target.txt")

        with patch(
            "hsync_client.lan_server.CONFIG",
            {
                "LOCAL_BUCKET": str(tmp_path),
                "LAN_KEY": None,
            },
        ):
            url = f"http://localhost:{port}/api/v1/buckets/test/symlinks"
            response = requests.post(
                url, params={"path": "link.txt", "target": "new-target.txt"}, timeout=5
            )

            assert response.status_code == 201
            assert link.is_symlink()
            assert os.readlink(link) == "new-target.txt"
