# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""L1 — reconcile-core micro-benchmark (decision leaves + full diff).

Measures the sync engine's reconcile path in :mod:`hsync_client.sync`, split in
two layers:

* **decision leaves (pure, no I/O)** — ``_compare_files`` (the per-file causal
  matrix, one call per file present on both sides), ``_tombstone_clock_dominates``
  / ``_tombstone_deletes_local`` (ADR-0003 deletion proof) and
  ``_parse_advertised_version`` (wire ``version`` decode).
* **bulk diff (integration)** — ``get_sync_actions(..., persist_state=False)``
  over a synthetic steady-state listing (``in_sync`` on every file): the honest
  per-file reconcile cost excluding content hashing (the SHA-256 LRU cache stays
  warm across samples), i.e. relpath validation + ``File.from_dict`` +
  ``File.get`` stat + sync-state lookups + ``_compare_files``.

Measured ops assert the exact branch/outcome so a silently wrong decision cannot
be reported as fast.

Usage:
    cd hsync-client && .venv/bin/python bench/bench_reconcile.py [--count N] [--json-out FILE]
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
from hsync_common.models.file import File  # noqa: E402
from hsync_common.models.sync_action import SyncActionName  # noqa: E402

from hsync_client import sync_state  # noqa: E402
from hsync_client.config import set_config  # noqa: E402
from hsync_client.state_backend import reset_bucket_folder_cache  # noqa: E402
from hsync_client.sync import (  # noqa: E402
    _compare_files,
    _parse_advertised_version,
    _tombstone_clock_dominates,
    _tombstone_deletes_local,
    get_sync_actions,
)

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64


def _file(sha: str) -> File:
    return File(
        filepath="/srv/hsync/bucket/doc.txt",
        mtime=1_700_000_000,
        size=64,
        sha256sum=sha,
        permissions="644",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    # The conflict_download branch logs eagerly via loguru (the eager f-string
    # cost stays in the measurement on purpose); silence the sink so the bench
    # output is not flooded by thousands of DEBUG lines.
    from loguru import logger

    logger.remove()

    cid_a = make_cid(1)
    cid_b = make_cid(2)

    local_a, local_b = _file(_A), _file(_B)
    remote_a, remote_b, remote_c = _file(_A), _file(_B), _file(_C)

    def op_in_sync() -> None:
        assert _compare_files(remote_a, local_a, None, None) == (SyncActionName.nothing, 0)

    def op_download_base() -> None:
        assert _compare_files(remote_b, local_a, None, None, base_hash=_A) == (
            SyncActionName.download,
            1,
        )

    def op_upload_base() -> None:
        assert _compare_files(remote_a, local_b, None, None, base_hash=_A) == (
            SyncActionName.upload,
            1,
        )

    def op_conflict_upload() -> None:
        assert _compare_files(
            remote_c,
            local_b,
            {cid_a: 1},
            {cid_b: 1},
            base_hash=_A,
            local_corrected_mtime=200,
            remote_corrected_mtime=100,
        ) == (SyncActionName.upload, 1)

    def op_conflict_download() -> None:
        assert _compare_files(
            remote_c,
            local_b,
            {cid_a: 1},
            {cid_b: 1},
            base_hash=_A,
            local_corrected_mtime=100,
            remote_corrected_mtime=200,
        ) == (SyncActionName.download, 0)

    tomb_dom = {"vclock": {cid_a: 3}}
    tomb_sub = {"vclock": {cid_a: 1}}
    tomb_bad = {"vclock": "garbage"}
    tomb_empty: dict = {}

    def op_tomb_dominates() -> None:
        assert _tombstone_clock_dominates(tomb_dom, {cid_a: 1}) is True

    def op_tomb_not_dominates() -> None:
        assert _tombstone_clock_dominates(tomb_sub, {cid_a: 3}) is False

    def op_tomb_invalid() -> None:
        assert _tombstone_clock_dominates(tomb_bad, {cid_a: 1}) is False
        assert _tombstone_clock_dominates(tomb_empty, None) is False

    def op_tomb_delete() -> None:
        assert _tombstone_deletes_local(tomb_dom, {cid_a: 1}) is True

    def op_tomb_keep() -> None:
        assert _tombstone_deletes_local(tomb_sub, {cid_a: 3}) is False

    adv_valid = {
        "vclock": {cid_a: 1},
        "version_id": "deadbeef",
        "origin_cid": cid_a,
        "ts": 1_700_000_000,
        "corrected_mtime": 1_700_000_000,
    }

    def op_parse_valid() -> None:
        vclock, corrected = _parse_advertised_version(adv_valid)
        assert vclock == {cid_a: 1} and corrected == 1_700_000_000

    def op_parse_absent() -> None:
        assert _parse_advertised_version(None) == (None, 0)

    def op_parse_malformed() -> None:
        assert _parse_advertised_version("junk") == (None, 0)

    results: list[BenchResult] = [
        measure("compare_files(in_sync)", op_in_sync, count=count),
        measure("compare_files(download)", op_download_base, count=count),
        measure("compare_files(upload)", op_upload_base, count=count),
        measure("compare_files(conflict_upload)", op_conflict_upload, count=count),
        measure("compare_files(conflict_download)", op_conflict_download, count=count),
        measure("tombstone_clock_dominates(dom)", op_tomb_dominates, count=count),
        measure("tombstone_clock_dominates(sub)", op_tomb_not_dominates, count=count),
        measure("tombstone_clock_dominates(invalid)", op_tomb_invalid, count=count),
        measure("tombstone_deletes_local(delete)", op_tomb_delete, count=count),
        measure("tombstone_deletes_local(keep)", op_tomb_keep, count=count),
        measure("parse_advertised_version(valid)", op_parse_valid, count=count),
        measure("parse_advertised_version(absent)", op_parse_absent, count=count),
        measure("parse_advertised_version(malformed)", op_parse_malformed, count=count),
    ]

    # ---- bulk diff: steady-state reconcile over an in-sync listing ----
    with tempfile.TemporaryDirectory(prefix="hsync_reconcile_") as tmp:
        bucket = str(Path(tmp) / "bucket")
        state_dir = str(Path(tmp) / "state")
        os.makedirs(bucket, exist_ok=True)
        os.makedirs(state_dir, exist_ok=True)
        os.environ["HSYNC_STATE_DIR"] = state_dir
        set_config({"LOCAL_BUCKET": bucket, "CID": cid_a, "SYNC_DIRECTION": "bidirectional"})
        sync_state._stores.clear()
        reset_bucket_folder_cache()

        n_files = 100
        for i in range(n_files):
            Path(bucket, f"f-{i:04d}.txt").write_bytes(b"x" * 64)

        # Populate the SHA-256 LRU cache and seed the merge base for each file.
        local_dicts = []
        for path in sorted(Path(bucket).glob("f-*.txt")):
            local = File.get(str(path))
            assert local is not None and local.sha256sum
            local_dicts.append(local.to_dict())
            sync_state.record_base(str(path), local.sha256sum, vclock={cid_a: 1}, corrected_mtime=1)

        remote_dicts = []
        for local in local_dicts:
            rel = str(Path(local["filepath"]).relative_to(bucket))
            remote_dicts.append(
                {
                    "filepath": rel,
                    "name": local["name"],
                    "mtime": local["mtime"],
                    "ctime": local["ctime"],
                    "size": local["size"],
                    "sha256sum": local["sha256sum"],
                    "permissions": local["permissions"],
                    "version": {
                        "vclock": {cid_a: 1},
                        "version_id": "v",
                        "origin_cid": cid_a,
                        "ts": 0,
                        "corrected_mtime": 1,
                    },
                }
            )

        def op_bulk() -> None:
            actions = get_sync_actions(remote_dicts, local_dicts, persist_state=False)
            assert len(actions) == n_files
            assert all(a.name is SyncActionName.nothing for a in actions)

        results.append(measure("get_sync_actions(100 in_sync)", op_bulk, count=20))

        print(f"reconcile micro-benchmark (ops={count}, decision leaves + bulk diff)")
        print_table(results)
        maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
