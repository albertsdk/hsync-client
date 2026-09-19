# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Stress tests — LAN P2P load/replication harness (black-box).

Starts a real master + slave pair over loopback mDNS (via the shared
``lan_helpers``), generates the tree **after** the slave is ``[READY]`` (plan
§13.3 — event-driven dir-mode parity), measures convergence with the quiet
poll (cheap signal, single full verify — plan §7), verifies byte-for-byte,
then applies mutation wave(s) and re-verifies. Both peers are always
terminated in ``finally``.

Design: ``docs/internal/stress-test-plan.md``. Requires ``HSYNC_BB_STRESS=1`` to
run; ``make ci`` sets it at a bounded scale, ``make test-stress`` runs it full
scale.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

from . import stress_tree
from .lan_helpers import (
    _lan_start,
    _terminate,
    _wait_for_slave_events,
    _wait_for_text_in_stderr,
    assert_realtime_watcher,
    watcher_degraded_lines,
)

_ENABLED = os.environ.get("HSYNC_BB_STRESS") == "1"
_REASON = "Stress tests are opt-in; set HSYNC_BB_STRESS=1 to enable"

pytestmark = pytest.mark.skipif(not _ENABLED, reason=_REASON)

_K = stress_tree.stress_knobs("lan")

# Read at import time: the autouse env-isolation fixture scrubs HSYNC_* before
# each test body, so an os.environ lookup inside the test would always be empty.
_NO_PEER_DOWNLOAD = os.environ.get("HSYNC_BB_STRESS_NO_PEER_DOWNLOAD") == "1"


def _report(record: dict[str, object]) -> None:
    stress_tree.collect(record)
    stress_tree.print_report(record)
    stress_tree.write_report(_K["report"], record)  # type: ignore[arg-type]


def _fail_if_mismatched(
    mismatches: list[stress_tree.Mismatch],
    master: subprocess.Popen,
    slave: subprocess.Popen,
) -> None:
    """Fail on verification mismatches, appending watcher diagnostics.

    A convergence failure is only actionable once the environment precondition
    is visible in the report: if either peer degraded to the polling fallback
    *during* the run (see :func:`watcher_degraded_lines`) that is what explains
    a burst that was never broadcast by events, so it is appended to the
    mismatch dump instead of leaving an opaque report.
    """
    if not mismatches:
        return
    try:
        stress_tree.fail_with(mismatches)
    except AssertionError as exc:
        degraded = [
            *(f"master: {line}" for line in watcher_degraded_lines(master)),
            *(f"slave: {line}" for line in watcher_degraded_lines(slave)),
        ]
        if not degraded:
            raise
        raise AssertionError(
            f"{exc}\nWatcher degradation observed during the run:\n  " + "\n  ".join(degraded[:5])
        ) from exc


class TestLanStress:
    """Master→slave burst → converge → verify → mutate → re-verify."""

    @pytest.mark.parametrize("iteration", range(_K["iterations"]))
    def test_burst_verify_mutate_reverify(
        self,
        lan_master_dir,
        lan_slave_dir,
        lan_bucket,
        iteration: int,
    ) -> None:
        seed = int(_K["seed"]) + iteration  # derived seed, plan §7
        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            assert _wait_for_text_in_stderr(master, "Registered mDNS service", timeout=15), (
                "Master mDNS service did not register"
            )
            slave = _lan_start(
                lan_slave_dir,
                lan_bucket,
                never_master=True,
                extra_args=("--no-peer-download",) if _NO_PEER_DOWNLOAD else (),
            )
            try:
                assert _wait_for_slave_events(slave, timeout=90), (
                    "Slave did not become ready (watcher live + handshake done)"
                )
                # Environment precondition: event-driven replication needs a
                # live inotify watcher on both peers. Fail fast with the real
                # cause instead of an opaque convergence timeout (see
                # lan_helpers.assert_realtime_watcher).
                assert_realtime_watcher(master, "Master")
                assert_realtime_watcher(slave, "Slave")

                # Generate AFTER [READY]: dir-mode parity is event-driven in
                # LAN mode and the post-initial-sync blind spot would lose a
                # burst written earlier (plan §13.3).
                t0 = time.perf_counter()
                manifest = stress_tree.generate(
                    Path(lan_master_dir),
                    seed=seed,
                    n_files=int(_K["n_files"]),
                    n_dirs=int(_K["n_dirs"]),
                    max_depth=int(_K["max_depth"]),
                    max_file_size=int(_K["max_file_size"]),
                )
                t_generate = time.perf_counter() - t0
                n_files, n_dirs, total_bytes = stress_tree.manifest_totals(manifest)

                mismatches, _stats, t_sync, t_verify = stress_tree.converge_quiet_poll(
                    Path(lan_slave_dir),
                    manifest,
                    mode="lan",
                    timeout=float(_K["timeout"]),
                    poll_interval=float(_K["poll_interval"]),
                    mtime_tolerance=float(_K["mtime_tolerance"]),
                )
                _report(
                    stress_tree.make_record(
                        mode="lan",
                        seed=seed,
                        n_files=n_files,
                        n_dirs=n_dirs,
                        total_bytes=total_bytes,
                        t_generate_s=t_generate,
                        t_sync_s=t_sync,
                        t_verify_s=t_verify,
                        max_concurrent_transfers=0,
                        iteration=iteration,
                        phase="initial_burst",
                        mismatches=mismatches,
                    )
                )
                _fail_if_mismatched(mismatches, master, slave)

                for wave in range(int(_K["waves"])):
                    manifest, _primitives = stress_tree.apply_mutation(
                        Path(lan_master_dir),
                        manifest,
                        seed=seed,
                        wave=wave,
                        fraction=float(_K["mutation_fraction"]),
                        max_file_size=int(_K["max_file_size"]),
                    )
                    n_files, n_dirs, total_bytes = stress_tree.manifest_totals(manifest)
                    mismatches, _stats, t_sync, t_verify = stress_tree.converge_quiet_poll(
                        Path(lan_slave_dir),
                        manifest,
                        mode="lan",
                        timeout=float(_K["timeout"]),
                        poll_interval=float(_K["poll_interval"]),
                        mtime_tolerance=float(_K["mtime_tolerance"]),
                    )
                    _report(
                        stress_tree.make_record(
                            mode="lan",
                            seed=seed,
                            n_files=n_files,
                            n_dirs=n_dirs,
                            total_bytes=total_bytes,
                            t_generate_s=0.0,
                            t_sync_s=t_sync,
                            t_verify_s=t_verify,
                            max_concurrent_transfers=0,
                            iteration=iteration,
                            phase=f"mutation_wave{wave}",
                            mismatches=mismatches,
                        )
                    )
                    _fail_if_mismatched(mismatches, master, slave)
            finally:
                _terminate(slave)
        finally:
            _terminate(master)
