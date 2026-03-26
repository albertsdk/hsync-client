# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for LAN advanced topology, filtering, throttling, and file attributes.

Requires HSYNC_BB_LAN=1 to run.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

from .lan_helpers import (
    _lan_start,
    _stderr_text,
    _terminate,
    _wait_for_file,
    _wait_for_file_gone,
    _wait_for_master,
    _wait_for_slave_events,
    _wait_for_text_in_stderr,
)

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)


# =============================================================================
# Tombstone propagation (offline deletion catch-up)
# =============================================================================


class TestLanTombstone:
    def test_tombstone_propagation_on_reconnect(
        self, lan_master_dir, lan_slave_dir, tmp_path, lan_bucket
    ):
        """File deleted while slave is offline → tombstone cleans it up on reconnect."""
        # Given a file on the master synced to a slave that then disconnects
        bridge_dir = str(tmp_path / "bridge")
        Path(bridge_dir).mkdir()

        (Path(lan_master_dir) / "pre_delete.txt").write_bytes(b"to-be-deleted\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Phase 1: first slave syncs the file, then disconnects
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "pre_delete.txt"), timeout=30), (
                    "File did not sync from master to slave during initial sync"
                )
            finally:
                _terminate(slave1)

            # When the file is deleted on the master (tombstone recorded)
            bridge = _lan_start(bridge_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_text_in_stderr(master, "File watcher started", timeout=30), (
                    "Master file watcher did not start"
                )
                # Readiness threshold: servers + watcher are up, the master is
                # serving — the observable [READY] line must follow.
                assert _wait_for_text_in_stderr(master, "[READY]", timeout=30), (
                    "Master did not reach the [READY] threshold"
                )
                # Delete the file on master — the watcher detects it and
                # records a tombstone
                (Path(lan_master_dir) / "pre_delete.txt").unlink()
                assert _wait_for_text_in_stderr(master, "[LOCAL DELETE]", timeout=15), (
                    "Master did not detect the deletion and record a tombstone"
                )
            finally:
                _terminate(bridge)

            # Then, on reconnect, the slave reconciles the tombstone and
            # deletes the stale local file
            slave2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file_gone(
                    str(Path(lan_slave_dir) / "pre_delete.txt"), timeout=30
                ), "Deleted file still present on slave after reconnect (tombstone missed)"
            finally:
                _terminate(slave2)
        finally:
            _terminate(master)

    def test_slave_deletion_survives_master_restart(
        self, lan_master_dir, lan_slave_dir, lan_bucket
    ):
        """Slave deletes while the master is down; on reconnect the deletion
        propagates to the master instead of resurrecting the file on the slave.

        Regression for the reported scenario: A/B killed, AAA deleted on the
        still-running (or freshly reconnecting) C, A restarted with the same
        identity — AAA and its file previously reappeared on C. The deletion
        must survive locally (causal tombstone, ADR-0003) and the stale remote
        copy must be deleted, never downloaded back.
        """
        import shutil

        # Given a nested file synced to the slave
        aaa = Path(lan_master_dir) / "AAA"
        aaa.mkdir(parents=True)
        (aaa / "test.txt").write_bytes(b"delete-me\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Phase 1: the file syncs to the slave (its merge base is recorded).
                assert _wait_for_file(str(Path(lan_slave_dir) / "AAA" / "test.txt"), timeout=60), (
                    "File did not sync from master to slave during initial sync"
                )
                assert _wait_for_text_in_stderr(slave, "Initial sync complete", timeout=60), (
                    "Slave did not complete the initial sync"
                )

                # When the master dies and the whole AAA tree is deleted on the
                # (now offline) slave
                _terminate(master)
                assert _wait_for_text_in_stderr(slave, "Client file watcher stopped", timeout=30), (
                    "Slave watcher did not stop after the master went down"
                )
                shutil.rmtree(Path(lan_slave_dir) / "AAA")

                # And the master restarts with the same identity, so the slave
                # reconnects
                master2 = _lan_start(lan_master_dir, lan_bucket, force_master=True)
                try:
                    # Then the slave mints a tombstone and deletes the stale
                    # remote copy instead of downloading it back.
                    assert _wait_for_text_in_stderr(slave, "[TOMBSTONE]", timeout=90), (
                        "Slave did not reconcile the deletion via tombstone\n"
                        f"--- slave log tail ---\n{_stderr_text(slave)[-3000:]}"
                    )
                    assert _wait_for_file_gone(
                        str(Path(lan_master_dir) / "AAA" / "test.txt"), timeout=60
                    ), "Deleted file still present on master after reconnect (delete_remote missed)"
                    # The slave must not resurrect the deleted file.
                    time.sleep(3)
                    assert not (Path(lan_slave_dir) / "AAA" / "test.txt").exists(), (
                        "Deleted file resurrected on the slave after reconnect"
                    )
                finally:
                    _terminate(master2)
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Multi-slave topology (3 peers)
# =============================================================================


class TestLanMultiSlave:
    def test_multiple_slaves_converge(self, lan_master_dir, lan_slave_dir, tmp_path, lan_bucket):
        """Master + 2 slaves: file from slave1 appears on slave2 via master."""
        # Given a master and two slaves
        slave2_dir = str(tmp_path / "slave2")
        Path(slave2_dir).mkdir()

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Start both slaves first so the election runs (master only emits
            # "MASTER" after discovering a peer via mDNS).
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            slave2 = _lan_start(slave2_dir, lan_bucket, never_master=True)
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                "Master did not become ready"
            )
            try:
                # Wait for both event channels: slave1's watcher must be live
                # to upload and slave2 must be listening to receive the
                # broadcast (see _wait_for_slave_events).
                assert _wait_for_slave_events(slave1), "Slave1 is not listening for events"
                assert _wait_for_slave_events(slave2), "Slave2 is not listening for events"

                # When slave1 creates a file
                (Path(lan_slave_dir) / "from_slave1.txt").write_bytes(b"via-master\n")

                # Then it converges on both master and slave2
                assert _wait_for_file(str(Path(lan_master_dir) / "from_slave1.txt"), timeout=30), (
                    "Slave1 file did not reach master"
                )
                assert _wait_for_file(str(Path(slave2_dir) / "from_slave1.txt"), timeout=30), (
                    "Slave1 file did not reach slave2 via master"
                )

                assert (Path(slave2_dir) / "from_slave1.txt").read_bytes() == b"via-master\n"
            finally:
                _terminate(slave1)
                _terminate(slave2)
        finally:
            _terminate(master)


# =============================================================================
# One-shot control
# =============================================================================


class TestOneShot:
    def test_once_exits_when_alone(self, lan_master_dir, lan_bucket):
        """--once exits instead of waiting when no peers are found."""
        # Given a LAN client started with --once and no peers
        proc = _lan_start(
            lan_master_dir,
            lan_bucket,
            extra_args=("--once",),
        )
        try:
            # When no peers are discovered within the discovery timeout
            try:
                exit_code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("--once did not exit when no peers were found")
            # Then the client exits (0 = clean, 5 = LAN_NO_PEER)
            assert exit_code in (0, 5), f"Unexpected exit code: {exit_code}"
        finally:
            _terminate(proc)


# =============================================================================
# Port override
# =============================================================================


class TestLanPortOverride:
    def test_lan_port_override(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--listen-port on master: slave connects on the specified port."""
        from hsync_client.lan import get_free_port

        # Use a free ephemeral port instead of a fixed one: a fixed port can
        # collide with a lingering listener from a previous run (or a parallel
        # CI job) and, unlike the LAN transport itself, has no fallback.
        port = get_free_port()
        # Given a master listening on a custom port
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True, lan_port=port)
        try:
            # Start a slave so the master discovers it and runs the election
            # (master only emits "MASTER" after add_peer triggers _elect_master).
            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_text_in_stderr(master, "MASTER", timeout=30), (
                    "Master did not become ready"
                )
                # Then the master uses the specified port
                stderr = _stderr_text(master)
                assert f"Master HTTP server on port {port}" in stderr, (
                    "Master did not use the specified port"
                )

                # Wait for the slave to be ready before mutating: a file written
                # in the post-initial-sync blind spot is only recovered by the
                # periodic resync (300s), far beyond the sync timeout below.
                assert _wait_for_slave_events(slave), "slave did not reach [READY]"

                # And a file still syncs over that port
                (Path(lan_master_dir) / "port_test.txt").write_bytes(b"custom port\n")
                assert _wait_for_file(str(Path(lan_slave_dir) / "port_test.txt"), timeout=30), (
                    "File did not sync with custom master port"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Master failover (hard crash → deterministic re-election)
# =============================================================================


class TestMasterFailover:
    def test_master_death_reelects_successor(self, lan_master_dir, tmp_path, lan_bucket):
        """A hard-crashed master is replaced by the surviving peers, unassisted.

        Regression: with only two nodes the dead master's mDNS record was never
        re-evaluated (a hard-crashed peer stops announcing and sends no goodbye),
        so the survivor could wait indefinitely for a master that would never
        answer. The periodic staleness sweep must demote the dead master within
        ``HSYNC_PEER_TIMEOUT`` and the deterministic election must promote the
        earliest-started survivor, which then resumes serving.
        """
        # Given a serving master and two neutral survivors that sync from it
        peer_a = str(tmp_path / "peer_a")
        peer_b = str(tmp_path / "peer_b")
        Path(peer_a).mkdir()
        Path(peer_b).mkdir()

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            a = _lan_start(peer_a, lan_bucket)
            try:
                b = _lan_start(peer_b, lan_bucket)
                try:
                    assert _wait_for_text_in_stderr(master, "[READY]", timeout=30), (
                        "Master did not reach [READY]"
                    )
                    (Path(lan_master_dir) / "before.txt").write_bytes(b"before\n")
                    assert _wait_for_file(str(Path(peer_a) / "before.txt"), timeout=30), (
                        "peer_a did not sync from the master"
                    )
                    assert _wait_for_file(str(Path(peer_b) / "before.txt"), timeout=30), (
                        "peer_b did not sync from the master"
                    )

                    # When the master is hard-killed (no mDNS goodbye, no clean
                    # WSS close — only the staleness sweep can detect it)
                    master.kill()
                    master.wait()

                    # Then the earliest-started survivor is re-elected and
                    # resumes serving.
                    assert _wait_for_text_in_stderr(a, "MASTER", timeout=45), (
                        "peer_a was not elected master after the crash"
                    )

                    (Path(peer_a) / "after.txt").write_bytes(b"after\n")
                    assert _wait_for_file(str(Path(peer_b) / "after.txt"), timeout=45), (
                        "Failover did not resume sync to peer_b"
                    )
                finally:
                    _terminate(b)
            finally:
                _terminate(a)
        finally:
            _terminate(master)

    def test_quiescent_slaves_failover_to_one_successor(self, lan_master_dir, tmp_path, lan_bucket):
        """The heartbeat keeps idle slaves fresh so failover elects one successor.

        Regression: an idle slave's TXT record never changes, so without the
        periodic heartbeat its ``last_seen`` goes stale; when the master then
        dies, every surviving slave falsely expires the others and self-elects
        (split-brain). The heartbeat must keep every peer fresh so the
        deterministic election converges on a single successor.
        """
        peer_a = str(tmp_path / "peer_a")
        peer_b = str(tmp_path / "peer_b")
        Path(peer_a).mkdir()
        Path(peer_b).mkdir()

        # Tight liveness window (heartbeat 2s, timeout 5s is the tightest valid
        # HSYNC_PEER_TIMEOUT >= 2 * HSYNC_HEARTBEAT_INTERVAL).
        fast_env = {"HSYNC_HEARTBEAT_INTERVAL": "2", "HSYNC_PEER_TIMEOUT": "5"}

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True, extra_env=fast_env)
        try:
            a = _lan_start(peer_a, lan_bucket, extra_env=fast_env)
            try:
                b = _lan_start(peer_b, lan_bucket, extra_env=fast_env)
                try:
                    assert _wait_for_text_in_stderr(master, "[READY]", timeout=30), (
                        "master did not reach [READY]"
                    )
                    assert _wait_for_text_in_stderr(a, "[READY]", timeout=30), (
                        "peer_a did not reach [READY]"
                    )
                    assert _wait_for_text_in_stderr(b, "[READY]", timeout=30), (
                        "peer_b did not reach [READY]"
                    )
                    # Quiesce longer than the peer timeout: without the heartbeat
                    # both slaves' records would already be stale here, and the
                    # failover below would split-brain.
                    time.sleep(7)

                    master.kill()
                    master.wait()

                    # Exactly one survivor must self-elect (no split-brain); the
                    # winner is whichever peer started first, so accept either.
                    winner = _wait_for_text_in_stderr(a, "MASTER", timeout=90) or (
                        _wait_for_text_in_stderr(b, "MASTER", timeout=15)
                    )
                    assert winner, "no survivor was elected after the crash"
                    # Let a spurious self-election + yield (split-brain) settle.
                    time.sleep(5)

                    a_count = _stderr_text(a).count("This client elected as MASTER")
                    b_count = _stderr_text(b).count("This client elected as MASTER")
                    assert a_count + b_count == 1, (
                        f"split-brain: peer_a elected {a_count}x, peer_b {b_count}x\n"
                        f"peer_a:\n{_stderr_text(a)}\npeer_b:\n{_stderr_text(b)}"
                    )

                    # The surviving pair still syncs master -> slave.
                    src_dir, dst_dir = (peer_a, peer_b) if a_count == 1 else (peer_b, peer_a)
                    (Path(src_dir) / "after.txt").write_bytes(b"after\n")
                    assert _wait_for_file(str(Path(dst_dir) / "after.txt"), timeout=60), (
                        "Failover did not resume sync"
                    )
                finally:
                    _terminate(b)
            finally:
                _terminate(a)
        finally:
            _terminate(master)


class TestNoSplitBrainOnQuiescentPeer:
    """A WSS-connected slave is never falsely self-elected after quiescence.

    Regression: a healthy but idle peer's mDNS record is not re-announced
    (zeroconf only re-delivers a TXT record when its *value* changes), so its
    ``last_seen`` goes stale. Before the WebSocket-authoritative liveness fix,
    a newly joining peer triggered ``_elect_master`` on the stale slave, which
    excluded its (still-connected) master and self-elected — a split-brain. The
    WebSocket link must keep the master alive regardless of mDNS staleness.
    """

    def test_slave_does_not_self_elect_after_quiescence(self, lan_master_dir, tmp_path, lan_bucket):
        peer_a = str(tmp_path / "peer_a")
        peer_b = str(tmp_path / "peer_b")
        Path(peer_a).mkdir()
        Path(peer_b).mkdir()

        # Shorten the liveness window so staleness manifests in seconds rather
        # than the production 15s default (heartbeat 1s, timeout 3s is the
        # tightest valid HSYNC_PEER_TIMEOUT >= 2 * HSYNC_HEARTBEAT_INTERVAL).
        fast_env = {"HSYNC_HEARTBEAT_INTERVAL": "1", "HSYNC_PEER_TIMEOUT": "3"}

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True, extra_env=fast_env)
        try:
            a = _lan_start(peer_a, lan_bucket, extra_env=fast_env)
            try:
                assert _wait_for_text_in_stderr(master, "[READY]", timeout=30), (
                    "master did not reach [READY]"
                )
                assert _wait_for_text_in_stderr(a, "[READY]", timeout=30), (
                    "peer_a did not reach [READY]"
                )
                # Let both peers' mDNS records go stale while their WebSocket
                # links stay up (the quiescence window that used to split-brain).
                time.sleep(5)

                b = _lan_start(peer_b, lan_bucket, extra_env=fast_env)
                try:
                    assert _wait_for_text_in_stderr(b, "[READY]", timeout=30), (
                        "peer_b did not reach [READY]"
                    )
                    # Allow the join-triggered election to settle.
                    time.sleep(3)

                    a_log = _stderr_text(a)
                    assert "This client elected as MASTER" not in a_log, (
                        "peer_a self-elected after peer_b joined (split-brain):\n" + a_log
                    )
                    assert "Master changed" not in _stderr_text(master), (
                        "the force_master master churned its mastership:\n" + _stderr_text(master)
                    )
                finally:
                    _terminate(b)
            finally:
                _terminate(a)
        finally:
            _terminate(master)


# =============================================================================
# Leadership lease / yield (ADR-0027)
# =============================================================================


class TestMasterYield:
    def test_force_master_takeover_yields_incumbent(self, lan_master_dir, tmp_path, lan_bucket):
        """A force_master peer takes over, and the incumbent yields.

        The yield path (ADR-0027) fires when a live master loses the election
        to another ``server_ready`` peer, which in a healthy LAN only happens on
        network-partition heal — not simulatable on loopback mDNS. To exercise
        it deterministically, the incumbent is started with
        ``HSYNC_BB_MASTER_READY_DELAY`` (a test seam that widens the
        "elected but not yet server_ready" window): the force_master peer wins
        the election, becomes ``server_ready``, and the incumbent must then
        **yield** — log ``[MASTER YIELD]`` and revoke its ``server_ready``
        incumbency instead of remaining a "zombie incumbent".
        """
        # Given a regular incumbent (with a readiness-delay seam) and a peer
        # to elect against
        regular = str(tmp_path / "regular")
        fm_dir = str(tmp_path / "force_master")
        Path(regular).mkdir()
        Path(fm_dir).mkdir()

        reg = _lan_start(regular, lan_bucket, extra_env={"HSYNC_BB_MASTER_READY_DELAY": "10"})
        try:
            nm = _lan_start(lan_master_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_text_in_stderr(reg, "MASTER", timeout=30), (
                    "regular node did not win the election and start serving"
                )

                # When a force_master peer joins and takes over the mastership
                fm = _lan_start(fm_dir, lan_bucket, force_master=True)
                try:
                    assert _wait_for_text_in_stderr(fm, "MASTER", timeout=45), (
                        "force_master peer did not take over the mastership"
                    )

                    # Then the incumbent yields its mastership
                    if not _wait_for_text_in_stderr(reg, "[MASTER YIELD]", timeout=30):
                        # Diagnostic: on a flaky yield, surface the subprocess
                        # logs so the failure can be triaged (the pipes die with
                        # the Popen in teardown and the log would otherwise be
                        # lost).
                        print(f"\n--- [DIAG] incumbent log ---\n{_stderr_text(reg)}")
                        print(f"\n--- [DIAG] force_master log ---\n{_stderr_text(fm)}")
                        pytest.fail("the incumbent did not yield (no [MASTER YIELD] marker)")
                finally:
                    _terminate(fm)
            finally:
                _terminate(nm)
        finally:
            _terminate(reg)


# =============================================================================
# Filter path (subdirectory sync)
# =============================================================================


class TestLanFilterPath:
    def test_filter_path_restricts_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--subdir subdir/ restricts sync to that subdirectory only."""
        # Given two subdirectories on the master
        sub_a = Path(lan_master_dir) / "sub_a"
        sub_a.mkdir()
        (sub_a / "a.txt").write_bytes(b"in-a\n")

        sub_b = Path(lan_master_dir) / "sub_b"
        sub_b.mkdir()
        (sub_b / "b.txt").write_bytes(b"in-b\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Start a slave so the master discovers it and runs the election.
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # When the slave starts with --subdir sub_a
            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--subdir", "sub_a"),
            )
            try:
                # Then the file in sub_a syncs…
                assert _wait_for_file(str(Path(lan_slave_dir) / "sub_a" / "a.txt"), timeout=30), (
                    "File in filtered subdirectory did not sync"
                )
                # …and sub_b is never synced
                time.sleep(5)
                assert not (Path(lan_slave_dir) / "sub_b").exists(), (
                    "File outside filter path was synced"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Bandwidth throttle
# =============================================================================


class TestLanBandwidthThrottle:
    def test_bandwidth_throttle_allows_sync(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """--limit-download with a generous limit still allows sync."""
        # Given a serving master
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Start a slave so the master discovers it and runs the election.
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # And a slave with a generous bandwidth limit
            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=(
                    "--limit-download",
                    "10MB",
                    "--limit-upload",
                    "10MB",
                ),
            )
            try:
                # When a moderately sized file is created on the master
                content = b"x" * 50000  # 50 KB
                (Path(lan_master_dir) / "throttled.txt").write_bytes(content)

                # Then it still syncs under the throttle
                slave_file = Path(lan_slave_dir) / "throttled.txt"
                assert _wait_for_file(str(slave_file), timeout=30), (
                    "File did not sync with bandwidth throttle"
                )
                assert slave_file.read_bytes() == content
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# hsyncignore hot-reload
# =============================================================================


class TestLanHsyncignore:
    @pytest.mark.flaky(reruns=3, reruns_delay=5)
    def test_hsyncignore_reload_respected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """Adding .hsyncignore stops syncing matching files but keeps existing copies.

        This encodes the industry-standard "exclude protects, it does not
        delete" contract (rsync ``--exclude`` without ``--delete-excluded``,
        Syncthing ``.stignore``): a newly added ignore pattern must stop
        propagating matching files going forward, but must NOT retroactively
        remove copies already present on a peer. ``.hsyncignore`` is
        engine-owned and never itself synced, so the master applies the rule
        per-node and drops matching files from its listing after the hot-reload.
        """
        # Given one visible file and one later-excluded file on the master
        (Path(lan_master_dir) / "include_me.txt").write_bytes(b"visible\n")
        (Path(lan_master_dir) / "exclude_me.log").write_bytes(b"hidden\n")

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Start a slave so the master discovers it and runs the election.
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # First slave session: both files sync (no .hsyncignore yet).
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            assert _wait_for_file(str(Path(lan_slave_dir) / "include_me.txt"), timeout=30), (
                "include_me.txt did not sync"
            )
            assert _wait_for_file(str(Path(lan_slave_dir) / "exclude_me.log"), timeout=30), (
                "exclude_me.log did not sync"
            )
            _terminate(slave1)

            # When .hsyncignore is added excluding *.log, then fresh matching
            # and non-matching files are created
            (Path(lan_master_dir) / ".hsyncignore").write_text("*.log\n")
            (Path(lan_master_dir) / "new_exclude.log").write_bytes(b"nope\n")
            (Path(lan_master_dir) / "new_include.txt").write_bytes(b"yes\n")

            # Then, on reconnect, the new non-matching file syncs…
            slave2 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                assert _wait_for_file(str(Path(lan_slave_dir) / "new_include.txt"), timeout=30), (
                    "new_include.txt did not sync after .hsyncignore reload"
                )

                # …the newly excluded file is NOT propagated…
                assert not _wait_for_file(
                    str(Path(lan_slave_dir) / "new_exclude.log"), timeout=5
                ), "new_exclude.log was synced despite the .hsyncignore *.log rule"

                # …and existing copies are preserved, never retroactively
                # deleted (exclude protects, it does not delete).
                assert (Path(lan_slave_dir) / "exclude_me.log").exists(), (
                    "exclude_me.log was deleted; an ignore rule must not remove "
                    "already-synced copies on a peer"
                )
                assert (Path(lan_slave_dir) / "include_me.txt").exists(), (
                    "include_me.txt disappeared"
                )
            finally:
                _terminate(slave2)
        finally:
            _terminate(master)


# =============================================================================
# File permissions
# =============================================================================


class TestLanPermissions:
    def test_file_permissions_synced(self, lan_master_dir, lan_slave_dir, lan_bucket):
        """File with 600 perms on master → slave receives 600."""
        import stat

        # Given a serving master
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            # Start a slave so the master discovers it and runs the election.
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            # When a 600-permission file is created on the master
            f = Path(lan_master_dir) / "perms.txt"
            f.write_bytes(b"permissions-test\n")
            f.chmod(0o600)

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Then it syncs and its permissions are preserved (not world-readable)
                slave_f = Path(lan_slave_dir) / "perms.txt"
                assert _wait_for_file(str(slave_f), timeout=30), (
                    "File with custom permissions did not sync"
                )
                mode = slave_f.stat().st_mode
                assert not (mode & stat.S_IROTH), "File should not be world-readable"
            finally:
                _terminate(slave)
        finally:
            _terminate(master)


# =============================================================================
# Max inbound size guard
# =============================================================================


class TestLanMaxInboundSize:
    def test_oversized_upload_refused_small_file_syncs(
        self, lan_master_dir, lan_slave_dir, lan_bucket, bb_sample
    ):
        """Master with --max-inbound-size 1 refuses uploads over 1 MiB; small
        files still sync normally."""
        # Given a master with a 1 MiB inbound size cap
        master = _lan_start(
            lan_master_dir,
            lan_bucket,
            force_master=True,
            extra_args=("--max-inbound-size", "1"),
        )
        try:
            _wait_for_master(master, lan_slave_dir, lan_bucket)

            slave = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            try:
                # Wait for the slave's watcher to be live before creating files
                assert _wait_for_slave_events(slave), "slave did not reach [READY]"

                # When a small file is created on the slave
                s_small = bb_sample(prefix="small", size=0)
                small_content = b"small file\n"
                (Path(lan_slave_dir) / s_small.name).write_bytes(small_content)

                # Then it syncs to the master (under the cap)
                assert _wait_for_file(str(Path(lan_master_dir) / s_small.name), timeout=30), (
                    f"small file did not sync. {s_small.ctx}"
                )
                assert (Path(lan_master_dir) / s_small.name).read_bytes() == small_content

                # When an oversized file (> 1 MiB) is created on the slave
                s_big = bb_sample(suffix=".bin", prefix="big", size=2 * 1024 * 1024)
                (Path(lan_slave_dir) / s_big.name).write_bytes(s_big.payload)

                # Then it does NOT appear on the master (refused by the cap)
                time.sleep(5)
                assert not (Path(lan_master_dir) / s_big.name).exists(), (
                    f"oversized file was accepted despite --max-inbound-size 1. {s_big.ctx}"
                )
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
