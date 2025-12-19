# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the ADR-0023 corrected-mtime tie-break (LAN mode).

Drives the real client through its public CLI surface and observes only
outcomes a user could: files on disk and the documented ``HSYNC_*``
environment. No internal module is imported.

Requires ``HSYNC_BB_LAN=1`` to run.
"""

import os
import time
from pathlib import Path

import pytest

from .lan_helpers import (
    _conflict_copies,
    _lan_start,
    _terminate,
    _wait_for_file,
    _wait_for_master,
    _wait_for_text_in_stderr,
)

_BB_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_BB_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _BB_LAN_ENABLED, reason=_BB_LAN_REASON)


def _dead_ntp_target() -> str:
    """An address+port guaranteed silent: a closed loopback TCP port."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{s.getsockname()[1]}"


# =============================================================================
# BB-3 / BB-4 — LAN P2P
# =============================================================================


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
