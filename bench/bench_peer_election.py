# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""L1 — peer-liveness scan and master election micro-benchmark.

Measures the two hot ``PeerManager`` paths touched by the performance plan:

* ``add_peer`` heartbeat refresh on an established peer set (the mDNS cadence
  path): the liveness scan runs over every peer on each update;
* ``_elect_master``: the candidate scan + rank over every alive peer.

Plain script, never collected by pytest. Usage:

    cd hsync-client && .venv/bin/python bench/bench_peer_election.py \
        [--count N] [--sizes 50,200,500] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    make_cid,
    maybe_write_json,
    measure,
    print_table,
)

from hsync_client.p2p_peer_models import Peer  # noqa: E402
from hsync_client.p2p_peers import PeerManager  # noqa: E402

_BUCKET = "bench"


def _make_peers(size: int, start: int) -> list[Peer]:
    return [
        Peer(
            cid=make_cid(start + i),
            bucket=_BUCKET,
            address=f"10.0.0.{(i % 250) + 1}",
            port=9000 + i,
            version="1.0.0",
            group=None,
            startup_time=1_700_000_000.0 + i,
        )
        for i in range(size)
    ]


def _measure_size(size: int, count: int) -> list[BenchResult]:
    manager = PeerManager(
        local_cid=make_cid(0),
        bucket=_BUCKET,
        startup_time=1_700_000_000.0,
        peer_timeout=3600.0,
    )
    peers = _make_peers(size, start=1)
    for peer in peers:
        manager.add_peer(peer)

    cursor = [0]

    def op_add_peer() -> None:
        peer = peers[cursor[0] % size]
        cursor[0] += 1
        manager.add_peer(peer)

    def op_elect() -> None:
        manager._elect_master()

    return [
        measure(f"add_peer(refresh, {size} peers)", op_add_peer, count=count),
        measure(f"elect_master({size} peers)", op_elect, count=count),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--sizes", default="50,200,500")
    add_json_out(parser)
    args = parser.parse_args()

    # add_peer/election log through loguru on the update path; silence the sink so
    # thousands of DEBUG lines do not dominate the run.
    from loguru import logger

    logger.remove()

    sizes = [int(part) for part in args.sizes.split(",") if part.strip()]
    results: list[BenchResult] = []
    for size in sizes:
        results.extend(_measure_size(size, args.count))

    print(f"peer election micro-benchmark (ops={args.count}, sizes={sizes})")
    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
