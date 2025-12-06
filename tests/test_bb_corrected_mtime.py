# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the ADR-0023 corrected-mtime tie-break.

Drives the real client through its public CLI surface and observes only
outcomes a user could: exit codes, files on disk, the public REST listing and
the documented ``HSYNC_*`` environment. No internal module is imported.

Requirements (same gates as the other black-box suites):
* server-mode scenarios need ``HSYNC_BB_SERVER=1``;
* LAN scenarios need ``HSYNC_BB_LAN=1``.
"""

import os
import time
import uuid
from pathlib import Path

import pytest

from .lan_helpers import (
    _lan_start,
    _terminate,
    _wait_for_file,
    _wait_for_master,
    _wait_for_text_in_stderr,
)

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"
server_skip = pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON)

_BB_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_BB_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)
lan_skip = pytest.mark.skipif(not _BB_LAN_ENABLED, reason=_BB_LAN_REASON)

# ---------------------------------------------------------------------------
# Black-box helpers (public surface only)
# ---------------------------------------------------------------------------


def _sha(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def _conflict_copies(local_dir: str, stem: str) -> list[Path]:
    """Conflict copies present on disk for a given file stem."""
    return [p for p in Path(local_dir).iterdir() if p.name.startswith(f"{stem}_conflicted_copy_")]


def _server_listing(live_server, bucket: str) -> list[dict]:
    """Full server-side listing via the public REST endpoint."""
    import requests

    base, api_key = live_server
    r = requests.get(
        f"{base}/api/v1/buckets/{bucket}/files",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15.0,
        verify=False,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _server_upload_other(live_server, bucket, name, content, other_cid, vclock, corrected_mtime):
    """Upload a file as another real client, using the public REST API.

    ``corrected_mtime`` is the ADR-0023 field a real client now sends.
    """
    import json

    import requests

    base, api_key = live_server
    r = requests.post(
        f"{base}/api/v1/buckets/{bucket}/files",
        data={
            "cid": other_cid,
            "path": "",
            "sha256sum": _sha(content),
            "permissions": "644",
            "offset": "0",
            "size": "0",
            "compressed": "false",
            "vclock": json.dumps(vclock),
            "version_id": "",
            "corrected_mtime": str(corrected_mtime),
        },
        files={"file": (name, content, "application/octet-stream")},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
        verify=False,
    )
    assert r.status_code == 201, f"{r.status_code} {r.text}"


def _dead_ntp_target() -> str:
    """An address+port guaranteed silent: a closed loopback TCP port."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{s.getsockname()[1]}"


# =============================================================================
# BB-1 / BB-2 — server mode: the corrected mtime decides, never the raw clock
# =============================================================================


@server_skip
class TestBlackBoxCorrectedMtimeServer:
    def test_recent_local_edit_wins_and_server_preserves_loser(
        self, live_server, bb_bucket, local_dir, run_sync_once
    ):
        """Local edit with the newer corrected mtime keeps the canonical path;
        the losing server-side content is preserved as a conflicted copy,
        visible in the public listing (no silent data loss)."""
        name = f"doc-{uuid.uuid4().hex[:8]}.txt"
        stem = name.split(".")[0]
        base = b"shared base\n"
        base_time = 1_700_000_000
        older = b"older edit by the other user\n"
        newer = b"newer edit made locally\n"

        # 1. Establish a common ancestor through a real CLI sync.
        p = Path(local_dir) / name
        p.write_bytes(base)
        os.utime(p, (base_time, base_time))
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0

        # 2. The other user uploads an "older" version (corrected_mtime lower),
        #    this client keeps an even newer local edit — raw mtime higher.
        other_cid = "OtherClient1234567890"
        _server_upload_other(
            live_server,
            bb_bucket,
            name,
            older,
            other_cid,
            {other_cid: 1},
            corrected_mtime=base_time + 10,
        )
        p.write_bytes(newer)
        os.utime(p, (base_time + 60, base_time + 60))

        # 3. Sync: the newer local edit must win the path; the server keeps
        #    the losing "older" content as a conflicted copy.
        result = run_sync_once(bb_bucket, local_dir)
        assert result.exit_code == 0
        listing = _server_listing(live_server, bb_bucket)
        canonical = [f for f in listing if f["filepath"] == name]
        assert canonical, f"{name} missing from server listing"
        assert canonical[0]["sha256sum"] == _sha(newer)
        server_copies = [
            f["filepath"] for f in listing if f"{stem}_conflicted_copy_" in f["filepath"]
        ]
        assert server_copies, "server did not preserve the losing content as a copy"
        # And the local file still holds the winner.
        assert p.read_bytes() == newer

    def test_remote_newer_corrected_mtime_beats_newer_raw_local_clock(
        self, live_server, bb_bucket, local_dir, run_sync_once
    ):
        """The corrected mtime decides the tie-break, not the raw local clock.

        The local file carries a plainly higher raw mtime, but the remote
        version's corrected_mtime is higher: the remote wins, the local
        content is preserved as a local conflict copy.
        """
        name = f"doc-{uuid.uuid4().hex[:8]}.txt"
        stem = name.split(".")[0]
        base = b"shared base\n"
        base_time = 1_700_000_000
        local_edit = b"local edit with newer raw mtime\n"
        remote_edit = b"remote edit with newer corrected time\n"

        p = Path(local_dir) / name
        p.write_bytes(base)
        os.utime(p, (base_time, base_time))
        assert run_sync_once(bb_bucket, local_dir).exit_code == 0

        other_cid = "OtherClient1234567890"
        # Remote corrected time far in the future of the local edit's raw mtime.
        _server_upload_other(
            live_server,
            bb_bucket,
            name,
            remote_edit,
            other_cid,
            {other_cid: 1},
            corrected_mtime=base_time + 9_000,
        )
        # Local raw mtime newer than the remote's *raw* arrival but its
        # corrected time (raw, since the loopback server offset is ~0) stays
        # below the remote corrected_mtime.
        p.write_bytes(local_edit)
        os.utime(p, (base_time + 120, base_time + 120))

        result = run_sync_once(bb_bucket, local_dir)
        # The cycle resolves a causal conflict and preserves the losing
        # local edit: the one-shot exit contract reports this as 4
        # (CONFLICTS, see docs/scheduling.md).
        assert result.exit_code == 4
        canonical = _server_listing(live_server, bb_bucket)
        entry = next(f for f in canonical if f["filepath"] == name)
        assert entry["sha256sum"] == _sha(remote_edit), (
            "remote corrected_mtime should beat the local raw clock"
        )
        # The losing local content is preserved as a conflict copy.
        copies = _conflict_copies(local_dir, stem)
        assert copies, "losing local edit was not preserved as a conflict copy"
        assert any(c.read_bytes() == local_edit for c in copies)
        assert p.read_bytes() == remote_edit


# =============================================================================
# BB-3 / BB-4 — LAN P2P
# =============================================================================


@lan_skip
class TestBlackBoxCorrectedMtimeLan:
    def test_divergence_keeps_newer_and_preserves_loser(
        self, lan_master_dir, lan_slave_dir, lan_bucket
    ):
        """Concurrent divergence on both peers: the more recent mtime keeps the
        canonical name and the losing content survives as a conflict copy."""
        name = "shared.txt"
        stem = name.split(".")[0]
        base = b"base\n"
        base_time = 1_700_000_000
        (Path(lan_master_dir) / name).write_bytes(base)
        os.utime(Path(lan_master_dir) / name, (base_time, base_time))

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=30), (
                    "slave never finished its initial sync"
                )

                newer = b"newer edit (slave side)\n"
                older = b"older edit (master side)\n"
                (Path(lan_slave_dir) / name).write_bytes(newer)
                os.utime(Path(lan_slave_dir) / name, (base_time + 120, base_time + 120))
                (Path(lan_master_dir) / name).write_bytes(older)
                os.utime(Path(lan_master_dir) / name, (base_time + 5, base_time + 5))

                # Convergence: the newer edit wins on every peer.
                slave_path = Path(lan_slave_dir) / name
                master_path = Path(lan_master_dir) / name
                deadline = time.time() + 45
                while time.time() < deadline:
                    if (
                        slave_path.exists()
                        and master_path.exists()
                        and slave_path.read_bytes() == newer
                        and master_path.read_bytes() == newer
                    ):
                        break
                    time.sleep(0.5)
                assert slave_path.read_bytes() == newer, "slave did not converge to the winner"
                assert master_path.read_bytes() == newer, "master did not converge to the winner"

                # The losing content survives somewhere as a conflicted copy.
                loser_kept = False
                for p in _conflict_copies(lan_master_dir, stem) + _conflict_copies(
                    lan_slave_dir, stem
                ):
                    if p.read_bytes() == older:
                        loser_kept = True
                        break
                assert loser_kept, "losing content was not preserved as a conflict copy"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)

    def test_unreachable_ntp_does_not_block_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """With an unreachable NTP host the LAN sync still completes (offset 0,
        degraded temporal mode) and files propagate normally."""
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)
            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_env={"HSYNC_NTP_HOSTS": _dead_ntp_target()},
            )
            try:
                assert _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=30), (
                    "slave never finished its initial sync"
                )
                content = b"still synchronises without NTP\n"
                (Path(lan_master_dir) / "offline.txt").write_bytes(content)
                assert _wait_for_file(str(Path(lan_slave_dir) / "offline.txt"), timeout=30), (
                    "file never propagated to slave while NTP was unreachable"
                )
                assert (Path(lan_slave_dir) / "offline.txt").read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
