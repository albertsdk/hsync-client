# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""L1 — client tombstone wrapper micro-benchmark (temp bucket + temp state dir).

Measures the client-side binding around the shared ``TombstoneStore``
(:mod:`hsync_client.tombstones`), the deletion-propagation path of LAN mode:

* ``get_tombstone_store`` — the cached store resolution;
* ``get_payload`` — the JSON-serializable copy served/gossiped to the master;
* ``record_deletion`` — recording a deletion marker and **persisting** it (the
  realtime path flushes per deletion, since deletions are rare);
* ``merge_payload`` — the causal merge of an untrusted master payload, also
  persisted when it changes;
* ``mint_50(memory)`` / ``mint_50_then_flush`` — the offline-deletion mint loop:
  50 markers recorded in-memory (``persist=False``) alone, versus followed by a
  single ``flush()`` (the batched end-to-end cost the sync engine now pays).

Isolation mirrors the test suite: a ``TemporaryDirectory`` bucket, a
``HSYNC_STATE_DIR`` override and a seeded ``CONFIG``, with the module store
cache cleared. The merge op drives a strictly-dominant incoming clock so it
adopts every sample (constant measured cost), on a key separate from the
deletion-record path so the two scenarios never interfere.

Usage:
    cd hsync-client && .venv/bin/python bench/bench_tombstones.py [--count N] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# Allow ``from common import ...`` when run from the bench/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    make_cid,
    maybe_write_json,
    measure,
    print_table,
)

from hsync_client import tombstones  # noqa: E402
from hsync_client.config import set_config  # noqa: E402
from hsync_client.state_backend import reset_bucket_folder_cache  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    cid_a = make_cid(1)
    cid_b = make_cid(2)
    with tempfile.TemporaryDirectory(prefix="hsync_tomb_") as tmp:
        bucket = str(Path(tmp) / "bucket")
        state_dir = str(Path(tmp) / "state")
        os.makedirs(bucket, exist_ok=True)
        os.makedirs(state_dir, exist_ok=True)
        os.environ["HSYNC_STATE_DIR"] = state_dir
        set_config({"LOCAL_BUCKET": bucket, "CID": cid_a})
        tombstones._stores.clear()
        reset_bucket_folder_cache()

        abs_path = str(Path(bucket) / "dir" / "file.txt")
        Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
        ghost_path = str(Path(bucket) / "dir" / "ghost.txt")

        # Seed the merge target once (separate path from the record op).
        tombstones.record_deletion(ghost_path, origin_cid=cid_a)
        store = tombstones.get_tombstone_store()
        ghost_key = next(iter(store.to_payload()))

        merge_state = {"tick": 4}

        def op_get_store() -> None:
            assert tombstones.get_tombstone_store() is store

        def op_get_payload() -> None:
            assert isinstance(tombstones.get_payload(), dict)

        def op_record_deletion() -> None:
            tombstones.record_deletion(abs_path, sha256sum="a" * 64, origin_cid=cid_a)

        def op_merge_payload() -> None:
            merge_state["tick"] += 1
            tick = merge_state["tick"]
            payload = {
                ghost_key: {
                    "sha256": "b" * 64,
                    "version_id": "t2",
                    "origin_cid": cid_b,
                    "ts": 1_000 + tick,
                    "vclock": {cid_a: 1, cid_b: tick},
                }
            }
            assert tombstones.merge_payload(payload) == 1

        mint_keys = [f"mint-{i:03d}" for i in range(50)]

        def op_mint_memory() -> None:
            for key in mint_keys:
                tombstones.record_deletion_by_key(key, origin_cid=cid_a, persist=False)

        def op_mint_then_flush() -> None:
            for key in mint_keys:
                tombstones.record_deletion_by_key(key, origin_cid=cid_a, persist=False)
            tombstones.flush()

        results: list[BenchResult] = [
            measure("get_tombstone_store(cached)", op_get_store, count=count),
            measure("get_payload", op_get_payload, count=count),
            measure("record_deletion(persist)", op_record_deletion, count=200),
            measure("merge_payload(dominant)", op_merge_payload, count=200),
            measure("mint_50(memory)", op_mint_memory, count=50),
            measure("mint_50_then_flush", op_mint_then_flush, count=50),
        ]

        print(f"tombstone wrapper micro-benchmark (ops={count}, temp bucket + state dir)")
        print_table(results)
        maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
