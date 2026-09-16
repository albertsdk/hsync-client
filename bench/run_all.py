# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Run every L1 benchmark and write one versioned results file.

Mirrors ``hsync-common/bench/run_all.py`` and ``hsync-bootnode``'s committed
baseline: a single committed file records the baseline of one machine so two
revisions can be compared (``git diff bench_result.txt``). The file is a
comparison artefact, not an SLA: each section is the raw stdout of one
``bench_*.py`` script plus its exit code, so a failed scenario is visible
instead of being silently missing.

Each script still runs standalone (``--count``/``--json-out``); this runner only
chains them in a fixed order with a shared sample count.

Usage:
    cd hsync-client && .venv/bin/python bench/run_all.py [--count N] [--out FILE] [--json-out-dir DIR]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

_BENCH_DIR = Path(__file__).resolve().parent
_SCRIPTS = (
    "bench_autotune",
    "bench_utils",
    "bench_event_queue",
    "bench_pinning",
    "bench_sync_state",
    "bench_tombstones",
    "bench_arbitration",
    "bench_reconcile",
)
_DEFAULT_OUT = _BENCH_DIR.parent / "bench_result.txt"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument(
        "--out",
        default=str(_DEFAULT_OUT),
        help="Results file to write (default: <repo root>/bench_result.txt).",
    )
    parser.add_argument(
        "--json-out-dir",
        default=None,
        help="Also write one <script>.json per scenario into this directory, for "
        "compare.py A/B runs.",
    )
    args = parser.parse_args()

    sections: list[str] = [
        f"hsync-client L1 benchmark suite — count={args.count}",
        f"python {sys.version.split()[0]} — {sys.platform}",
        f"generated {time.strftime('%Y-%m-%d %H:%M:%S')} by bench/run_all.py",
        "comparative figures (this machine, warm page cache), not SLAs",
        "",
    ]
    failed: list[str] = []
    json_out_dir: Path | None = Path(args.json_out_dir) if args.json_out_dir else None
    if json_out_dir is not None:
        json_out_dir.mkdir(parents=True, exist_ok=True)
    for script in _SCRIPTS:
        start = time.perf_counter()
        cmd = [sys.executable, str(_BENCH_DIR / f"{script}.py"), "--count", str(args.count)]
        if json_out_dir is not None:
            cmd += ["--json-out", str(json_out_dir / f"{script}.json")]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        elapsed = time.perf_counter() - start
        sections.append(f"=== {script} (exit={proc.returncode}, {elapsed:.1f}s) ===")
        sections.append(proc.stdout.rstrip("\n") or "(no stdout)")
        if proc.returncode != 0:
            sections.append("--- stderr ---")
            sections.append(proc.stderr.rstrip("\n") or "(no stderr)")
            failed.append(script)
        sections.append("")
        print(f"{script}: exit={proc.returncode} ({elapsed:.1f}s)")

    Path(args.out).write_text("\n".join(sections) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    if failed:
        print(f"FAILED scenarios: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
