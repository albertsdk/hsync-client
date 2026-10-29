# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box end-to-end: two clients discover and sync **through a rendezvous**.

The sibling ``hsync-rendezvous`` runs on loopback; both clients point at it
(``RENDEZVOUS 127.0.0.1 --port <p> --insecure``) instead of mDNS, so
this exercises the whole rendezvous rendezvous plan: register/lease, query, peer
feed, endpoint publication and the readiness gate. Requires ``HSYNC_BB_P2P=1``
and skips when the sibling rendezvous is not built.
"""

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
_P2P_REASON = "rendezvous e2e needs the sibling rendezvous + two live clients; set HSYNC_BB_P2P=1"

pytestmark = pytest.mark.skipif(not _P2P_ENABLED, reason=_P2P_REASON)


class TestRendezvousRendezvous:
    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_peers_discover_and_sync_through_rendezvous(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket, tmp_path
    ):
        """A prefer-master and a never-master peer find each other via the rendezvous."""
        rendezvous_port = _reserve_udp_port()
        ready_file = tmp_path / "rendezvous.ready"
        try:
            rendezvous = _rendezvous_start(rendezvous_port, ready_file)
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
                p2p_master_dir, p2p_bucket, prefer_master=True, extra_args=rendezvous_args
            )
            try:
                # The banner proves the rendezvous plan is the active one.
                assert _wait_for_text_in_stderr(
                    master,
                    "Rendezvous: mDNS on the local network + rendezvous server(s)",
                    timeout=15,
                ), "the master did not select the rendezvous plan"
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

                    content = b"rendezvous-origin\n"
                    (Path(p2p_master_dir) / "boot.txt").write_bytes(content)

                    # The rendezvous plan paces register/refresh/query on the
                    # spec's 30 s window (``rdv-1.md``): each peer spends its
                    # first lease cycle publishing its endpoint, so mutual
                    # rendezvous lands in the *next* window (~40 s after the
                    # peers start). 30 s was a one-window budget and lost that
                    # race systematically; two windows plus the sync itself
                    # keep the assertion about the wire path, not the clock.
                    assert _wait_for_file(str(Path(p2p_slave_dir) / "boot.txt"), timeout=90), (
                        "File did not sync from master to slave through the rendezvous"
                    )
                    assert (Path(p2p_slave_dir) / "boot.txt").read_bytes() == content
                finally:
                    _terminate(slave)
            finally:
                _terminate(master)
        finally:
            _stop_rendezvous(rendezvous)


_NAT_ENABLED = os.environ.get("HSYNC_BB_P2P_NAT") == "1"


class TestRendezvousRendezvousNat:
    """Opt-in real-NAT checks (``HSYNC_BB_P2P_NAT=1``; skipped in CI/loopback)."""

    @pytest.mark.skipif(
        not _NAT_ENABLED,
        reason="real-NAT check needs a live UPnP/IGD; set HSYNC_BB_P2P_NAT=1",
    )
    @pytest.mark.flaky(reruns=2, reruns_delay=5)
    def test_upnp_mapper_installs_and_removes_a_tcp_mapping(self):
        """A live IGD maps the master's TCP port and the mapping is torn down."""
        import socket

        from hsync_client.upnp import UpnpMapper

        with socket.socket() as probe:
            probe.bind(("", 0))
            port = int(probe.getsockname()[1])

        mapper = UpnpMapper()
        external = mapper.map_port(port)
        try:
            if external is None:
                pytest.skip("no UPnP/IGD reachable on this network")
            assert external == port
            assert mapper.active is True
        finally:
            mapper.unmap()
        assert mapper.active is False
