# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box end-to-end: two real clients exchange files through the rendezvous relay.

The sibling ``hsync-rendezvous`` runs on loopback **with ``--relay``**; both clients
point at it instead of mDNS. The master advertises only undialable candidates
(TEST-NET-2 addresses injected through the test seam
``HSYNC_P2P_TEST_ADVERTISED_ENDPOINTS``), so every direct-candidate probe on the
slave fails and it must open a relayed tunnel: the node pairs the two peers, the
master bridges the relayed session to its own data plane (``[RELAY] listening``),
and the slave's HTTPS/WSS flow rides the tunnel (``[RELAY] master … relaying the
data plane``). The observable proof is both ``[RELAY]`` lines plus a real file
landing on the slave from the master — the same two-leg path a CGNAT deployment
uses.

Requires ``HSYNC_BB_P2P=1`` and the sibling rendezvous built; exercises the path
that ``tests/test_p2p_relay.py`` promises at the unit level.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .p2p_helpers import (
    _p2p_start,
    _rendezvous_start,
    _reserve_udp_port,
    _stop_rendezvous,
    _terminate,
    _wait_for_file,
    _wait_for_rendezvous,
    _wait_for_text_in_stderr,
)

_P2P_ENABLED = os.environ.get("HSYNC_BB_P2P") == "1"
_P2P_REASON = "rendezvous relay e2e needs the sibling rendezvous + two live clients; set HSYNC_BB_P2P=1 to enable"
# Test-only seam that makes the master publish undialable candidates (see
# hsync_client/p2p_rendezvous_rendezvous.py::_TEST_ADVERTISED_ENDPOINTS_ENV).
_ADV_ENV = "HSYNC_P2P_TEST_ADVERTISED_ENDPOINTS"

pytestmark = pytest.mark.skipif(not _P2P_ENABLED, reason=_P2P_REASON)


class TestRendezvousRelay:
    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_master_with_no_dialable_candidate_syncs_through_the_relay(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket, tmp_path
    ):
        """Two peers on \"different networks\" exchange a file via the node.

        The master's real listener stays up on loopback (where the relay
        acceptor bridges it), but its published candidates are TEST-NET-2
        addresses — dead for the slave, exactly like the server-reflexive
        guesses of a CGNAT master. All probes fail, so the slave's data plane is
        forced through the rendezvous.
        """
        rendezvous_port = _reserve_udp_port()
        ready_file = tmp_path / "rendezvous.ready"
        try:
            rendezvous = _rendezvous_start(rendezvous_port, ready_file, relay=True)
        except FileNotFoundError:
            pytest.skip("hsync-rendezvous is not built (sibling repo venv missing)")
        try:
            assert _wait_for_rendezvous(rendezvous, ready_file), "rendezvous did not become ready"
            rendezvous_args = (
                "--rendezvous-server",
                f"127.0.0.1:{rendezvous_port}",
                "--insecure",
            )

            master = _p2p_start(
                p2p_master_dir,
                p2p_bucket,
                prefer_master=True,
                extra_args=rendezvous_args,
                extra_env={
                    _ADV_ENV: "198.51.100.7:8443,198.51.100.8:8443",
                },
            )
            try:
                assert _wait_for_text_in_stderr(
                    master,
                    "Rendezvous: mDNS on the local network + rendezvous server(s)",
                    timeout=15,
                ), "the master did not select the rendezvous plan"
                # The serving role registers a relay listener with the node: this
                # is the master side of the tunnel.
                assert _wait_for_text_in_stderr(
                    master, "[RELAY] listening on the rendezvous", timeout=60
                ), "the master did not register a relay listener with the rendezvous"

                slave = _p2p_start(
                    p2p_slave_dir, p2p_bucket, never_master=True, extra_args=rendezvous_args
                )
                try:
                    assert _wait_for_text_in_stderr(
                        slave,
                        "Rendezvous: mDNS on the local network + rendezvous server(s)",
                        timeout=15,
                    )
                    became_master = _wait_for_text_in_stderr(
                        master, "MASTER", timeout=60
                    ) or _wait_for_text_in_stderr(slave, "MASTER", timeout=10)
                    assert became_master, "Neither peer became master"

                    # The slave probes the dead candidates, fails them all, and
                    # opens the relayed tunnel: the two-leg path line is the
                    # observable proof that the data plane rides the node.
                    assert _wait_for_text_in_stderr(
                        slave, "relaying the data plane through the rendezvous", timeout=120
                    ), "the slave did not fall back to the relayed tunnel"

                    content = b"relayed-through-the-rendezvous\n"
                    (Path(p2p_master_dir) / "notes.txt").write_bytes(content)

                    assert _wait_for_file(str(Path(p2p_slave_dir) / "notes.txt"), timeout=120), (
                        "File did not sync from master to slave through the rendezvous relay"
                    )
                    assert (Path(p2p_slave_dir) / "notes.txt").read_bytes() == content
                    assert master.poll() is None, "the master exited while relaying"
                finally:
                    _terminate(slave)
            finally:
                _terminate(master)
        finally:
            _stop_rendezvous(rendezvous)
