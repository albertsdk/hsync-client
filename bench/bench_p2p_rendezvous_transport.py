# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""L2 — P2P rendezvous transport benchmark (TLS/TCP vs QUIC/UDP).

The p2p rendezvous plan reaches the discovery node over one of two transports:
TLS/TCP (``--rendezvous-transport tcp``) or QUIC/UDP (``quic``). This script
measures the client-side cost of that choice against a **real** sibling
``hsync-rendezvous`` on loopback. The node runs with ``--no-rate-limit`` — the
documented benchmark seam, which also disables the per-IP connection guard — so
the numbers isolate transport/handshake cost instead of admission policy.

Scenarios, per transport:

* ``handshake`` — ``fetch_fingerprint``: connect + TLS/QUIC handshake + close,
  no ``rdv/1`` frame. This is the dominant per-exchange cost.
* ``register`` / ``query`` / ``refresh`` / ``leave`` — one full exchange each;
  the production client opens a fresh connection per exchange.
* ``--stress`` — N concurrent clients loop register→query→refresh→leave for a
  bounded duration; reports cycles/s, frames/s, error rate and the node/client
  RSS delta. Unique CIDs are left at the end of each cycle so the node table
  does not grow during the run.

The p2p **data plane** is HTTPS/WSS over TCP and is covered separately by
``bench_p2p_peer_transfer.py``; this script measures the rendezvous channel only.

Usage:
    cd hsync-client
    .venv/bin/python bench/bench_p2p_rendezvous_transport.py --count 50
    .venv/bin/python bench/bench_p2p_rendezvous_transport.py --count 50 \
        --stress --stress-seconds 10 --clients 8
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Allow ``from common import ...`` and ``from tests.p2p_helpers import ...``.
BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(BENCH_DIR))
sys.path.insert(0, str(REPO_ROOT))

from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    make_cids,
    measure,
    print_table,
    scaled_warmup,
)
from hsync_common.protocol.tls_trust import TrustPolicy  # noqa: E402

from hsync_client.rdv_client import RendezvousClient  # noqa: E402
from hsync_client.rdv_frames import (  # noqa: E402
    build_leave_frame,
    build_query_frame,
    build_refresh_frame,
    build_register_frame,
)
from tests.p2p_helpers import (  # noqa: E402
    _rendezvous_bin,
    _rendezvous_env,
    _stop_rendezvous,
    _wait_for_rendezvous,
)

HOST = "127.0.0.1"
BUCKET = "bench"
CERT_FP = "aa" * 32
ENDPOINT = [{"proto": "tcp", "host": "198.51.100.7", "port": 8443}]


def free_duplex_port() -> int:
    """Return a loopback port currently free for both a UDP and a TCP listener."""
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind((HOST, 0))
            port = int(udp.getsockname()[1])
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
            try:
                tcp.bind((HOST, port))
            except OSError:
                continue
        return port
    raise RuntimeError("could not reserve a duplex loopback port")


def start_node(port: int, ready_file: Path) -> subprocess.Popen:
    """Start the sibling rendezvous on loopback, ephemeral and without limits."""
    binary = _rendezvous_bin()
    if binary is None:
        raise SystemExit(
            "hsync-rendezvous is not built: expected "
            "../hsync-rendezvous/python/.venv/bin/hsync-rendezvous"
        )
    proc = subprocess.Popen(
        [
            binary,
            "run",
            "--listen-host",
            HOST,
            "--listen-port",
            str(port),
            "--ready-file",
            str(ready_file),
            "--ephemeral",
            "--no-relay",
            "--no-rate-limit",
        ],
        env=_rendezvous_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if not _wait_for_rendezvous(proc, ready_file):
        output = proc.stdout.read() if proc.stdout else ""
        _stop_rendezvous(proc)
        raise SystemExit(f"rendezvous did not become ready: {output}")
    return proc


def rss_kb(pid: int) -> int | None:
    """Resident set size from ``/proc`` (Linux), or ``None`` elsewhere."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^VmRSS:\s+(\d+)\s+kB$", status, re.MULTILINE)
    return int(match.group(1)) if match else None


def register_frame(cid: str):
    return build_register_frame(
        cid=cid,
        bucket=BUCKET,
        cert_fp=CERT_FP,
        version="1.0.0",
        capacity=5,
        endpoints=ENDPOINT,
    )


def new_client(port: int, transport: str) -> RendezvousClient:
    return RendezvousClient(HOST, port, policy=TrustPolicy.none, timeout=10.0, transport=transport)


def _expect_ack(ack) -> None:
    if ack is None:
        raise RuntimeError("exchange dropped")


def _expect_response(response) -> None:
    if response is None:
        raise RuntimeError("query dropped")


def _expect_fingerprint(value: str) -> None:
    if not value:
        raise RuntimeError("no peer certificate")


def run_sequential(port: int, transport: str, count: int) -> list[BenchResult]:
    """Handshake-only cost plus each wire exchange, one connection per call."""
    client = new_client(port, transport)
    seed = make_cids(1, 7_000_000)[0]
    client.register(register_frame(seed))
    warmup = scaled_warmup(count, cap=10)
    results = [
        measure(
            f"{transport}/handshake",
            lambda: _expect_fingerprint(client.fetch_fingerprint()),
            count=count,
            warmup=warmup,
        )
    ]
    registers = iter(make_cids(count + warmup, 1_000_000))
    results.append(
        measure(
            f"{transport}/register",
            lambda: _expect_ack(client.register(register_frame(next(registers)))),
            count=count,
            warmup=warmup,
        )
    )
    results.append(
        measure(
            f"{transport}/query",
            lambda: _expect_response(client.query(build_query_frame(bucket=BUCKET))),
            count=count,
            warmup=warmup,
        )
    )
    # Re-register the seed so a slow run that crossed the TTL still refreshes it.
    client.register(register_frame(seed))
    results.append(
        measure(
            f"{transport}/refresh",
            lambda: _expect_ack(
                client.refresh(build_refresh_frame(cid=seed, bucket=BUCKET, connected=1))
            ),
            count=count,
            warmup=warmup,
        )
    )
    leaves = iter(make_cids(count + warmup, 2_000_000))
    results.append(
        measure(
            f"{transport}/leave",
            lambda: client.leave(build_leave_frame(cid=next(leaves), bucket=BUCKET)),
            count=count,
            warmup=warmup,
        )
    )
    client.leave(build_leave_frame(cid=seed, bucket=BUCKET))
    return results


def run_stress(port: int, transport: str, clients: int, seconds: float, node_pid: int) -> dict:
    """Concurrent register→query→refresh→leave cycles for *seconds*."""
    deadline = time.monotonic() + seconds
    samples: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    node_before = rss_kb(node_pid)
    client_before = rss_kb(os.getpid())

    def worker(index: int) -> None:
        client = new_client(port, transport)
        cycle = 0
        local: list[float] = []
        local_errors: list[str] = []
        while time.monotonic() < deadline:
            cid = make_cids(1, 5_000_000 + index * 100_000 + cycle)[0]
            cycle += 1
            started = time.perf_counter()
            try:
                client.register(register_frame(cid))
                client.query(build_query_frame(bucket=BUCKET))
                client.refresh(build_refresh_frame(cid=cid, bucket=BUCKET, connected=1))
                client.leave(build_leave_frame(cid=cid, bucket=BUCKET))
            except Exception as exc:  # noqa: BLE001 - the benchmark records the failure
                local_errors.append(type(exc).__name__)
                continue
            local.append(time.perf_counter() - started)
        with lock:
            samples.extend(local)
            errors.extend(local_errors)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=clients) as pool:
        list(pool.map(worker, range(clients)))
    elapsed = time.monotonic() - started
    cycle_result = BenchResult(f"{transport}/stress-cycle", samples, len(samples))
    return {
        "transport": transport,
        "clients": clients,
        "duration_s": round(elapsed, 3),
        "cycles": len(samples),
        "frames": len(samples) * 4,
        "cycles_per_s": round(len(samples) / elapsed, 1) if elapsed else 0.0,
        "frames_per_s": round(len(samples) * 4 / elapsed, 1) if elapsed else 0.0,
        "errors": len(errors),
        "error_types": sorted(set(errors)),
        "cycle": cycle_result.as_dict(),
        "node_rss_kb": {"before": node_before, "after": rss_kb(node_pid)},
        "client_rss_kb": {"before": client_before, "after": rss_kb(os.getpid())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure the p2p rendezvous transport (TLS/TCP vs QUIC/UDP)."
    )
    parser.add_argument(
        "--count", type=int, default=50, help="timed samples per sequential scenario"
    )
    parser.add_argument(
        "--transport",
        choices=("both", "tcp", "quic"),
        default="both",
        help="measure TLS/TCP, QUIC/UDP, or both (TCP first, then UDP)",
    )
    parser.add_argument("--stress", action="store_true", help="run the concurrent stress phase")
    parser.add_argument("--stress-seconds", type=float, default=10.0)
    parser.add_argument("--clients", type=int, default=8)
    add_json_out(parser)
    args = parser.parse_args()

    transports = ("tcp", "quic") if args.transport == "both" else (args.transport,)
    port = free_duplex_port()
    ready_file = Path(tempfile.mkdtemp(prefix="hsync-rdv-bench-")) / "rendezvous.ready"
    proc = start_node(port, ready_file)
    results: list[BenchResult] = []
    stress: list[dict] = []
    try:
        for transport in transports:
            label = "TLS/TCP" if transport == "tcp" else "QUIC/UDP"
            print(f"\n=== {transport.upper()} ({label}) ===")
            scenario = run_sequential(port, transport, args.count)
            print_table(scenario)
            results.extend(scenario)
            if args.stress:
                stats = run_stress(port, transport, args.clients, args.stress_seconds, proc.pid)
                print(
                    f"stress: {stats['cycles']} cycles / {stats['frames']} frames in "
                    f"{stats['duration_s']}s = {stats['cycles_per_s']} cycles/s, "
                    f"{stats['frames_per_s']} frames/s, {stats['errors']} errors"
                    f" {stats['error_types'] or ''}"
                )
                print(f"rss: node {stats['node_rss_kb']} client {stats['client_rss_kb']} (kB)")
                stress.append(stats)
    finally:
        _stop_rendezvous(proc)

    if args.json_out:
        payload = {"results": [result.as_dict() for result in results], "stress": stress}
        Path(args.json_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\njson: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
