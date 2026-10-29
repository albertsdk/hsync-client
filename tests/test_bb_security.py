# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso
"""Black-box **security probes** for the ``hsync-client`` p2p master (CI wiring).

Adversarial suite: each test is an attack attempt against the public surface
(the p2p master's HTTPS REST + WSS endpoints), never against the implementation
— the same probes must keep passing against any conforming deployment. Attack
classes covered:

* **Authentication** — no/malformed/tampered token, wrong scheme, unauthenticated
  writes refused.
* **Path traversal / confinement** — upload/download/delete/rename/permissions/
  create-dir/symlink traversal and symlink-target escape.
* **Upload integrity / DoS** — checksum mismatch/missing, engine state files,
  decompression bomb, far-future mtime clamp.
* **Tombstone gossip** — invalid JSON, non-object payload (and an opt-in heavy
  oversize probe).
* **Realtime WebSocket** — bad token, invalid cid.
* **Info disclosure** — unauthenticated health/time endpoints.

Opt-in: requires ``HSYNC_BB_SECURITY=1`` (security probes are adversarial by
nature, so they stay out of the default run — mirrors ``hsync-server``'s
``tests/test_bb_security.py`` and ``hsync-rendezvous``'s
``tests/test_bb_rendezvous_security.py``).

The shared probes live in ``pentest/probes.py``; the standalone runner is
``pentest/run_pentest.py``; the findings report is
``pentest/SECURITY_FINDINGS.md``.
"""

import asyncio
import os
import time
from unittest.mock import patch

import pytest

from hsync_client.p2p_server import P2PServer
from hsync_client.p2p_websocket import P2PWebSocketServer, SlaveCidRegistry
from pentest.probes import Pentest

_ENABLED = os.environ.get("HSYNC_BB_SECURITY") == "1"
_REASON = "p2p security probes are opt-in; set HSYNC_BB_SECURITY=1 to enable"
pytestmark = pytest.mark.skipif(not _ENABLED, reason=_REASON)

_KEY = "pentest-shared-secret"
_BUCKET = "pentest"
_MASTER_CID = "m" * 21


def _config(bucket: str) -> dict:
    return {"LOCAL_BUCKET": bucket, "P2P_KEY": _KEY, "P2P_PEER_DOWNLOAD": False}


@pytest.fixture
def pt(tmp_path):
    """An authenticated probe client against an in-process p2p master."""
    registry = SlaveCidRegistry(master_cid=_MASTER_CID)
    ws = P2PWebSocketServer(port=0, master_cid=_MASTER_CID, cid_registry=registry)
    server = P2PServer(port=0, use_https=False, cid_guard=registry, websocket_server=ws)
    server.start()
    time.sleep(0.1)
    config = _config(str(tmp_path))
    try:
        with (
            patch("hsync_client.p2p_server.CONFIG", config),
            patch("hsync_client.p2p_websocket.CONFIG", config),
        ):
            yield Pentest(f"http://127.0.0.1:{server.port}", _KEY, bucket=_BUCKET)
    finally:
        server.stop()


def _assert_secure(result):
    assert result.status == "SECURE", f"{result.id}: {result.detail}"


# =============================================================================
# Authentication
# =============================================================================
class TestAuth:
    def test_no_auth(self, pt):
        _assert_secure(pt.probe_no_auth())

    def test_bad_token(self, pt):
        _assert_secure(pt.probe_bad_token())

    def test_token_tamper(self, pt):
        _assert_secure(pt.probe_token_tamper())

    def test_wrong_scheme(self, pt):
        _assert_secure(pt.probe_wrong_scheme())

    def test_unauth_write(self, pt):
        _assert_secure(pt.probe_unauth_write())


# =============================================================================
# Path traversal / confinement
# =============================================================================
class TestPathTraversal:
    def test_upload_path(self, pt):
        _assert_secure(pt.probe_upload_path_traversal())

    def test_upload_filename(self, pt):
        _assert_secure(pt.probe_upload_filename_traversal())

    def test_download(self, pt):
        _assert_secure(pt.probe_download_traversal())

    def test_delete(self, pt):
        _assert_secure(pt.probe_delete_traversal())

    def test_rename(self, pt):
        _assert_secure(pt.probe_rename_traversal())

    def test_permissions(self, pt):
        _assert_secure(pt.probe_permissions_traversal())

    def test_create_dir(self, pt):
        _assert_secure(pt.probe_create_dir_traversal())

    def test_symlink_path(self, pt):
        _assert_secure(pt.probe_symlink_path_traversal())

    def test_symlink_target_escape(self, pt):
        _assert_secure(pt.probe_symlink_target_escape())


# =============================================================================
# Upload integrity / DoS
# =============================================================================
class TestUpload:
    def test_checksum_mismatch(self, pt):
        _assert_secure(pt.probe_upload_checksum_mismatch())

    def test_missing_checksum(self, pt):
        _assert_secure(pt.probe_upload_missing_checksum())

    def test_internal_name(self, pt):
        _assert_secure(pt.probe_upload_internal_name())

    def test_decompression_bomb(self, pt):
        _assert_secure(pt.probe_upload_decompression_bomb())

    def test_mtime_clamp(self, pt):
        result = pt.probe_mtime_clamp()
        # INFO when the clamp was not exercised (upload refused for another
        # reason); otherwise it must be SECURE.
        assert result.status in ("SECURE", "INFO"), f"{result.id}: {result.detail}"


# =============================================================================
# Tombstone gossip
# =============================================================================
class TestTombstones:
    def test_invalid_json(self, pt):
        _assert_secure(pt.probe_tombstone_invalid_json())

    def test_non_dict(self, pt):
        _assert_secure(pt.probe_tombstone_non_dict())

    def test_oversize(self, pt):
        # Heavy (default ~19 MB); INFO when HSYNC_PENTEST_HEAVY is unset.
        assert pt.probe_tombstone_oversize().status in ("SECURE", "INFO")


# =============================================================================
# Realtime WebSocket control plane
# =============================================================================
class TestWebSocket:
    def test_bad_token(self, pt):
        _assert_secure(asyncio.run(pt.probe_ws_bad_token()))

    def test_invalid_cid(self, pt):
        _assert_secure(asyncio.run(pt.probe_ws_invalid_cid()))


# =============================================================================
# Observations
# =============================================================================
class TestInfo:
    def test_health_public(self, pt):
        assert pt.probe_health_public().status == "INFO"

    def test_time_public(self, pt):
        assert pt.probe_time_public().status == "INFO"
