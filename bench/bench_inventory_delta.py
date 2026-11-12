# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""L1 — P2P inventory delta-apply micro-benchmark.

Measures the store write path touched by the performance plan: one committed
snapshot, then repeated ``apply_frame`` delta frames of ``P2P_INVENTORY_MAX_ENTRIES``
upserts each (the maximum the protocol allows), plus the per-frame
``_counts_locked`` cost. The optimized version batches the per-entry writes with
``executemany`` when the upsert/delete path sets are disjoint (the delta case
here) and answers all quota counts in one statement.

Plain script, never collected by pytest. Usage:

    cd hsync-client && .venv/bin/python bench/bench_inventory_delta.py \
        [--count N] [--entries 500] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    make_cid,
    maybe_write_json,
    measure,
    print_table,
    scaled_warmup,
)

from hsync_client.p2p_inventory import P2PInventoryStore  # noqa: E402

_BUCKET = "bench"
_GEN = 1
_MAX_ENTRIES = 500  # P2P_INVENTORY_MAX_ENTRIES


def _upsert(path: str, seed: str) -> dict:
    return {
        "path": path,
        "sha256": hashlib.sha256(seed.encode()).hexdigest(),
        "size": 64,
    }


def _snapshot(entries: int) -> dict:
    return {
        "type": "inventory",
        "gen": _GEN,
        "seq": 0,
        "mode": "snapshot",
        "more": False,
        "entries": [_upsert(f"s-{i:05d}.bin", f"s-{i}") for i in range(entries)],
    }


def _delta(seq: int, entries: int) -> dict:
    return {
        "type": "inventory",
        "gen": _GEN,
        "seq": seq,
        "mode": "delta",
        "more": False,
        "entries": [_upsert(f"f{seq:05d}-{i:05d}.bin", f"{seq}-{i}") for i in range(entries)],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--entries", type=int, default=_MAX_ENTRIES)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count
    entries = min(args.entries, _MAX_ENTRIES)

    from loguru import logger

    logger.remove()

    with tempfile.TemporaryDirectory(prefix="hsync_inventory_") as tmp:
        store = P2PInventoryStore(
            str(Path(tmp) / "inventory.db"),
            _BUCKET,
            clock=time.monotonic,
            per_cid_quota=1_000_000,
            global_quota=2_000_000,
        )
        try:
            cid = make_cid(1)
            token = store.begin_session(cid, serve_slots=0)
            ack = store.apply_frame(cid, token, _snapshot(entries))
            assert ack["status"] == "ok", ack

            total = count + scaled_warmup(count)
            frames = [_delta(seq, entries) for seq in range(1, total + 1)]
            cursor = [0]

            def op_apply() -> None:
                frame = frames[cursor[0]]
                cursor[0] += 1
                result = store.apply_frame(cid, token, frame)
                assert result["status"] == "ok", result

            def op_counts() -> None:
                committed, staged, global_count = store._counts_locked(cid, token)
                assert committed >= entries and staged == 0 and global_count >= entries

            results: list[BenchResult] = [
                measure(f"apply_frame(delta {entries} upserts)", op_apply, count=count),
                measure("_counts_locked", op_counts, count=count),
            ]
        finally:
            store.close()

    print(f"inventory delta micro-benchmark (ops={count}, entries={entries})")
    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
