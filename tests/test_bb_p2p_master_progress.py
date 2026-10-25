# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box: the serving master publishes transfer progress for its peers.

``--events-json-fd`` is the channel a GUI consumes, and when the Desktop starts
first it is the master: it only *serves* (and receives peers' pushes), so those
paths must report the same ``progress`` samples and terminal ``transfer`` events
the syncing side has always reported. This suite drives the real wire (two live
clients over loopback mDNS) and asserts what a GUI would read.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from .p2p_helpers import (
    _p2p_start,
    _terminate,
    _wait_for_file,
    _wait_for_master,
)

_P2P_ENABLED = os.environ.get("HSYNC_BB_P2P") == "1"
_P2P_REASON = (
    "P2P e2e needs loopback mDNS + two live client instances; set HSYNC_BB_P2P=1 to enable"
)

pytestmark = pytest.mark.skipif(not _P2P_ENABLED, reason=_P2P_REASON)


def _events(events_path: str) -> list[dict]:
    """Parse the JSON-Lines event stream the master has written so far."""
    path = Path(events_path)
    if not path.exists():
        return []
    parsed: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except ValueError:
            continue
    return parsed


def _wait_until(predicate, *, timeout: float = 30.0) -> bool:
    """Poll *predicate* until it is true or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.3)
    return predicate()


class TestMasterProgressEvents:
    """The master's own event stream carries the transfers of both directions."""

    def test_master_reports_served_and_received_transfers(
        self, p2p_bucket, p2p_master_dir, p2p_slave_dir, tmp_path
    ):
        content = (b"master-to-slave-" * 200_000)[: 3 * 1024 * 1024]
        (Path(p2p_master_dir) / "served.bin").write_bytes(content)
        pushed = b"slave-to-master\n" * 8_192
        (Path(p2p_slave_dir) / "pushed.bin").write_bytes(pushed)

        events_path = str(tmp_path / "master-events.jsonl")
        master = _p2p_start(
            p2p_master_dir,
            p2p_bucket,
            prefer_master=True,
            events_path=events_path,
        )
        try:
            _wait_for_master(master, p2p_slave_dir, p2p_bucket)
            slave = _p2p_start(p2p_slave_dir, p2p_bucket, never_master=True, extra_args=("--once",))
            try:
                assert _wait_for_file(str(Path(p2p_slave_dir) / "served.bin"), timeout=60)
                assert _wait_for_file(str(Path(p2p_master_dir) / "pushed.bin"), timeout=60), (
                    "the slave's own file never reached the master"
                )
                # Terminal events follow the commits: wait for both before
                # asserting, so the check is not racing the response path.
                assert _wait_until(
                    lambda: (
                        {"upload", "download"}
                        <= {
                            e.get("op")
                            for e in _events(events_path)
                            if e.get("event") == "transfer"
                        }
                    )
                ), f"missing terminal events: {_events(events_path)[-5:]}"
            finally:
                _terminate(slave)

            events = _events(events_path)
            progress = [e for e in events if e.get("event") == "progress"]
            transfers = [e for e in events if e.get("event") == "transfer"]

            served = [e for e in progress if e.get("file", "").endswith("served.bin")]
            assert served, "the master must report progress for a file it serves"
            assert all(e["op"] == "upload" for e in served)
            # A compressed serve reports the wire size as its total; either way
            # the final sample must be the complete payload.
            assert served[-1]["transferred"] == served[-1]["total"] > 0

            served_terminal = [
                e
                for e in transfers
                if e.get("op") == "upload" and e.get("file", "").endswith("served.bin")
            ]
            assert served_terminal, "the served file must end with a transfer event"
            assert all(e["size"] == len(content) for e in served_terminal)

            received = [e for e in progress if e.get("file", "").endswith("pushed.bin")]
            assert received, "the master must report the upload it receives"
            assert all(e["op"] == "download" for e in received)
            assert received[-1]["transferred"] == received[-1]["total"] == len(pushed)

            received_terminal = [
                e
                for e in transfers
                if e.get("op") == "download" and e.get("file", "").endswith("pushed.bin")
            ]
            assert received_terminal, "the received file must end with a transfer event"
            assert all(e["size"] == len(pushed) for e in received_terminal)
        finally:
            _terminate(master)
