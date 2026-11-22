# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box: a deletion survives a restart of *both* peers.

The reported scenario: the user deletes a synced folder on one device while the
link to the master is down (the log signature is ``was deleted locally while
offline``), the deletion then reaches the master, the two desktop clients are
restarted, and the files come back.

The regression surface is what a user observes, never an internal store: after
the restart neither peer may recreate the deleted tree, and the restarted client
may not plan a download for it. A deletion is made durable by the causal
markers both peers keep (ADR-0003); a restart must not lose them.

The planted tree keeps the deletion well below the mass-deletion guard
(``sync.py::_mass_delete_blocked``), so the guard never masks the behaviour
under test.

Requires HSYNC_BB_P2P=1 to run.
"""

import os
import shutil
import time
from pathlib import Path

import pytest

from .p2p_helpers import (
    _p2p_ready_path,
    _p2p_start,
    _stderr_text,
    _terminate,
    _wait_for_client_ready,
    _wait_for_file,
    _wait_for_file_gone,
)

_P2P_ENABLED = os.environ.get("HSYNC_BB_P2P") == "1"
_P2P_REASON = (
    "P2P e2e needs loopback mDNS + two live client instances; set HSYNC_BB_P2P=1 to enable"
)

pytestmark = pytest.mark.skipif(not _P2P_ENABLED, reason=_P2P_REASON)

# Regression guard for the reported sequence: the slave recorded the offline
# deletion marker from a merge base that did not carry the master's clock, so the
# marker could not dominate the remote version and the file was downloaded again.
# `sync.py::_reconcile_tombstones` now inherits that clock from the remote listing
# when the remote copy is byte-identical to the deleted base.
# The album folder the user deleted, named like the reported one.
TREE = "Crono"
DELETED = tuple(f"{TREE}/{name}.mp3" for name in ("01 Symphony", "02 Steel", "03 Pull"))
# The rest of the share stays: the deletion is a minority of the files so the
# mass-deletion guard (50 files / 50 % by default) does not trip.
KEPT = tuple(f"Other/{name}.mp3" for name in ("a", "b", "c", "d", "e"))
ALL_FILES = DELETED + KEPT


def _plant(root: Path) -> None:
    for rel in ALL_FILES:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 4096)


def _start_ready(directory: str, bucket: str, *, timeout: float = 60, **kwargs):
    """Start a peer and wait for its own ready file (PID-checked)."""
    proc = _p2p_start(directory, bucket, **kwargs)
    assert _wait_for_client_ready(proc, _p2p_ready_path(directory), timeout=timeout), (
        f"client on {directory} never became ready\n{_stderr_text(proc)}"
    )
    return proc


def _wait_for_all_on_master(p2p_master_dir: str, slave) -> None:
    for rel in ALL_FILES:
        assert _wait_for_file(str(Path(p2p_master_dir) / rel), timeout=60), (
            f"{rel} never reached the master\n{_stderr_text(slave)}"
        )


def _wait_for_deleted_gone(p2p_master_dir: str, timeout: float = 90) -> bool | None:
    """Wait until every deleted file is gone from the master.

    The *directory* may legitimately survive as an empty shell (the engine keeps
    a ``.hsynckeep`` sentinel for an empty syncable directory), so the deleted
    files are what the deletion must remove.
    """
    for rel in DELETED:
        if not _wait_for_file_gone(str(Path(p2p_master_dir) / rel), timeout=timeout):
            return False
    return True


class TestDeletedTreeStaysDeletedAfterRestart:
    def test_a_deleted_tree_does_not_come_back_after_both_peers_restart(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket
    ):
        """Baseline: the deletion propagates while the link is live."""
        _run_scenario(p2p_master_dir, p2p_slave_dir, p2p_bucket, link_down_delete=False)

    def test_a_tree_deleted_while_the_link_is_down_does_not_come_back_after_settling(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket
    ):
        """The reported sequence, with the share settled before the deletion.

        A cycle after the upload has merged the master's clock into the merge
        base, so the deletion marker should dominate the remote version and the
        deletion propagate (``was deleted locally while offline``).
        """
        _run_scenario(
            p2p_master_dir,
            p2p_slave_dir,
            p2p_bucket,
            link_down_delete=True,
            settle_seconds=10.0,
        )

    def test_a_tree_deleted_right_after_the_upload_does_not_come_back(
        self, p2p_master_dir, p2p_slave_dir, p2p_bucket
    ):
        """Same sequence without settling: the merge base is still local-only."""
        _run_scenario(p2p_master_dir, p2p_slave_dir, p2p_bucket, link_down_delete=True)


def _sync_up_and_delete(
    p2p_master_dir: str,
    p2p_slave_dir: str,
    p2p_bucket: str,
    *,
    link_down_delete: bool,
    settle_seconds: float = 0.0,
) -> tuple:
    """Sync the planted tree up, delete it, and return the settled pair.

    With ``link_down_delete`` the master is stopped first, so the running slave
    records the deletion against the detached link and can only replay it when
    the master returns — the sequence the reported log shows. ``settle_seconds``
    waits that long after the upload before the link goes down; even without it
    the deletion must propagate, because the mint takes the master's clock from
    the remote listing for the content it deletes. Returns the live ``(master,
    slave)`` pair once the deletion has settled on both peers.
    """
    _plant(Path(p2p_slave_dir))

    master = _start_ready(p2p_master_dir, p2p_bucket, prefer_master=True)
    slave = _start_ready(p2p_slave_dir, p2p_bucket, never_master=True)
    try:
        _wait_for_all_on_master(p2p_master_dir, slave)
        if settle_seconds:
            time.sleep(settle_seconds)

        if link_down_delete:
            # The link is down: the master is gone, the slave keeps running and
            # its watcher records the deletion against the detached link.
            _terminate(master)
            time.sleep(2)
            shutil.rmtree(Path(p2p_slave_dir) / TREE)
            time.sleep(2)
            master = _start_ready(p2p_master_dir, p2p_bucket, prefer_master=True)
        else:
            shutil.rmtree(Path(p2p_slave_dir) / TREE)

        assert _wait_for_deleted_gone(p2p_master_dir, timeout=90), (
            "the deletion never reached the master\n"
            f"master:\n{_stderr_text(master)}\nslave:\n{_stderr_text(slave)}"
        )
        if link_down_delete:
            assert "was deleted locally while offline" in _stderr_text(slave), (
                "the slave did not replay the deletion recorded while offline\n"
                f"{_stderr_text(slave)}"
            )
        time.sleep(3)
        return master, slave
    except BaseException:
        _terminate(slave)
        _terminate(master)
        raise


def _run_scenario(
    p2p_master_dir: str,
    p2p_slave_dir: str,
    p2p_bucket: str,
    *,
    link_down_delete: bool,
    settle_seconds: float = 0.0,
) -> None:
    """Converge a deletion, restart both peers, and require it to stay deleted."""
    master, slave = _sync_up_and_delete(
        p2p_master_dir,
        p2p_slave_dir,
        p2p_bucket,
        link_down_delete=link_down_delete,
        settle_seconds=settle_seconds,
    )
    _terminate(slave)
    _terminate(master)

    # The reported trigger: both clients restart on the same device identity
    # and state (a desktop Stop/Start, or a reboot).
    master = _start_ready(p2p_master_dir, p2p_bucket, prefer_master=True)
    try:
        slave = _start_ready(p2p_slave_dir, p2p_bucket, never_master=True)
        try:
            # Give the restarted pair time to plan and apply a cycle: a
            # resurrection shows up as the deleted tree being recreated.
            time.sleep(10)
            slave_tail = _stderr_text(slave)
            for rel in DELETED:
                assert not (Path(p2p_slave_dir) / rel).exists(), (
                    f"{rel} came back on the restarted slave\n"
                    f"slave:\n{slave_tail}\nmaster:\n{_stderr_text(master)}"
                )
                assert not (Path(p2p_master_dir) / rel).exists(), (
                    f"{rel} came back on the restarted master\n"
                    f"master:\n{_stderr_text(master)}\nslave:\n{slave_tail}"
                )
            # A resurrection also shows up as a transfer: the restarted slave
            # must not download any of the deleted paths (its plan markers alone
            # are not enough — the cycle logs the transfer it actually runs).
            downloaded = [line for line in slave_tail.splitlines() if "[DOWNLOAD]" in line]
            for rel in DELETED:
                assert not any(rel in line for line in downloaded), (
                    f"the restarted slave downloaded {rel}\n{slave_tail}"
                )
            # What was kept must still be there: the deletion converged without
            # taking the rest of the share with it.
            for rel in KEPT:
                assert (Path(p2p_master_dir) / rel).exists(), (
                    f"{rel} disappeared from the master\n{_stderr_text(master)}"
                )
                assert (Path(p2p_slave_dir) / rel).exists(), (
                    f"{rel} disappeared from the slave\n{slave_tail}"
                )
        finally:
            _terminate(slave)
    finally:
        _terminate(master)
