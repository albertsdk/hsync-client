# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Contract tests for the p2p master's metadata-only PATCH guard.

``PATCH …/files/permissions`` converges a chmod/touch without any payload, so
the peer cannot see the content. ``expected_sha256`` makes it verify that the
path still holds the content the slave compared against: a concurrent write
must not receive stale metadata (the slave then re-uploads instead).
"""

from hashlib import sha256 as _sha256
from unittest.mock import patch

import pytest

from hsync_client.p2p_peers import get_free_port
from hsync_client.p2p_server import P2PServer

from .p2p_helpers import unauth_write_config


@pytest.fixture
def server(tmp_path):
    port = get_free_port()
    srv = P2PServer(port=port)
    srv.start()
    with patch("hsync_client.p2p_server.CONFIG", unauth_write_config(str(tmp_path))):
        yield port
    srv.stop()


def _patch_permissions(port: int, **params):
    import requests

    return requests.patch(
        f"http://localhost:{port}/api/v1/buckets/test/files/permissions",
        params=params,
        timeout=5,
    )


class TestMetadataContentGuard:
    def test_matching_content_applies_the_mode(self, server, tmp_path):
        (tmp_path / "doc.bin").write_bytes(b"guarded")

        response = _patch_permissions(
            server,
            filename="doc.bin",
            permissions="600",
            expected_sha256=_sha256(b"guarded").hexdigest(),
        )

        assert response.status_code == 200, response.text
        assert oct((tmp_path / "doc.bin").stat().st_mode & 0o777) == "0o600"

    def test_stale_content_is_a_conflict(self, server, tmp_path):
        (tmp_path / "doc.bin").write_bytes(b"new content")

        response = _patch_permissions(
            server,
            filename="doc.bin",
            permissions="600",
            expected_sha256=_sha256(b"what the slave saw").hexdigest(),
        )

        assert response.status_code == 409
        assert oct((tmp_path / "doc.bin").stat().st_mode & 0o777) != "0o600"

    def test_malformed_guard_is_rejected(self, server, tmp_path):
        (tmp_path / "doc.bin").write_bytes(b"bytes")

        response = _patch_permissions(
            server,
            filename="doc.bin",
            permissions="600",
            expected_sha256="not-a-digest",
        )

        assert response.status_code == 400
