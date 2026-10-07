# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""L3 — p2p soak (MANUAL, not a test): sustained mutation waves between peers.

Starts a real master+slave pair over loopback mDNS (reusing the black-box
harness helpers), seeds a tree, then applies mutation waves for --duration
seconds. Every wave is measured with the quiet-poll convergence check and the
RSS of both peers is sampled from /proc.

Pass criteria: at least one wave, zero mismatches on every wave, and the RSS
growth of both peers under --max-rss-mb. Wave failures are recorded and stop
the loop (a diverged replica cannot be used to measure the rest).

Usage:
    cd hsync-client && .venv/bin/python bench/soak_p2p.py \
        [--duration 600] [--files 120] [--dirs 20] [--max-rss-mb 150]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import stress_tree  # noqa: E402
from p2p_helpers import (  # noqa: E402
    _p2p_start,
    _terminate,
    _wait_for_slave_events,
    _wait_for_text_in_stderr,
    assert_realtime_watcher,
)


def _proc_rss_mb(pid: int) -> float:
    """RSS of *pid* in MiB (0.0 when the process is gone).

    Kept local on purpose: bench scripts are standalone per repository (there
    is no cross-repo helper module, and hsync-common is off-limits for bench
    tooling), so this mirrors the bootnode/server soak helpers.
    """
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        return 0.0
    return 0.0


def run(args: argparse.Namespace) -> int:
    """Start the peers, seed, run waves until *duration*; return 0 on PASS."""
    with tempfile.TemporaryDirectory(prefix="hsync-soak-p2p-") as tmp:
        root = Path(tmp)
        master_dir = root / "master"
        slave_dir = root / "slave"
        master_dir.mkdir()
        slave_dir.mkdir()
        bucket = f"soak-lan-{uuid.uuid4().hex[:8]}"
        master = _p2p_start(str(master_dir), bucket, force_master=True)
        slave = None
        try:
            assert _wait_for_text_in_stderr(master, "Registered mDNS service", timeout=30), (
                "master mDNS service did not register"
            )
            slave = _p2p_start(str(slave_dir), bucket, never_master=True)
            assert _wait_for_slave_events(slave, timeout=120), "slave did not become ready"
            assert_realtime_watcher(master, "Master")
            assert_realtime_watcher(slave, "Slave")

            rss_m0 = _proc_rss_mb(master.pid)
            rss_s0 = _proc_rss_mb(slave.pid)
            manifest = stress_tree.generate(
                master_dir,
                seed=args.seed,
                n_files=args.files,
                n_dirs=args.dirs,
                max_depth=args.max_depth,
                max_file_size=args.max_file_size,
            )
            n_files, n_dirs, total_bytes = stress_tree.manifest_totals(manifest)
            print(
                f"seeded {n_files} files / {n_dirs} dirs / {total_bytes / 1048576:.1f} MiB; "
                f"initial rss master={rss_m0:.0f}MB slave={rss_s0:.0f}MB"
            )

            problems: list[str] = []
            t0 = time.perf_counter()
            mismatches, _stats, t_sync, t_verify = stress_tree.converge_quiet_poll(
                slave_dir,
                manifest,
                mode="p2p",
                timeout=args.timeout,
                poll_interval=args.poll_interval,
                mtime_tolerance=args.mtime_tolerance,
            )
            print(f"  initial burst: t_sync={t_sync:6.2f}s t_verify={t_verify:5.2f}s")
            if mismatches:
                problems.append(f"initial burst: {len(mismatches)} mismatch(es)")
                for mm in mismatches[:5]:
                    print(f"    {mm}")

            waves = 0
            while not problems and time.perf_counter() - t0 < args.duration:
                manifest, _primitives = stress_tree.apply_mutation(
                    master_dir,
                    manifest,
                    seed=args.seed,
                    wave=waves,
                    fraction=args.mutation_fraction,
                    max_file_size=args.max_file_size,
                )
                n_files, n_dirs, total_bytes = stress_tree.manifest_totals(manifest)
                mismatches, _stats, t_sync, t_verify = stress_tree.converge_quiet_poll(
                    slave_dir,
                    manifest,
                    mode="p2p",
                    timeout=args.timeout,
                    poll_interval=args.poll_interval,
                    mtime_tolerance=args.mtime_tolerance,
                )
                waves += 1
                print(
                    f"  wave {waves}: t_sync={t_sync:6.2f}s t_verify={t_verify:5.2f}s "
                    f"files={n_files} dirs={n_dirs} "
                    f"rss master={_proc_rss_mb(master.pid):.0f}MB "
                    f"slave={_proc_rss_mb(slave.pid):.0f}MB"
                )
                if mismatches:
                    problems.append(f"wave {waves}: {len(mismatches)} mismatch(es)")
                    for mm in mismatches[:5]:
                        print(f"    {mm}")

            rss_m1 = _proc_rss_mb(master.pid)
            rss_s1 = _proc_rss_mb(slave.pid)
            delta_m = rss_m1 - rss_m0
            delta_s = rss_s1 - rss_s0
            print(
                f"waves={waves} elapsed={time.perf_counter() - t0:.0f}s "
                f"rss master {rss_m0:.0f}->{rss_m1:.0f}MB (+{delta_m:.0f}) "
                f"slave {rss_s0:.0f}->{rss_s1:.0f}MB (+{delta_s:.0f})"
            )
            for problem in problems:
                print(f"  problem: {problem}")
            ok = (
                not problems
                and waves > 0
                and delta_m <= args.max_rss_mb
                and delta_s <= args.max_rss_mb
            )
            print("SOAK-" + ("PASS" if ok else "FAIL"))
            return 0 if ok else 1
        finally:
            if slave is not None:
                _terminate(slave)
            _terminate(master)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--files", type=int, default=120)
    parser.add_argument("--dirs", type=int, default=20)
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--max-file-size", type=int, default=1024 * 1024)
    parser.add_argument("--mutation-fraction", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=20260828)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--mtime-tolerance", type=float, default=2.0)
    parser.add_argument("--max-rss-mb", type=float, default=150.0)
    args = parser.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
