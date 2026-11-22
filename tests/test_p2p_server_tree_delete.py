# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""A recursive delete is authoritative for its subtree.

A ``DELETE … is_dir=true`` closes its tree for the whole removal. Without that
window a concurrent create, upload or rename recreates content inside the tree,
``rmtree`` fails with ``ENOTEMPTY`` and the caller's deletion is abandoned half
done — the folder then comes back on both sides at the next reconcile.

Covers the three parts of the contract: the in-flight window opened by the
delete handler, the 409 refusals, and the converging retry that survives a
writer which was already past the gate.
"""

import errno
import shutil
from unittest.mock import patch

import pytest
from hsync_common import deletions

from hsync_client.p2p_peers import get_free_port
from hsync_client.p2p_server import P2PEndpointMixin, P2PServer

from .p2p_helpers import unauth_write_config


@pytest.fixture(autouse=True)
def _clean_registry():
    deletions.reset_tree_deletes()
    yield
    deletions.reset_tree_deletes()


@pytest.fixture
def server():
    port = get_free_port()
    srv = P2PServer(port=port)
    srv.start()
    yield srv, port
    srv.stop()


def _files_url(port: int) -> str:
    return f"http://localhost:{port}/api/v1/buckets/test/files"


def _dirs_url(port: int) -> str:
    return f"http://localhost:{port}/api/v1/buckets/test/dirs"


class TestConvergingRmtree:
    def test_a_content_slipping_in_mid_removal_is_retried(self, tmp_path):
        """ENOTEMPTY from a concurrent write must not abandon the delete."""
        tree = tmp_path / "Music"
        (tree / "i_o").mkdir(parents=True)
        (tree / "i_o" / "1.mp3").write_text("x")
        real_rmtree = shutil.rmtree
        calls = []

        def flaky(path, ignore_errors=False):
            calls.append(path)
            if len(calls) == 1:
                raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))
            return real_rmtree(path, ignore_errors=ignore_errors)

        with patch("shutil.rmtree", flaky):
            P2PEndpointMixin._rmtree_converging(str(tree))

        assert len(calls) == 2
        assert not tree.exists()

    def test_a_persistent_failure_is_reported(self, tmp_path):
        """A tree that never empties is a real error, not an infinite retry."""
        tree = tmp_path / "Music"
        tree.mkdir()

        def always_failing(path, ignore_errors=False):
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))

        with patch("shutil.rmtree", always_failing), pytest.raises(OSError):
            P2PEndpointMixin._rmtree_converging(str(tree))


class TestDeleteEndpointClosesItsTree:
    def test_a_failing_delete_reopens_the_tree(self, server, tmp_path):
        """The window is closed on failure too, or the path stays unwritable."""
        srv, port = server
        tree = tmp_path / "Music"
        (tree / "i_o").mkdir(parents=True)
        (tree / "i_o" / "1.mp3").write_text("x")

        def always_failing(path, ignore_errors=False):
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))

        import requests

        with (
            patch("hsync_client.p2p_server.CONFIG", unauth_write_config(tmp_path)),
            patch("shutil.rmtree", always_failing),
        ):
            response = requests.delete(
                _files_url(port), params={"path": "Music", "is_dir": "true"}, timeout=5
            )

        assert response.status_code == 500
        assert not deletions.covered_by_tree_delete(str(tree))
        assert srv is not None


class TestWritesInsideAnInFlightDelete:
    def test_create_dir_is_refused(self, server, tmp_path):
        import requests

        _, port = server
        deletions.begin_tree_delete(str(tmp_path / "Music"))
        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(tmp_path)):
            response = requests.post(_dirs_url(port), params={"path": "Music/Crono"}, timeout=5)

        assert response.status_code == 409
        assert not (tmp_path / "Music").exists()

    def test_upload_is_refused(self, server, tmp_path):
        import requests

        _, port = server
        deletions.begin_tree_delete(str(tmp_path / "Music"))
        files = {"file": ("1.mp3", b"content")}
        data = {"path": "Music/i_o", "size": "7", "sha256sum": "0" * 64}
        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(tmp_path)):
            response = requests.post(_files_url(port), files=files, data=data, timeout=5)

        assert response.status_code == 409
        assert not (tmp_path / "Music").exists()

    def test_rename_destination_is_refused(self, server, tmp_path):
        import requests

        _, port = server
        source = tmp_path / "track.mp3"
        source.write_text("x")
        deletions.begin_tree_delete(str(tmp_path / "Music"))
        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(tmp_path)):
            response = requests.patch(
                _files_url(port),
                params={"src": "track.mp3", "dst": "Music/track.mp3"},
                timeout=5,
            )

        assert response.status_code == 409
        assert source.exists()
        assert not (tmp_path / "Music").exists()

    def test_the_same_paths_work_once_the_delete_finished(self, server, tmp_path):
        """The window must not outlive the removal it guards."""
        import requests

        _, port = server
        target = tmp_path / "Music" / "Crono"
        deletions.begin_tree_delete(str(tmp_path / "Music"))
        deletions.end_tree_delete(str(tmp_path / "Music"))

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(tmp_path)):
            response = requests.post(_dirs_url(port), params={"path": "Music/Crono"}, timeout=5)

        assert response.status_code == 201
        assert target.is_dir()


class TestARemoteDeleteOfTheLastFile:
    """A directory emptied by a peer's delete must not survive as a shell.

    ``GET /dirs`` publishes every directory on disk, so a shell left behind here
    is re-created by the peer that removed the folder — the folder the user
    deleted comes back. An *intentional* empty directory carries a
    ``.hsynckeep`` marker, so it is never empty and is left alone.
    """

    def test_a_peer_delete_prunes_the_emptied_directory(self, server, tmp_path):
        import requests

        _, port = server
        tree = tmp_path / "Crono"
        tree.mkdir()
        (tree / "01.mp3").write_text("x")

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(tmp_path)):
            response = requests.delete(
                _files_url(port),
                params={
                    "path": "Crono/01.mp3",
                    "is_dir": "false",
                    "cid": "TestClientA1234567890",
                },
                timeout=5,
            )

        assert response.status_code == 200
        assert not (tree / "01.mp3").exists()
        assert not tree.exists(), "the emptied directory must not survive as a shell"

    def test_a_peer_delete_of_one_of_several_files_keeps_the_directory(self, server, tmp_path):
        import requests

        _, port = server
        tree = tmp_path / "Crono"
        tree.mkdir()
        (tree / "01.mp3").write_text("x")
        (tree / "02.mp3").write_text("y")

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(tmp_path)):
            response = requests.delete(
                _files_url(port),
                params={
                    "path": "Crono/01.mp3",
                    "is_dir": "false",
                    "cid": "TestClientA1234567890",
                },
                timeout=5,
            )

        assert response.status_code == 200
        assert tree.is_dir()
        assert (tree / "02.mp3").exists()

    def test_a_delete_without_an_origin_keeps_the_directory(self, server, tmp_path):
        """Local tooling keeps the structure it vacated (a ``.hsynckeep``)."""
        import requests

        _, port = server
        tree = tmp_path / "Crono"
        tree.mkdir()
        (tree / "01.mp3").write_text("x")

        with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(tmp_path)):
            response = requests.delete(
                _files_url(port), params={"path": "Crono/01.mp3", "is_dir": "false"}, timeout=5
            )

        assert response.status_code == 200
        assert (tree / ".hsynckeep").exists()
