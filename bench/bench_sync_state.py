# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""L1 — client sync-state wrapper micro-benchmark (temp bucket + temp state dir).

Measures the client-side binding around the shared ``SyncStateStore``
(:mod:`hsync_client.sync_state`): every reconcile compares the local file
against the recorded merge base, so these wrappers run per path per cycle:

* ``get_sync_state_store`` — the cached store resolution (folder marker + lock);
* ``get_base_hash`` — the per-path merge-base lookup the uploader pays;
* ``upload_version_fields`` — the causal version advertisement sent with every
  upload, on the unchanged (no tick) and locally-edited (tick) branches;
* ``record_base`` — recording the agreed base after a successful transfer;
* ``merge_and_record`` — the convergence merge (D11) after an inbound sync;
* ``flush`` — the once-per-cycle SQLite persistence of the store.

Isolation mirrors the test suite: a ``TemporaryDirectory`` bucket plus a
``HSYNC_STATE_DIR`` override and a seeded ``CONFIG``, with the module store cache
cleared so the measured store is freshly resolved.

Usage:
    cd hsync-client && .venv/bin/python bench/bench_sync_state.py [--count N] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import json
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
from hsync_client.config import set_config  # noqa: E402
from hsync_client.state_backend import reset_bucket_folder_cache  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    cid = make_cid(1)
    with tempfile.TemporaryDirectory(prefix="hsync_syncstate_") as tmp:
        bucket = str(Path(tmp) / "bucket")
        state_dir = str(Path(tmp) / "state")
        os.makedirs(bucket, exist_ok=True)
        os.makedirs(state_dir, exist_ok=True)
        os.environ["HSYNC_STATE_DIR"] = state_dir
        set_config({"LOCAL_BUCKET": bucket, "CID": cid})
        sync_state._stores.clear()
        reset_bucket_folder_cache()

        abs_path = str(Path(bucket) / "dir" / "file.txt")
        Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
        Path(abs_path).write_bytes(b"x" * 64)

        base = "a" * 64
        base_other = "b" * 64
        # Seed the store so the hit/tick branches are exercised.
        sync_state.record_base(abs_path, base, vclock={cid: 1}, corrected_mtime=100)
        assert sync_state.get_base_hash(abs_path) == base

        store = sync_state.get_sync_state_store()
        same_vclock_json = json.dumps({cid: 1})
        ticked_vclock_json = json.dumps({cid: 2})

        def op_get_store() -> None:
            assert sync_state.get_sync_state_store() is store

        def op_get_base_hash() -> None:
            assert sync_state.get_base_hash(abs_path) == base

        def op_local_version_and_base_same() -> None:
            eff, stored = sync_state.local_version_and_base(abs_path, base)
            assert eff is not None and eff["vclock"] == {cid: 1}
            assert stored == base

        def op_local_version_and_base_ticked() -> None:
            eff, stored = sync_state.local_version_and_base(abs_path, base_other)
            assert eff is not None and eff["vclock"] == {cid: 2}
            assert stored == base

        def op_upload_fields_same() -> None:
            vclock_json, version_id, corrected = sync_state.upload_version_fields(abs_path, base)
            assert vclock_json == same_vclock_json
            assert isinstance(version_id, str) and len(version_id) == 32
            assert corrected == 100

        def op_upload_fields_ticked() -> None:
            vclock_json, _version_id, corrected = sync_state.upload_version_fields(
                abs_path, base_other
            )
            assert vclock_json == ticked_vclock_json
            assert corrected >= 0

        def op_record_base() -> None:
            sync_state.record_base(abs_path, base, vclock={cid: 1}, corrected_mtime=100)

        def op_merge_and_record() -> None:
            sync_state.merge_and_record(
                abs_path, base, remote_vclock={cid: 2}, remote_corrected_mtime=100
            )

        def op_flush() -> None:
            sync_state.flush()

        results: list[BenchResult] = [
            measure("get_sync_state_store(cached)", op_get_store, count=count),
            measure("get_base_hash(hit)", op_get_base_hash, count=count),
            measure("local_version_and_base(same)", op_local_version_and_base_same, count=count),
            measure(
                "local_version_and_base(ticked)", op_local_version_and_base_ticked, count=count
            ),
            measure("upload_version_fields(same)", op_upload_fields_same, count=count),
            measure("upload_version_fields(ticked)", op_upload_fields_ticked, count=count),
            measure("record_base", op_record_base, count=count),
            measure("merge_and_record", op_merge_and_record, count=count),
            measure("flush", op_flush, count=50),
        ]

        print(f"sync-state wrapper micro-benchmark (ops={count}, temp bucket + state dir)")
        print_table(results)
        maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
