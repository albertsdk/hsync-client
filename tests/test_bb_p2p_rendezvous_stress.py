# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Stress — p2p rendezvous transport (TLS/TCP and QUIC/UDP).

Black-box load on the **rendezvous channel** of p2p mode: a real sibling
``hsync-rendezvous`` runs on loopback (``--no-rate-limit``, the documented
benchmark seam, so admission policy does not mask transport cost), and N
concurrent ``RendezvousClient`` workers each loop
register → query → refresh → leave.

Per transport (TCP first, then UDP) the test asserts:

* **correctness** — every exchange returns the expected frame (no dropped or
  refused frames; errors are collected, never swallowed);
* **latency regression** — the median full cycle stays under a loopback bound.
  The bound is deliberately generous (CI machines vary) but below the ~130 ms
  per-exchange cost a QUIC connection paid when the client waited for the
  peer's connection drain on every close — the regression this test exists to
  catch;
* **no leak** — the node's RSS delta over the run stays bounded.

Opt-in: ``HSYNC_BB_STRESS=1`` (like the other stress suites). Skips when the
sibling rendezvous is not built. Knobs: ``HSYNC_BB_STRESS_RDV_CLIENTS`` (default
8), ``HSYNC_BB_STRESS_RDV_SECONDS`` (default 5).
"""

from __future__ import annotations

import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from hsync_common.protocol.tls_trust import TrustPolicy

from hsync_client.rdv_client import RendezvousClient
from hsync_client.rdv_frames import (
    build_leave_frame,
    build_query_frame,
    build_refresh_frame,
    build_register_frame,
)
from tests.p2p_helpers import (
    _rendezvous_bin,
    _rendezvous_start,
    _reserve_udp_port,
    _stop_rendezvous,
    _wait_for_rendezvous,
)

_ENABLED = os.environ.get("HSYNC_BB_STRESS") == "1"
pytestmark = pytest.mark.skipif(
    not _ENABLED, reason="Stress tests are opt-in; set HSYNC_BB_STRESS=1 to enable"
)

HOST = "127.0.0.1"
BUCKET = "rdvstress"
CERT_FP = "bb" * 32

# Per-exchange median bounds (ms) measured with ONE client, where the cost is
# stable. TLS/TCP lands at ~2 ms and QUIC at ~13 ms on loopback; a transport that
# waits for the peer's connection drain on close costs ~130 ms per exchange, an
# order of magnitude above these bounds. Concurrency is covered separately: a
# GIL-bound transport makes per-cycle latency scale with the client count, so a
# tight absolute bound there would flake without adding signal.
_EXCHANGE_P50_BOUND_MS = {"tcp": 30.0, "quic": 60.0}
# A cycle that never finishes within this bound is a hang, not slow hardware.
_CYCLE_P50_SANITY_MS = 5000.0
# Node RSS growth allowed over one run (no leak). Session/limit state is
# bounded by the daemon; a leak would show as tens of MB per second.
_MAX_NODE_RSS_DELTA_KB = 128 * 1024

_CLIENTS = int(os.environ.get("HSYNC_BB_STRESS_RDV_CLIENTS", "8"))
_SECONDS = float(os.environ.get("HSYNC_BB_STRESS_RDV_SECONDS", "5"))


def _rss_kb(pid: int) -> int | None:
    """Resident set size from ``/proc`` (Linux), or ``None`` elsewhere."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return None


def _cid(rng: random.Random) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(rng.choice(alphabet) for _ in range(21))


@pytest.fixture
def rendezvous_node(tmp_path: Path):
    """Start the sibling rendezvous unlimited on one loopback port; always tear it down."""
    if _rendezvous_bin() is None:
        pytest.skip("hsync-rendezvous is not built (sibling repo venv missing)")
    port = _reserve_udp_port()
    ready_file = tmp_path / "rendezvous.ready"
    try:
        # --no-rate-limit also disables the per-IP connection guard: the seam is
        # documented for benchmarks, and it keeps admission out of transport cost.
        proc = _rendezvous_start(
            port,
            ready_file,
            extra_args=("--ephemeral", "--no-relay", "--no-rate-limit"),
        )
    except FileNotFoundError:
        pytest.skip("hsync-rendezvous is not built (sibling repo venv missing)")
    try:
        if not _wait_for_rendezvous(proc, ready_file):
            pytest.skip("rendezvous did not become ready")
        yield proc, port
    finally:
        _stop_rendezvous(proc)


def _cycle(client: RendezvousClient, cid: str) -> None:
    """One full rdv/1 lease cycle; every exchange must be answered."""
    ack = client.register(
        build_register_frame(
            cid=cid,
            bucket=BUCKET,
            cert_fp=CERT_FP,
            version="1.0.0",
            capacity=5,
            endpoints=[{"proto": "tcp", "host": "198.51.100.7", "port": 8443}],
        )
    )
    assert ack is not None and ack.get("type") == "ack", ack
    response = client.query(build_query_frame(bucket=BUCKET))
    assert response is not None and response.get("type") == "response", response
    refreshed = client.refresh(build_refresh_frame(cid=cid, bucket=BUCKET, connected=1))
    assert refreshed is not None and refreshed.get("type") == "ack", refreshed
    client.leave(build_leave_frame(cid=cid, bucket=BUCKET))


def _run_load(port: int, transport: str, clients: int, seconds: float) -> dict:
    """Concurrent cycles for *seconds*; returns latency samples and errors."""
    deadline = time.monotonic() + seconds
    samples: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        rng = random.Random(random.randrange(2**32))
        client = RendezvousClient(
            HOST, port, policy=TrustPolicy.none, timeout=10.0, transport=transport
        )
        local: list[float] = []
        local_errors: list[str] = []
        while time.monotonic() < deadline:
            cid = _cid(rng)
            started = time.perf_counter()
            try:
                _cycle(client, cid)
            except Exception as exc:  # noqa: BLE001 - the stress report records the failure
                local_errors.append(f"{type(exc).__name__}: {exc}")
                continue
            local.append((time.perf_counter() - started) * 1000.0)
        with lock:
            samples.extend(local)
            errors.extend(local_errors)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=clients) as pool:
        list(pool.map(worker, range(clients)))
    elapsed = time.monotonic() - started
    ordered = sorted(samples)
    p50 = ordered[len(ordered) // 2] if ordered else 0.0
    p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))] if ordered else 0.0
    return {
        "transport": transport,
        "cycles": len(samples),
        "errors": errors,
        "elapsed_s": elapsed,
        "cycles_per_s": len(samples) / elapsed if elapsed else 0.0,
        "p50_ms": p50,
        "p99_ms": p99,
    }


def _report(stats: dict, rss_before: int | None, rss_after: int | None) -> None:
    print(
        f"\n[RDV-STRESS] transport={stats['transport']} clients={_CLIENTS} "
        f"cycles={stats['cycles']} in {stats['elapsed_s']:.2f}s "
        f"({stats['cycles_per_s']:.1f} cycles/s) "
        f"p50={stats['p50_ms']:.2f}ms p99={stats['p99_ms']:.2f}ms "
        f"errors={len(stats['errors'])} rss_kb={rss_before}->{rss_after}"
    )


@pytest.mark.parametrize("transport", ["tcp", "quic"])
def test_rendezvous_transport_sustains_concurrent_lease_cycles(
    rendezvous_node, transport: str
) -> None:
    """Given a limit-free node, N concurrent lease cycles run without errors/leaks."""
    # Given: a real rendezvous on loopback with rate limits and the guard disabled
    proc, port = rendezvous_node
    rss_before = _rss_kb(proc.pid)

    # When: N clients each loop register -> query -> refresh -> leave
    stats = _run_load(port, transport, _CLIENTS, _SECONDS)
    rss_after = _rss_kb(proc.pid)
    _report(stats, rss_before, rss_after)

    # Then: every exchange was answered and throughput actually happened
    assert stats["cycles"] > 0, "no cycle completed"
    assert stats["errors"] == [], stats["errors"][:5]

    # And: no cycle hangs (a real close-drain regression is caught per exchange
    # by the single-client test below; here only a pathological stall matters)
    assert stats["p50_ms"] < _CYCLE_P50_SANITY_MS, (
        f"{transport} p50 {stats['p50_ms']:.2f}ms exceeds {_CYCLE_P50_SANITY_MS:.0f}ms"
    )

    # And: the daemon did not leak memory over the run
    if rss_before is not None and rss_after is not None:
        assert rss_after - rss_before < _MAX_NODE_RSS_DELTA_KB, (
            f"node RSS grew {rss_after - rss_before} kB"
        )


@pytest.mark.parametrize("transport", ["tcp", "quic"])
def test_single_client_exchange_latency_has_no_close_drain(rendezvous_node, transport: str) -> None:
    """One client's exchange latency stays in the transport's sane range.

    ``rdv/1`` opens a fresh connection per exchange, so the close path is on the
    hot path. This measures a full register→query→refresh→leave cycle with a
    single client (no GIL contention, so the number is stable) and bounds the
    per-exchange cost: waiting for the peer's connection drain on close would
    blow past it by an order of magnitude.
    """
    # Given: a limit-free node and one client on the chosen transport
    _, port = rendezvous_node
    client = RendezvousClient(
        HOST, port, policy=TrustPolicy.none, timeout=10.0, transport=transport
    )
    samples: list[float] = []
    # When: ten full lease cycles run back to back
    for index in range(10):
        cid = _cid(random.Random(index))
        started = time.perf_counter()
        _cycle(client, cid)
        samples.append((time.perf_counter() - started) * 1000.0 / 4.0)  # per exchange
    # Then: the median exchange stays under the transport's bound
    median = sorted(samples)[len(samples) // 2]
    assert median < _EXCHANGE_P50_BOUND_MS[transport], (
        f"{transport} exchange median {median:.2f}ms exceeds "
        f"{_EXCHANGE_P50_BOUND_MS[transport]:.0f}ms"
    )


def test_tcp_and_quic_have_comparable_per_exchange_cost(rendezvous_node) -> None:
    """Both transports complete a lease cycle in the same order of magnitude.

    QUIC is heavier than TLS/TCP on loopback (UDP + congestion control state),
    but a per-exchange regression in one transport — like the QUIC close-drain
    that cost ~130 ms — breaks the ratio long before any absolute SLA.
    """
    # Given: a limit-free node and one measured cycle per transport
    _, port = rendezvous_node
    tcp = _run_load(port, "tcp", 2, 2.0)
    quic = _run_load(port, "quic", 2, 2.0)

    # When/Then: both report real samples
    assert tcp["cycles"] > 0 and quic["cycles"] > 0
    assert not tcp["errors"] and not quic["errors"]

    # And: neither transport is an order of magnitude slower than the other
    slower = max(tcp["p50_ms"], quic["p50_ms"])
    faster = min(tcp["p50_ms"], quic["p50_ms"])
    assert slower <= faster * 20 + 20.0, (
        f"transport spread too large: tcp p50={tcp['p50_ms']:.2f}ms quic p50={quic['p50_ms']:.2f}ms"
    )
