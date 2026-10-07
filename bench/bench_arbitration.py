# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""L1 — P2P upload-arbitration micro-benchmark (temp bucket + temp state dir).

Measures :func:`hsync_client.arbitration.arbitrate_upload` — the causal gate the
P2P master runs on every slave upload before touching the filesystem (ADR-0022).
The pure decision (``resolve_three_way``) lives in ``hsync-common``; this
benchmark measures the **client-side binding** around it: the in-bucket relpath
computation, the sync-state store read, the tolerant wire-clock parse and the
``ArbitrationResult`` assembly.

Four branches are covered, one per seeded path: the untouched first upload
(``accepted``), a causally-dominant edit (``accepted``), a stale re-push
(``superseded``) and a true divergence won on the corrected-mtime tie-break
(``accepted_conflict_won``). ``conflicted_copy_name`` (the ADR-0021 loser-path
builder) is measured separately.

Usage:
    cd hsync-client && .venv/bin/python bench/bench_arbitration.py [--count N] [--json-out FILE]
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

from hsync_client import sync_state  # noqa: E402
from hsync_client.arbitration import (  # noqa: E402
    ArbitrationOutcome,
    arbitrate_upload,
    conflicted_copy_name,
)
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
    h_first, h_old, h_new, h_store, h_in = ("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64)

    with tempfile.TemporaryDirectory(prefix="hsync_arb_") as tmp:
        bucket = str(Path(tmp) / "bucket")
        state_dir = str(Path(tmp) / "state")
        os.makedirs(bucket, exist_ok=True)
        os.makedirs(state_dir, exist_ok=True)
        os.environ["HSYNC_STATE_DIR"] = state_dir
        set_config({"LOCAL_BUCKET": bucket, "CID": cid_a})
        sync_state._stores.clear()
        reset_bucket_folder_cache()

        store = sync_state.get_sync_state_store()
        # Seed three branches; "first.txt" is intentionally left unseeded.
        store.record(
            "dominant.txt", base_hash=h_old, vclock={cid_a: 2}, origin_cid=cid_a, corrected_mtime=10
        )
        store.record(
            "superseded.txt",
            base_hash=h_new,
            vclock={cid_a: 3},
            origin_cid=cid_a,
            corrected_mtime=10,
        )
        store.record(
            "conflict.txt",
            base_hash=h_store,
            vclock={cid_a: 2},
            origin_cid=cid_a,
            corrected_mtime=10,
        )

        abs_first = str(Path(bucket) / "first.txt")
        abs_dom = str(Path(bucket) / "dominant.txt")
        abs_sup = str(Path(bucket) / "superseded.txt")
        abs_conf = str(Path(bucket) / "conflict.txt")

        def op_first() -> None:
            out = arbitrate_upload(
                abs_first,
                h_first,
                {cid_a: 1},
                "v1",
                cid_a,
                disk_hash=None,
            )
            assert out.outcome is ArbitrationOutcome.accepted

        def op_dominant() -> None:
            out = arbitrate_upload(
                abs_dom,
                h_new,
                {cid_a: 3},
                "v3",
                cid_a,
                disk_hash=h_old,
                incoming_corrected_mtime=20,
            )
            assert out.outcome is ArbitrationOutcome.accepted

        def op_superseded() -> None:
            out = arbitrate_upload(
                abs_sup,
                h_old,
                {cid_a: 1},
                "v1",
                cid_a,
                disk_hash=h_new,
                incoming_corrected_mtime=5,
            )
            assert out.outcome is ArbitrationOutcome.superseded

        def op_conflict() -> None:
            out = arbitrate_upload(
                abs_conf,
                h_in,
                {cid_b: 2},
                "v2",
                cid_b,
                disk_hash=h_store,
                incoming_corrected_mtime=20,
            )
            assert out.outcome is ArbitrationOutcome.accepted_conflict_won

        def op_conflicted_name() -> None:
            name = conflicted_copy_name("/srv/hsync/bucket/photo.jpg", "cid123", "20250101")
            assert name.endswith("photo_conflicted_copy_20250101_cid123.jpg")

        results: list[BenchResult] = [
            measure("arbitrate_upload(first)", op_first, count=count),
            measure("arbitrate_upload(dominant)", op_dominant, count=count),
            measure("arbitrate_upload(superseded)", op_superseded, count=count),
            measure("arbitrate_upload(conflict)", op_conflict, count=count),
            measure("conflicted_copy_name", op_conflicted_name, count=count),
        ]

        print(f"arbitration micro-benchmark (ops={count}, temp bucket + state dir)")
        print_table(results)
        maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
