# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box end-to-end: two clients discover and sync **through a bootnode**.

The sibling ``hsync-bootnode`` runs on loopback; both clients point at it
(``BOOTNODE 127.0.0.1 --port <p> --insecure --no-upnp``) instead of mDNS, so
this exercises the whole bootnode discovery plan: register/lease, query, peer
feed, endpoint publication and the readiness gate. Requires ``HSYNC_BB_P2P=1``
and skips when the sibling bootnode is not built.
"""

import os
from pathlib import Path

import pytest

from .p2p_helpers import (
    _bootnode_start,
    _p2p_start,
    _reserve_udp_port,
    _stop_bootnode,
    _terminate,
    _wait_for_bootnode,
    _wait_for_file,
    _wait_for_text_in_stderr,
)

_P2P_ENABLED = os.environ.get("HSYNC_BB_P2P") == "1"
_P2P_REASON = "bootnode e2e needs the sibling bootnode + two live clients; set HSYNC_BB_P2P=1"

pytestmark = pytest.mark.skipif(not _P2P_ENABLED, reason=_P2P_REASON)


class TestBootnodeDiscovery:
    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_peers_discover_and_sync_through_bootnode(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket, tmp_path
    ):
        """A force-master and a never-master peer find each other via the bootnode."""
        bootnode_port = _reserve_udp_port()
        ready_file = tmp_path / "bootnode.ready"
        try:
            bootnode = _bootnode_start(bootnode_port, ready_file)
        except FileNotFoundError:
            pytest.skip("hsync-bootnode is not built (sibling repo venv missing)")
        try:
            assert _wait_for_bootnode(bootnode, ready_file), "bootnode did not become ready"
            bootnode_args = (
                "127.0.0.1",
                "--port",
                str(bootnode_port),
                "--insecure",
                "--no-upnp",
            )

            master = _p2p_start(
                p2p_master_dir, p2p_bucket, force_master=True, extra_args=bootnode_args
            )
            try:
                # The banner proves the bootnode plan is the active one.
                assert _wait_for_text_in_stderr(master, "Discovery: bootnode", timeout=15), (
                    "the master did not select the bootnode plan"
                )
                slave = _p2p_start(
                    p2p_slave_dir, p2p_bucket, never_master=True, extra_args=bootnode_args
                )
                try:
                    assert _wait_for_text_in_stderr(slave, "Discovery: bootnode", timeout=15)
                    became_master = _wait_for_text_in_stderr(
                        master, "MASTER", timeout=60
                    ) or _wait_for_text_in_stderr(slave, "MASTER", timeout=10)
                    assert became_master, "Neither peer became master"

                    content = b"bootnode-origin\n"
                    (Path(p2p_master_dir) / "boot.txt").write_bytes(content)

                    assert _wait_for_file(str(Path(p2p_slave_dir) / "boot.txt"), timeout=30), (
                        "File did not sync from master to slave through the bootnode"
                    )
                    assert (Path(p2p_slave_dir) / "boot.txt").read_bytes() == content
                finally:
                    _terminate(slave)
            finally:
                _terminate(master)
        finally:
            _stop_bootnode(bootnode)


_NAT_ENABLED = os.environ.get("HSYNC_BB_P2P_NAT") == "1"


class TestBootnodeDiscoveryNat:
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
