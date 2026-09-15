# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Shared measurement core for the hsync-client L1 micro-benchmarks (stdlib only).

Methodology mirrors ``hsync-common/bench/common.py`` and
``hsync-bootnode/bench/common.py``: one :class:`BenchResult` per measured
operation, warmup executed outside the timed samples, per-call
``time.perf_counter`` latency, and an optional ``--json-out`` dump.

This module **never imports** ``hsync_client``: the benchmark harness must stay
usable even if the client internals are rewritten, and these helpers are not
part of the shipped package (consumers: the ``bench/bench_*.py`` scripts only).

Determinism: the CID helpers are derived from a counter (no ``random`` seed
juggling) so every benchmark run is reproducible. Timings are comparative (this
machine, warm OS page cache), never production SLAs.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence

__all__ = [
    "ALPHABET",
    "BenchResult",
    "add_json_out",
    "make_cid",
    "make_cids",
    "maybe_write_json",
    "measure",
    "measure_async",
    "percentile",
    "print_table",
    "scaled_warmup",
]

# CID alphabet (spec: ^[A-Za-z0-9]{21}$).
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def make_cid(n: int) -> str:
    """Deterministic 21-char CID for counter value ``n``."""
    base = len(ALPHABET)
    chars: list[str] = []
    value = n
    for _ in range(21):
        chars.append(ALPHABET[value % base])
        value //= base
    return "".join(reversed(chars))


def make_cids(count: int, start: int = 0) -> list[str]:
    """Return ``count`` deterministic CIDs starting at counter ``start``."""
    return [make_cid(start + i) for i in range(count)]


class BenchResult:
    """One measured operation: name, sample count and latency stats (ms)."""

    def __init__(self, name: str, samples: Sequence[float], count: int) -> None:
        lat_ms = [sample * 1000.0 for sample in samples]
        self.name = name
        self.count = count
        self.total_s = sum(samples)
        self.ops = count / self.total_s if self.total_s > 0 else 0.0
        self.p50 = statistics.median(lat_ms) if lat_ms else 0.0
        self.p99 = percentile(lat_ms, 99.0)
        self.mean = statistics.fmean(lat_ms) if lat_ms else 0.0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "count": self.count,
            "total_s": round(self.total_s, 4),
            "ops_per_s": round(self.ops, 1),
            "mean_ms": round(self.mean, 4),
            "p50_ms": round(self.p50, 4),
            "p99_ms": round(self.p99, 4),
        }


def percentile(data: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile: ``rank = ceil(pct/100 * n) - 1``."""
    if not data:
        return 0.0
    ordered = sorted(data)
    rank = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * pct / 100.0) - 1))
    return ordered[rank]


def scaled_warmup(count: int, cap: int = 200) -> int:
    """Warmup iterations for one scenario, capped and proportional to ``count``."""
    return max(1, min(cap, count // 10))


def measure(
    name: str,
    func: Callable[[], None],
    *,
    count: int,
    warmup: int | None = None,
) -> BenchResult:
    """Run ``func`` ``count`` times (after ``warmup``) and time each call."""
    if warmup is None:
        warmup = scaled_warmup(count)
    for _ in range(warmup):
        func()
    samples: list[float] = []
    for _ in range(count):
        start = time.perf_counter()
        func()
        samples.append(time.perf_counter() - start)
    return BenchResult(name, samples, count)


async def measure_async(
    name: str,
    func: Callable[[], Awaitable[None]],
    *,
    count: int,
    warmup: int | None = None,
) -> BenchResult:
    """Await ``func`` ``count`` times (after ``warmup``) on one running loop.

    Production drives the async helpers on a live event loop, so the loop is
    created once per scenario: its setup cost is never part of a measured sample.
    """
    if warmup is None:
        warmup = scaled_warmup(count)
    for _ in range(warmup):
        await func()
    samples: list[float] = []
    for _ in range(count):
        start = time.perf_counter()
        await func()
        samples.append(time.perf_counter() - start)
    return BenchResult(name, samples, count)


def print_table(results: Sequence[BenchResult]) -> None:
    """Print one aligned row per measured operation."""
    header = f"{'operation':<28}{'ops/s':>12}{'mean_ms':>10}{'p50_ms':>10}{'p99_ms':>10}{'n':>8}"
    print(header)
    print("-" * len(header))
    for result in results:
        print(
            f"{result.name:<28}{result.ops:>12.1f}{result.mean:>10.4f}"
            f"{result.p50:>10.4f}{result.p99:>10.4f}{result.count:>8}"
        )


def add_json_out(parser: argparse.ArgumentParser) -> None:
    """Register the shared ``--json-out`` option on ``parser``."""
    parser.add_argument(
        "--json-out",
        default=None,
        help="Write the results as JSON to the given path (besides stdout).",
    )


def maybe_write_json(path: str | None, results: Sequence[BenchResult]) -> None:
    """Dump ``results`` as JSON when ``path`` was provided."""
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump([result.as_dict() for result in results], handle, indent=2)
        handle.write("\n")
    print(f"wrote {path}")
