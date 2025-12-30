# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the ADR-0023 corrected-mtime tie-break (server mode).

Drives the real client through its public CLI surface and observes only
outcomes a user could: exit codes, files on disk, the public REST listing and
the documented ``HSYNC_*`` environment. No internal module is imported.

Requires ``HSYNC_BB_SERVER=1`` to run.
"""

import os
import uuid
from pathlib import Path

import pytest

from .lan_helpers import _conflict_copies

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON)


def _sha(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


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


# =============================================================================
# BB-1 / BB-2 — server mode: the corrected mtime decides, never the raw clock
# =============================================================================


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


class TestBlackBoxNtpUnreachableServer:
    def test_dead_ntp_hosts_do_not_block_server_sync(
        self, live_server, bb_bucket, local_dir, run_sync_once, monkeypatch
    ):
        """A dead ``HSYNC_NTP_HOSTS`` must not break server-mode sync.

        In server mode the client takes its time authority from the server's
        ``/api/v1/time`` endpoint (``probe_server_clock_offset``), never from the
        external SNTP list (``HSYNC_NTP_HOSTS``, a LAN-mode probe). This is a
        regression guard: server mode must remain unaffected by a dead
        ``HSYNC_NTP_HOSTS`` and never accidentally consult external NTP, so a
        sync started with one set still completes and propagates the file.

        The genuine "NTP unreachable does not block sync" behaviour (degraded
        temporal mode, offset 0) is exercised in LAN mode by
        ``test_bb_lan_corrected_mtime``.
        """
        import socket

        # Given a dead NTP target (a closed loopback port) configured in the env
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead_ntp = f"127.0.0.1:{s.getsockname()[1]}"
        monkeypatch.setenv("HSYNC_NTP_HOSTS", dead_ntp)

        # When syncing a file with the dead NTP target configured
        content = b"still synchronises without NTP\n"
        (Path(local_dir) / "no_ntp.txt").write_bytes(content)
        result = run_sync_once(bb_bucket, local_dir)

        # Then the sync completes (server mode ignores HSYNC_NTP_HOSTS) and the
        # file reaches the server
        assert result.exit_code == 0
        listing = _server_listing(live_server, bb_bucket)
        names = {f["filepath"] for f in listing}
        assert "no_ntp.txt" in names
