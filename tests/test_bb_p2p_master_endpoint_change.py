# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box: the slave re-selects the master when its endpoint changes.

The master and the slave meet through a rendezvous node (mDNS off, so the
rendezvous is the only discovery channel). The master first advertises its real
endpoints, so the slave connects directly. The test then rewrites the
``HSYNC_P2P_TEST_ADVERTISED_ENDPOINTS_FILE`` seam to dead TEST-NET-2 candidates:
the master republishes, the slave's advertised view changes, the endpoint guard
closes the slave WebSocket, and the slave re-selects — falling back to the
rendezvous relay. A file added on the master afterwards must still reach the
slave, with no restart.

Requires ``HSYNC_BB_P2P=1`` and the sibling rendezvous built.
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
    _wait_for_slave_events,
    _wait_for_text_in_stderr,
)

_P2P_ENABLED = os.environ.get("HSYNC_BB_P2P") == "1"
_P2P_REASON = "master-endpoint-change e2e needs the sibling rendezvous + two live clients; set HSYNC_BB_P2P=1 to enable"
# The runtime seam (see hsync_client/p2p_rendezvous_rdv.py::_test_advertised_endpoints_raw):
# the file is re-read on every publish, so a test can move a running master.
_ADV_FILE_ENV = "HSYNC_P2P_TEST_ADVERTISED_ENDPOINTS_FILE"

pytestmark = pytest.mark.skipif(not _P2P_ENABLED, reason=_P2P_REASON)


class TestMasterEndpointChange:
    @pytest.mark.flaky(reruns=2, reruns_delay=5)
    def test_slave_reselects_when_the_master_endpoint_changes(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket, tmp_path
    ):
        """The slave recovers after the master starts advertising a dead endpoint."""
        rendezvous_port = _reserve_udp_port()
        ready_file = tmp_path / "rendezvous.ready"
        try:
            rendezvous = _rendezvous_start(rendezvous_port, ready_file, relay=True)
        except FileNotFoundError:
            pytest.skip("hsync-rendezvous is not built (sibling repo venv missing)")
        seam = tmp_path / "advertised-endpoints"
        try:
            assert _wait_for_rendezvous(rendezvous, ready_file), "rendezvous did not become ready"
            rendezvous_args = (
                "--rendezvous-server",
                f"127.0.0.1:{rendezvous_port}",
                "--insecure",
                "--no-mdns",
            )
            master = _p2p_start(
                p2p_master_dir,
                p2p_bucket,
                prefer_master=True,
                extra_args=rendezvous_args,
                extra_env={_ADV_FILE_ENV: str(seam)},
            )
            try:
                assert _wait_for_text_in_stderr(
                    master, "Discovery: discovery server(s)", timeout=15
                ), "the master did not select the rendezvous-only plan"
                assert _wait_for_text_in_stderr(
                    master, "[RELAY] listening on the rendezvous", timeout=60
                ), "the master did not register a relay listener"

                slave = _p2p_start(
                    p2p_slave_dir, p2p_bucket, never_master=True, extra_args=rendezvous_args
                )
                try:
                    assert _wait_for_text_in_stderr(
                        slave, "Discovery: discovery server(s)", timeout=15
                    )
                    # The slave reaches the master directly (its real endpoints).
                    assert _wait_for_slave_events(slave, timeout=120), "the slave never linked"
                    first = Path(p2p_master_dir) / "before.txt"
                    first.write_bytes(b"before the move\n")
                    assert _wait_for_file(str(Path(p2p_slave_dir) / "before.txt"), timeout=120), (
                        "the direct path never delivered a file"
                    )

                    # The master moves network: it now advertises dead candidates.
                    seam.write_text("198.51.100.7:8443\n", encoding="utf-8")
                    assert _wait_for_text_in_stderr(
                        master,
                        "Advertised endpoint list changed; republishing",
                        timeout=120,
                    ), "the master did not republish the new candidates"
                    assert _wait_for_text_in_stderr(
                        slave,
                        "Master endpoint changed; closing the master WebSocket to re-select it",
                        timeout=180,
                    ), "the slave did not re-select the master endpoint"

                    # Recovery: a new file still reaches the slave (via the relay).
                    second = Path(p2p_master_dir) / "after.txt"
                    second.write_bytes(b"after the move\n")
                    assert _wait_for_file(str(Path(p2p_slave_dir) / "after.txt"), timeout=180), (
                        "the slave did not recover after the master moved"
                    )
                    assert (Path(p2p_slave_dir) / "after.txt").read_bytes() == b"after the move\n"
                    assert master.poll() is None, "the master exited while re-selecting"
                finally:
                    _terminate(slave)
            finally:
                _terminate(master)
        finally:
            _stop_rendezvous(rendezvous)
