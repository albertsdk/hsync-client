# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""L1 — hot sync/path helpers targeted by the performance plan.

Measures the functions changed by the optimization work so a before/after run
can quantify each gain:

* ``split_rename_pairs`` over a worst-case duplicate-content set (every new/old
  entry shares one ``(sha256, size)`` key), where the old ``list.pop(0)`` was
  quadratic;
* ``get_files_local`` over a synthetic bucket with regular files and symlinks
  (the walk already classifies symlinks, so the optimized scan must not re-stat
  them);
* ``path_present`` on a present file, a dangling symlink and a missing path;
* ``os.stat`` vs ``Path.stat`` for the directory-modified hot branch.

Plain script, never collected by pytest. Usage:

    cd hsync-client && .venv/bin/python bench/bench_sync_scan.py \
        [--count N] [--scan-count N] [--json-out FILE]
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

from hsync_client import sync_state  # noqa: E402
from hsync_client.config import set_config  # noqa: E402
from hsync_client.state_backend import reset_bucket_folder_cache  # noqa: E402
from hsync_client.sync import get_files_local, split_rename_pairs  # noqa: E402
from hsync_client.utils import path_present  # noqa: E402

_DUP_SHA = "a" * 64


def _dup_files(count: int, prefix: str) -> list[File]:
    return [
        File(
            filepath=f"/srv/hsync/bucket/{prefix}-{i:05d}.bin",
            mtime=1_700_000_000,
            ctime=1_700_000_000,
            size=64,
            sha256sum=_DUP_SHA,
            permissions="644",
        )
        for i in range(count)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument(
        "--scan-count",
        type=int,
        default=20,
        help="timed samples for get_files_local (walks a real temp tree)",
    )
    parser.add_argument("--pairs", type=int, default=5000, help="duplicate files per side")
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    results: list[BenchResult] = []

    # ---- split_rename_pairs: worst case, one duplicate-content key ----
    new_entries = _dup_files(args.pairs, "new")
    old_entries = _dup_files(args.pairs, "old")

    def op_split() -> None:
        pairs, remaining_new, remaining_old = split_rename_pairs(new_entries, old_entries)
        assert len(pairs) == args.pairs
        assert not remaining_new and not remaining_old

    results.append(measure(f"split_rename_pairs({args.pairs}x2 dup)", op_split, count=5))

    # ---- get_files_local: regular files + symlinks ----
    from loguru import logger

    logger.remove()

    with tempfile.TemporaryDirectory(prefix="hsync_scan_") as tmp:
        bucket = str(Path(tmp) / "bucket")
        os.makedirs(bucket, exist_ok=True)
        n_files = 500
        n_links = 200
        for i in range(n_files):
            Path(bucket, f"f-{i:04d}.bin").write_bytes(b"x")
        target = "f-0000.bin"
        for i in range(n_links):
            os.symlink(target, os.path.join(bucket, f"link-{i:04d}"))

        set_config(
            {
                "LOCAL_BUCKET": bucket,
                "CID": make_cid(1),
                "FILTER_PATH": "",
                "SYNC_DIRECTION": "bidirectional",
            }
        )
        sync_state._stores.clear()
        reset_bucket_folder_cache()

        def op_scan() -> None:
            files = get_files_local()
            assert len(files) == n_files + n_links

        results.append(
            measure(f"get_files_local({n_files}f+{n_links}l)", op_scan, count=args.scan_count)
        )

    # ---- path_present: present / dangling symlink / missing ----
    with tempfile.TemporaryDirectory(prefix="hsync_paths_") as tmp:
        present = str(Path(tmp) / "present.bin")
        Path(present).write_bytes(b"x")
        dangling = str(Path(tmp) / "dangling")
        os.symlink("does-not-exist", dangling)
        missing = str(Path(tmp) / "missing.bin")

        def op_present() -> None:
            assert path_present(present) is True

        def op_dangling() -> None:
            assert path_present(dangling) is True

        def op_missing() -> None:
            assert path_present(missing) is False

        results.append(measure("path_present(present)", op_present, count=count))
        results.append(measure("path_present(dangling)", op_dangling, count=count))
        results.append(measure("path_present(missing)", op_missing, count=count))

        # ---- directory-modified branch: os.stat vs Path.stat ----
        def op_os_stat() -> None:
            st = os.stat(tmp)
            assert st.st_mode

        def op_path_stat() -> None:
            st = Path(tmp).stat()
            assert st.st_mode

        results.append(measure("dir_stat(os.stat)", op_os_stat, count=count))
        results.append(measure("dir_stat(Path.stat)", op_path_stat, count=count))

    print(f"sync/path hot-path micro-benchmark (ops={count}, pairs={args.pairs}x2)")
    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
