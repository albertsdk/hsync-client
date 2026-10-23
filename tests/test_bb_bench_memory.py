# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""End-to-end memory/time benchmark for continuous sync + delta upload.

Gated (never runs in CI):

    HSYNC_BB_SERVER=1 HSYNC_BENCH=1 .venv/bin/python -m pytest -q -s \\
        tests/test_bb_bench_memory.py

Tunables: ``HSYNC_BENCH_FILE_MB`` (default 32) and ``HSYNC_BENCH_CHANGE``
(default 0.25 = 25% of the file rewritten in place).

The benchmark drives the real client (subprocess) against the real server: an
initial full upload of an incompressible file, then a large in-place edit and
the resulting delta upload. It records the client's peak RSS (``VmHWM``), the
initial/delta wall time and whether the delta path was used. The assertion is a
coarse no-O(file)-buffering guard; the exact heap numbers live in the unit
memory tests (``test_memory_bounds.py`` and ``hsync-common/test_delta.py``).
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest
import requests

from tests.conftest import _ready_path, _wait_for
from tests.p2p_helpers import _terminate

_BENCH_ENABLED = os.environ.get("HSYNC_BENCH") == "1" and os.environ.get("HSYNC_BB_SERVER") == "1"
pytestmark = pytest.mark.skipif(
    not _BENCH_ENABLED,
    reason="End-to-end memory benchmark: set HSYNC_BENCH=1 and HSYNC_BB_SERVER=1",
)

FILE_MB = int(os.environ.get("HSYNC_BENCH_FILE_MB", "32"))
CHANGE_FRACTION = float(os.environ.get("HSYNC_BENCH_CHANGE", "0.25"))


def _vm_hwm_mb(pid: int) -> float:
    """Peak RSS (high-water mark) of *pid*, in MiB."""
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return 0.0
    for line in text.splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024
    return 0.0


def _sha_file(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _server_sha(base: str, api_key: str, bucket: str, name: str) -> str | None:
    response = requests.get(
        f"{base}/api/v1/buckets/{bucket}/files",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15.0,
        verify=False,
    )
    response.raise_for_status()
    for entry in response.json():
        if entry["filepath"] == name:
            return entry.get("sha256sum")
    return None


class TestDeltaUploadBenchmark:
    def test_delta_upload_memory_and_time(
        self, live_server, bb_bucket, local_dir, watch_sync, tmp_path
    ):
        base, api_key = live_server
        name = "bench.bin"
        size = FILE_MB * 1024 * 1024
        changed = max(1, int(size * CHANGE_FRACTION))
        log_path = tmp_path / "client.log"
        delta_log = tmp_path / "client_delta.log"
        proc = watch_sync(bb_bucket, local_dir, "--log-file", str(delta_log))
        try:
            assert _wait_for(lambda: Path(_ready_path(local_dir)).exists(), timeout=60), (
                "continuous client did not become ready"
            )
            time.sleep(1)  # let the watcher install its baseline
            baseline_peak = _vm_hwm_mb(proc.pid)

            local_path = Path(local_dir) / name
            local_path.write_bytes(os.urandom(size))
            initial_sha = _sha_file(local_path)
            t0 = time.monotonic()
            assert _wait_for(
                lambda: _server_sha(base, api_key, bb_bucket, name) == initial_sha,
                timeout=180,
            ), "initial upload did not converge"
            initial_s = time.monotonic() - t0
            after_initial = _vm_hwm_mb(proc.pid)

            # Rewrite a large contiguous region in place (same size → delta).
            with local_path.open("r+b") as fh:
                fh.seek(size // 2)
                fh.write(os.urandom(changed))
            expected = _sha_file(local_path)
            t1 = time.monotonic()
            assert _wait_for(
                lambda: _server_sha(base, api_key, bb_bucket, name) == expected,
                timeout=180,
            ), "delta upload did not converge"
            delta_s = time.monotonic() - t1
            peak = _vm_hwm_mb(proc.pid)

            # The loguru file sink is asynchronous (enqueue=True): wait for the
            # delta line to be flushed before deciding whether the path ran.
            def _delta_in_log() -> bool:
                text = delta_log.read_text(errors="replace") if delta_log.exists() else ""
                return "[DELTA" in text or "Delta upload" in text

            delta_attempted = _wait_for(_delta_in_log, timeout=10)
            log_tail = delta_log.read_text(errors="replace") if delta_log.exists() else ""
            if not log_tail:
                log_tail = log_path.read_text(errors="replace") if log_path.exists() else ""
            delta_ok = "[DELTA" in log_tail  # successful apply (not a 409 fallback)
            growth = peak - baseline_peak
            print(
                f"\n[BENCH] file={FILE_MB} MB changed={changed / 1e6:.1f} MB "
                f"initial={initial_s:.2f}s delta={delta_s:.2f}s "
                f"rss_baseline_peak={baseline_peak:.1f} MB "
                f"rss_after_initial={after_initial:.1f} MB rss_peak={peak:.1f} MB "
                f"growth={growth:.1f} MB delta_attempted={delta_attempted} delta_ok={delta_ok}"
            )

            # The memory benchmark needs the delta code path to run (the sink +
            # streamed multipart). A causal 409 may make the client fall back to
            # a full transfer; that is a delta-versioning behaviour, not the
            # memory optimisation, so it is reported rather than failed here.
            assert delta_attempted, f"delta path not attempted; log tail:\n{log_tail[-2000:]}"
            # Coarse guard: a buffered payload (literals + compression + the
            # requests multipart body) would grow RSS by several times the file
            # size; streaming stays near the mmap window + chunk buffers.
            assert growth < FILE_MB * 1.5 + 32, (
                f"client RSS grew {growth:.1f} MB for a {FILE_MB} MB file"
            )
        finally:
            _terminate(proc)
