# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Contract tests for the P2P master's rename endpoint.

The master serves the same ``PATCH …/files?src=&dst=`` contract as the central
server, so a slave that renamed a file while the master was unreachable can
converge with a zero-byte move instead of DELETE + re-upload.
"""

import os
from hashlib import sha256 as _sha256
from unittest.mock import patch

import pytest
from hsync_common.constants import HSYNCKEEP_FILENAME

from hsync_client.p2p_peers import get_free_port
from hsync_client.p2p_server import P2PServer

from .p2p_helpers import unauth_write_config


@pytest.fixture
def server(tmp_path):
    """A live P2P master serving *tmp_path* as its bucket."""
    port = get_free_port()
    srv = P2PServer(port=port)
    srv.start()
    with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
        yield port
    srv.stop()


def _rename(port: int, **params):
    import requests

    return requests.patch(
        f"http://localhost:{port}/api/v1/buckets/test/files",
        params=params,
        timeout=5,
    )


class TestRenamePreconditions:
    """A refused move must leave both sides untouched."""

    def test_existing_destination_is_a_conflict(self, server, tmp_path):
        (tmp_path / "old.bin").write_bytes(b"old")
        (tmp_path / "taken.bin").write_bytes(b"taken")

        response = _rename(server, src="old.bin", dst="taken.bin")

        assert response.status_code == 409
        assert (tmp_path / "old.bin").read_bytes() == b"old"
        assert (tmp_path / "taken.bin").read_bytes() == b"taken"

    def test_stale_checksum_is_a_conflict(self, server, tmp_path):
        (tmp_path / "old.bin").write_bytes(b"current")

        response = _rename(
            server,
            src="old.bin",
            dst="new.bin",
            expected_sha256=_sha256(b"what the listing claimed").hexdigest(),
        )

        assert response.status_code == 409
        assert (tmp_path / "old.bin").exists()
        assert not (tmp_path / "new.bin").exists()

    def test_missing_source_is_not_found(self, server, tmp_path):
        assert _rename(server, src="absent.bin", dst="new.bin").status_code == 404

    def test_invalid_checksum_is_rejected(self, server, tmp_path):
        (tmp_path / "old.bin").write_bytes(b"x")

        response = _rename(server, src="old.bin", dst="new.bin", expected_sha256="not-a-digest")

        assert response.status_code == 400

    def test_traversal_is_rejected(self, server, tmp_path):
        (tmp_path / "old.bin").write_bytes(b"x")

        assert _rename(server, src="old.bin", dst="../escape.bin").status_code == 400
        assert not (tmp_path.parent / "escape.bin").exists()


class TestRenameApplies:
    """The accepted move returns the destination entry the caller records."""

    def test_moves_the_file_and_returns_its_wire_entry(self, server, tmp_path):
        content = b"payload"
        (tmp_path / "old.bin").write_bytes(content)

        response = _rename(
            server,
            src="old.bin",
            dst="nested/new.bin",
            expected_sha256=_sha256(content).hexdigest(),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["info"] == "Renamed successfully"
        assert body["file"]["filepath"] == "nested/new.bin"
        assert body["file"]["sha256sum"] == _sha256(content).hexdigest()
        assert not (tmp_path / "old.bin").exists()
        assert (tmp_path / "nested" / "new.bin").read_bytes() == content

    def test_applies_the_advertised_metadata(self, server, tmp_path):
        (tmp_path / "old.bin").write_bytes(b"meta")
        target_mtime = 1_700_000_000

        response = _rename(
            server, src="old.bin", dst="new.bin", mtime=target_mtime, permissions="600"
        )

        assert response.status_code == 200, response.text
        moved = tmp_path / "new.bin"
        assert int(moved.stat().st_mtime) == target_mtime
        assert oct(moved.stat().st_mode & 0o777) == "0o600"

    def test_a_vacated_directory_keeps_its_sentinel(self, server, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "old.bin").write_bytes(b"x")

        assert _rename(server, src="sub/old.bin", dst="new.bin").status_code == 200

        # The emptied source directory survives the reconcile thanks to the marker.
        assert (tmp_path / "sub" / HSYNCKEEP_FILENAME).exists()

    def test_moves_a_directory_tree(self, server, tmp_path):
        (tmp_path / "tree").mkdir()
        (tmp_path / "tree" / "one.bin").write_bytes(b"one")
        (tmp_path / "tree" / "two.bin").write_bytes(b"two")

        response = _rename(server, src="tree", dst="moved")

        assert response.status_code == 200, response.text
        assert not (tmp_path / "tree").exists()
        assert (tmp_path / "moved" / "one.bin").read_bytes() == b"one"
        assert (tmp_path / "moved" / "two.bin").read_bytes() == b"two"


class TestMoveBase:
    """The stored merge base follows the content, without ticking a new clock."""

    def test_move_base_carries_the_entry(self, tmp_path):
        from hsync_client import sync_state as state

        cfg = {"LOCAL_BUCKET": str(tmp_path), "CID": "PeerL1234567890ABCDEF"}
        src = tmp_path / "old.bin"
        src.write_bytes(b"content")
        digest = _sha256(b"content").hexdigest()
        dst = tmp_path / "new.bin"
        os.replace(src, dst)

        with patch("hsync_client.state_backend.CONFIG", cfg):
            state.record_base(str(src), digest, vclock={"PeerL1234567890ABCDEF": 3})
            state.move_base(str(src), str(dst))

            assert state.get_base_hash(str(src)) is None
            assert state.get_base_hash(str(dst)) == digest
            info = state.version_info_for_path(str(dst))
            assert info is not None
            assert info["vclock"] == {"PeerL1234567890ABCDEF": 3}
