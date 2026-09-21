#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""HTTP-level LAN transfer benchmark (see hsync-client docs/internal/stress-test-plan.md).

Measures the LAN master's per-request overhead with and without keep-alive,
plus the compressed/uncompressed download throughput for a large compressible
file. The pre-keep-alive HTTP/1.0 behaviour ("new connection per request") is
emulated by sending ``Connection: close``: the master then tears the
connection down after every response, reproducing the old model on the same
binary — so the two modes below are a direct before/after comparison.

Run with the client's virtualenv from the repository root:

    .venv/bin/python scripts/benchmark_p2p_transfers.py
"""

import http.client
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from hsync_client.lan import get_free_port  # noqa: E402

from hsync_client.p2p_server import P2PServer  # noqa: E402

N_HEALTH = 1500
N_STATUS = 500
N_SMALL_DOWNLOADS = 300
BIG_FILE_MB = 200


def _timed_requests(port: int, path: str, *, keep_alive: bool, n: int, params: str = "") -> float:
    """Run *n* sequential GETs; return elapsed seconds.

    keep_alive=False sends ``Connection: close`` and opens a fresh connection
    per request (the pre-keep-alive model); keep_alive=True reuses a single
    persistent connection (the current model).
    """
    t0 = time.perf_counter()
    conn = None
    for _ in range(n):
        if conn is None:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        headers = {} if keep_alive else {"Connection": "close"}
        conn.request("GET", f"{path}{params}", headers=headers)
        response = conn.getresponse()
        while response.read(256 * 1024):
            pass
        if not keep_alive or response.getheader("Connection", "").lower() == "close":
            conn.close()
            conn = None
    if conn is not None:
        conn.close()
    return time.perf_counter() - t0


def _report(title: str, elapsed_close: float, elapsed_keepalive: float, n: int) -> None:
    rate_close = n / elapsed_close
    rate_ka = n / elapsed_keepalive
    print(
        f"\n{title}\n"
        f"  connection-per-request (pre-keep-alive): {elapsed_close:7.3f}s "
        f"({rate_close:8.0f} req/s)\n"
        f"  keep-alive (current):                   {elapsed_keepalive:7.3f}s "
        f"({rate_ka:8.0f} req/s)\n"
        f"  speedup: {elapsed_close / elapsed_keepalive:5.1f}x"
    )


def main() -> None:
    # Quiet the logger: per-request DEBUG lines would otherwise dominate the
    # loop timings (and the output).
    from loguru import logger

    logger.remove()

    tmp = Path(tempfile.mkdtemp(prefix="hsync-bench-"))
    try:
        # Bucket content used by the download phases
        small = b"0123456789abcdef" * 1024  # 16 KiB
        (tmp / "small.txt").write_bytes(small)
        big = b"hsync-benchmark-pattern " * 1024  # 23 KiB, trivially compressible
        big_path = tmp / "big.txt"
        with big_path.open("wb") as f:
            for _ in range(BIG_FILE_MB * 1024 * 1024 // len(big)):
                f.write(big)

        import hsync_client.p2p_server as p2p_server

        p2p_server.CONFIG = {
            "LOCAL_BUCKET": str(tmp),
            "P2P_KEY": None,
        }

        port = get_free_port()
        server = P2PServer(port=port)
        server.start()
        time.sleep(0.1)
        try:
            print(f"LAN master on 127.0.0.1:{port} (HTTP), bucket={tmp}")

            # Phase A — tiny JSON endpoint (pure per-request overhead)
            close_a = _timed_requests(port, "/api/v1/health", keep_alive=False, n=N_HEALTH)
            ka_a = _timed_requests(port, "/api/v1/health", keep_alive=True, n=N_HEALTH)
            _report(f"A) {N_HEALTH} sequential GET /health", close_a, ka_a, N_HEALTH)

            # Phase B — upload-status (the per-file round trip every upload pays)
            params = "?filename=small.txt&sha256sum=" + "0" * 64 + "&size=16384"
            status_path = "/api/v1/buckets/test/files/upload-status"
            close_b = _timed_requests(
                port, status_path, keep_alive=False, n=N_STATUS, params=params
            )
            ka_b = _timed_requests(port, status_path, keep_alive=True, n=N_STATUS, params=params)
            _report(f"B) {N_STATUS} sequential GET upload-status", close_b, ka_b, N_STATUS)

            # Phase C — small-file downloads (real endpoint, 16 KiB bodies)
            dl = "/api/v1/buckets/test/files/download?filename=small.txt"
            close_c = _timed_requests(port, dl, keep_alive=False, n=N_SMALL_DOWNLOADS)
            ka_c = _timed_requests(port, dl, keep_alive=True, n=N_SMALL_DOWNLOADS)
            _report(
                f"C) {N_SMALL_DOWNLOADS} sequential 16 KiB downloads",
                close_c,
                ka_c,
                N_SMALL_DOWNLOADS,
            )

            # Phase D — big compressible file: zstd (in-RAM, below cap) vs raw
            big_dl = "/api/v1/buckets/test/files/download?filename=big.txt"
            for enabled in (True, False):
                p2p_server.CONFIG["COMPRESSION_ENABLED"] = enabled
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
                t0 = time.perf_counter()
                conn.request("GET", big_dl)
                response = conn.getresponse()
                total = 0
                while chunk := response.read(256 * 1024):
                    total += len(chunk)
                elapsed = time.perf_counter() - t0
                conn.close()
                mode = "zstd (compressed)" if enabled else "raw (uncompressed)"
                print(
                    f"\nD) {BIG_FILE_MB} MB compressible download — {mode}: "
                    f"{elapsed:6.2f}s ({total / (1024 * 1024):.1f} MB over the wire, "
                    f"Content-Encoding={response.getheader('Content-Encoding', 'none')})"
                )
        finally:
            server.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
