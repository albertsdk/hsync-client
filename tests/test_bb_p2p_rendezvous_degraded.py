# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box: an unreachable rendezvous never stops local-network sync.

The rendezvous is an optional cross-network rendezvous source. With a configured
but dead node (a closed loopback UDP port) two peers on the same network must
still find each other over mDNS, elect a master and exchange files in both
directions, and the dead source must be reported as a warning rather than
ending the run. Requires HSYNC_BB_P2P=1.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .p2p_helpers import (
    _p2p_start,
    _reserve_udp_port,
    _terminate,
    _wait_for_file,
    _wait_for_text_in_stderr,
)

_P2P_ENABLED = os.environ.get("HSYNC_BB_P2P") == "1"
_P2P_REASON = (
    "P2P e2e needs loopback mDNS + two live client instances; set HSYNC_BB_P2P=1 to enable"
)

pytestmark = pytest.mark.skipif(not _P2P_ENABLED, reason=_P2P_REASON)


class TestUnreachableRendezvous:
    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_local_network_sync_continues_without_the_rendezvous(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket
    ):
        """Both peers sync over mDNS while the configured rendezvous is dead."""
        # Given a rendezvous configured on a loopback port nothing listens on
        dead_port = _reserve_udp_port()
        rendezvous_args = ("--rendezvous-server", f"127.0.0.1:{dead_port}", "--insecure")

        master = _p2p_start(
            p2p_master_dir, p2p_bucket, prefer_master=True, extra_args=rendezvous_args
        )
        try:
            # The combined plan is active: mDNS runs next to the rendezvous
            assert _wait_for_text_in_stderr(
                master, "Rendezvous: mDNS on the local network + rendezvous server(s)", timeout=15
            ), "the hybrid rendezvous plan is not active"
            assert _wait_for_text_in_stderr(master, "Registered mDNS service", timeout=15), (
                "Master mDNS service did not register"
            )

            slave = _p2p_start(
                p2p_slave_dir, p2p_bucket, never_master=True, extra_args=rendezvous_args
            )
            try:
                became_master = _wait_for_text_in_stderr(
                    master, "MASTER", timeout=60
                ) or _wait_for_text_in_stderr(slave, "MASTER", timeout=10)
                assert became_master, "Neither peer became master"

                # When the master writes a file
                content = b"lan-only\n"
                (Path(p2p_master_dir) / "shared.txt").write_bytes(content)

                # Then it still reaches the slave over the local network
                assert _wait_for_file(str(Path(p2p_slave_dir) / "shared.txt"), timeout=30), (
                    "File did not sync over mDNS while the rendezvous was unreachable"
                )
                assert (Path(p2p_slave_dir) / "shared.txt").read_bytes() == content
            finally:
                _terminate(slave)

            # The dead source never ends the run: the master is still serving
            # after the whole exchange (a fatal rendezvous error would have
            # exited it with the documented FATAL code).
            assert master.poll() is None, "the master exited while the rendezvous was unreachable"
        finally:
            _terminate(master)
