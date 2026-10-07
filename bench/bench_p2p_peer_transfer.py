# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""L2 — P2P peer-assisted download wire benchmark (loopback, real servers).

Measures the two full-download paths of ``p2p/1`` end to end with real
``P2PServer`` instances, real TLS (self-signed certificate + fingerprint
pinning) and the real production entry point (``hsync_client.transfer.download_file``):

* **peer path** — exact-hash ``GET`` against a read-only seeder, cold (fresh
  thread session, so a full TCP+TLS handshake is inside the sample) and warm
  (keep-alive session reused);
* **master path** — the canonical fallback, always warm: a P2P slave already
  holds its master session for the whole role lifetime;
* **seeder read cap** — concurrent exact-hash GETs against
  ``HSYNC_P2P_PEER_READ_CAP`` (2), which exposes the ``503`` saturation rate the
  peer path converts into a master fallback;
* **production fallback** — concurrent ``download_file`` calls under that cap;
* **cycle** — a whole cycle of small files on a fixed worker count, peer path vs
  master-only baseline, reporting per cycle how many files the peer served, how
  many attempts it refused for saturation, and how many full downloads fell back
  to the master. This is the scenario peer-admission changes are judged on.

The per-size p50 of the peer and master paths yields the crossing size at which
the peer path stops repaying its fixed setup cost; the derived block printed at
the end reports it together with the effective throughput, which shows how much
a peer's ``throughput_ema`` is depressed by fixed cost on small files. That
crossing is an observation about the transport, not a routing policy: no size
threshold decides eligibility (ADR-0030).

Out of the samples on purpose: the TOFU first-use cost (``measure_with_setup``
warms up before timing, so the peer is already pinned — exactly as in production,
where a peer is pinned before it serves; ``bench_pinning.py`` measures first use
itself) and the full-body SHA-256 verification, which is re-checked once per
scenario outside the timed region because hashing 8 MiB would dwarf the
differences under test.

Usage:
    cd hsync-client && .venv/bin/python bench/bench_p2p_peer_transfer.py [--count N] [--json-out FILE] [--latency]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from unittest.mock import patch

# Allow ``from common import ...`` when run from the bench/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    make_cid,
    maybe_write_json,
    print_table,
)
from hsync_common.models.file import File  # noqa: E402
from p2p_peer_common import (  # noqa: E402
    DownloadOutcomeRecorder,
    LatencyRelay,
    P2PRequesterEnv,
    P2PServerHarness,
    build_file,
    concurrent_peer_gets,
    install_failure_recorder,
    install_outcome_recorder,
    measure_with_setup,
    write_payload,
)

from hsync_client.p2p_server import P2PServerMode  # noqa: E402
from hsync_client.p2p_sources import P2PPeerSource, P2PSourceResolver  # noqa: E402
from hsync_client.transfer import download_file  # noqa: E402

MASTER_CID = make_cid(101)
SEEDER_CID = make_cid(102)
REQUESTER_CID = make_cid(103)

# Sizes spanning three orders of magnitude below the master-sized payloads, so
# the peer/master crossing (fixed setup cost vs bandwidth) is visible inside the
# measured range.
SIZES: tuple[tuple[str, int], ...] = (
    ("4KiB", 4 * 1024),
    ("64KiB", 64 * 1024),
    ("256KiB", 256 * 1024),
    ("1MiB", 1024 * 1024),
    ("8MiB", 8 * 1024 * 1024),
)
REFERENCE_LABEL = "4KiB"
PEER_READ_CAP = 2
CONCURRENCY_SWEEP = (1, 2, 4, 8)
PAYLOAD_REL = "payload.bin"
CAP_REL = "cap/probe.bin"
CAP_SIZE = 4 * 1024
FALLBACK_FILES = tuple(f"cap/batch-{index}.bin" for index in range(4))
FALLBACK_SIZE = 4 * 1024

# The cycle scenario: one sync cycle's worth of small files pulled by a fixed
# worker count, the shape production has when a slave receives many small files.
# Both axes are swept because the seeder's read cap only bites when enough
# requests overlap: tiny files with few workers rarely collide, larger bodies
# with a wide pool do.
CYCLE_FILES = 24
CYCLE_SIZES: tuple[tuple[str, int], ...] = (
    ("1KiB", 1024),
    ("4KiB", 4 * 1024),
    ("256KiB", 256 * 1024),
)
CYCLE_WORKERS = (4, 8)

# The latency-cycles scenario: an injected RTT in front of the seeder, a
# production-shaped long-lived transfer pool, and the TLS handshakes a cycle pays.
# It answers the question a worker-to-seeder affinity scheme would have to win.
LATENCY_LABEL = "lat4KiB"
LATENCY_SIZE = 4 * 1024
LATENCY_WORKERS = (2, 8)
# Bulk parallelism sweep for the master path: one worker is the sequential loop
# the mobile sync used, four is the bound it now applies.
LATENCY_MASTER_WORKERS = (1, 4)
# One-way delay per forwarded chunk, so a request/response pair costs one RTT.
LATENCY_DELAYS_S = (0.0, 0.02)

PEER_PREFIX = "peer"
COLD_INFIX = "cold"
WARM_INFIX = "warm"
MASTER_PREFIX = "master"


def _scenario_name(path: str, stage: str, label: str) -> str:
    """Stable scenario name, e.g. ``peer_cold_4KiB`` / ``master_warm_8MiB``."""
    return f"{path}_{stage}_{label}"


def _sha256_file(path: Path) -> str:
    """Streaming SHA-256 of a downloaded file (used once per scenario)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_downloaded(requester: P2PRequesterEnv, file: File) -> None:
    """Per-sample branch assertion: whole body landed, no partial left behind."""
    target = requester.bucket_dir / str(file.filepath)
    assert target.is_file(), target
    assert target.stat().st_size == file.size, target
    assert not Path(f"{target}.part").exists(), target


def _verify_payload(requester: P2PRequesterEnv, file: File, digest: str) -> None:
    """Verify the digest of the last downloaded body, outside the timed region."""
    assert _sha256_file(requester.bucket_dir / str(file.filepath)) == digest


def _peer_sample(
    requester: P2PRequesterEnv,
    file: File,
    candidates: dict[tuple[str, str], tuple[P2PPeerSource, ...]],
    resolver: P2PSourceResolver,
) -> None:
    """One production peer download through the real resolver and candidate map."""
    download_file(file, candidates=candidates, source_resolver=resolver)
    _assert_downloaded(requester, file)


def _master_sample(requester: P2PRequesterEnv, file: File) -> None:
    """One production master download (``source_resolver=None`` = canonical path)."""
    download_file(file)
    _assert_downloaded(requester, file)


def _download_one(
    requester: P2PRequesterEnv,
    file: File,
    *,
    candidates: dict[tuple[str, str], tuple[P2PPeerSource, ...]],
    resolver: P2PSourceResolver,
) -> None:
    """One production peer download inside its own worker thread."""
    download_file(file, candidates=candidates, source_resolver=resolver)
    _assert_downloaded(requester, file)


def _drain_files(
    requester: P2PRequesterEnv,
    files: list[File],
    candidates: dict[tuple[str, str], tuple[P2PPeerSource, ...]],
    resolver: P2PSourceResolver,
    *,
    workers: int,
    use_peer: bool,
) -> None:
    """Drain *files* through the production download path on *workers* threads.

    Peer mode hands the candidate map to the resolver (the real peer path with
    its admission/failure handling); master mode calls the canonical path with no
    source at all. Both assert the body landed, so a silent fallback cannot be
    reported as a fast peer transfer.
    """
    worker = (
        partial(_download_one, requester, candidates=candidates, resolver=resolver)
        if use_peer
        else partial(_master_sample, requester)
    )
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bench_cycle") as pool:
        list(pool.map(worker, files))


def _run_size_matrix(
    count: int,
    *,
    master: P2PServerHarness,
    seeder: P2PServerHarness,
    requester: P2PRequesterEnv,
) -> list[BenchResult]:
    """Cold peer / warm peer / warm master per size, plus one bookkeeping probe."""
    results: list[BenchResult] = []
    for label, size in SIZES:
        content, digest = write_payload(seeder.bucket_dir, PAYLOAD_REL, size)
        write_payload(master.bucket_dir, PAYLOAD_REL, size)
        file = build_file(PAYLOAD_REL, content, digest)
        resolver = requester.resolver()
        # ``_identity`` is the production eligibility filter. Every size passes it
        # now that size no longer gates the peer path, so the bench is keyed
        # exactly like the engine instead of re-deriving the wire identity.
        key = resolver._identity(file)
        assert key == (PAYLOAD_REL, digest), key
        candidates = {key: (seeder.peer_source(),)}

        results.append(
            measure_with_setup(
                _scenario_name(PEER_PREFIX, COLD_INFIX, label),
                partial(requester.remove_destination, PAYLOAD_REL, drop_sessions=True),
                partial(_peer_sample, requester, file, candidates, resolver),
                count=count,
            )
        )
        _verify_payload(requester, file, digest)

        results.append(
            measure_with_setup(
                _scenario_name(PEER_PREFIX, WARM_INFIX, label),
                partial(requester.remove_destination, PAYLOAD_REL),
                partial(_peer_sample, requester, file, candidates, resolver),
                count=count,
            )
        )
        _verify_payload(requester, file, digest)

        results.append(
            measure_with_setup(
                _scenario_name(MASTER_PREFIX, WARM_INFIX, label),
                partial(requester.remove_destination, PAYLOAD_REL),
                partial(_master_sample, requester, file),
                count=count,
            )
        )
        _verify_payload(requester, file, digest)

        if label == REFERENCE_LABEL:
            # Isolates the per-file sync-state write the peer and master paths
            # both pay, so the comparison can also be read net of bookkeeping.
            with patch("hsync_client.transfer._record_synced_base"):
                results.append(
                    measure_with_setup(
                        f"{_scenario_name(PEER_PREFIX, WARM_INFIX, label)}_no_bookkeeping",
                        partial(requester.remove_destination, PAYLOAD_REL),
                        partial(_peer_sample, requester, file, candidates, resolver),
                        count=count,
                    )
                )
            _verify_payload(requester, file, digest)
    return results


def _run_read_cap_sweep(
    count: int,
    *,
    seeder: P2PServerHarness,
    requester: P2PRequesterEnv,
) -> tuple[list[BenchResult], list[tuple[int, int, int]]]:
    """One seeder serving ``count`` small files to ``workers`` warm requester threads.

    Threads are created once per worker count and reused for every request, so
    after the first request each thread holds a keep-alive session — the shape a
    production transfer pool has when a slave pulls many small files.

    Returns:
        ``(results, summary)`` where each summary row is
        ``(workers, http_200_count, http_503_count)``.
    """
    _content, digest = write_payload(seeder.bucket_dir, CAP_REL, CAP_SIZE)
    params = {"filename": CAP_REL, "sha256": digest, "cid": requester.cid}
    results: list[BenchResult] = []
    summary: list[tuple[int, int, int]] = []
    for workers in CONCURRENCY_SWEEP:
        outcomes = concurrent_peer_gets(
            [(seeder.download_url, params)] * count,
            cert_fingerprint=seeder.cert_fingerprint,
            workers=workers,
        )
        latencies: list[float] = []
        statuses: list[int] = []
        for latency, status, received in outcomes:
            latencies.append(latency)
            statuses.append(status)
            assert status in (200, 503), status
            assert received == CAP_SIZE or status == 503, (status, received)
        assert statuses.count(200) > 0, statuses
        results.append(BenchResult(f"seeder_readcap_batch({workers})", latencies, len(latencies)))
        summary.append((workers, statuses.count(200), statuses.count(503)))
    return results, summary


def _run_production_fallback(
    count: int,
    *,
    master: P2PServerHarness,
    seeder: P2PServerHarness,
    requester: P2PRequesterEnv,
) -> list[BenchResult]:
    """Concurrent production downloads of small files under the same read cap."""
    resolver = requester.resolver()
    files: list[File] = []
    candidates: dict[tuple[str, str], tuple[P2PPeerSource, ...]] = {}
    for relpath in FALLBACK_FILES:
        content, digest = write_payload(seeder.bucket_dir, relpath, FALLBACK_SIZE)
        write_payload(master.bucket_dir, relpath, FALLBACK_SIZE)
        file = build_file(relpath, content, digest)
        key = resolver._identity(file)
        assert key is not None, relpath
        files.append(file)
        candidates[key] = (seeder.peer_source(),)

    drain = partial(
        _drain_files,
        requester,
        files,
        candidates,
        resolver,
        workers=len(files),
        use_peer=True,
    )
    results = [
        measure_with_setup(
            f"peer_cap{PEER_READ_CAP}_fallback({len(files)} files)",
            partial(requester.remove_destinations, FALLBACK_FILES),
            drain,
            count=max(1, count // len(files)),
        )
    ]
    for file in files:
        target = requester.bucket_dir / str(file.filepath)
        assert target.is_file() and target.stat().st_size == file.size, target
    return results


def _run_cycle_scenarios(
    count: int,
    *,
    master: P2PServerHarness,
    seeder: P2PServerHarness,
    requester: P2PRequesterEnv,
) -> tuple[list[BenchResult], list[tuple[str, int, float, int, int]]]:
    """Cycle sweep (file size x worker count), peer path vs master-only baseline.

    This is the scenario the peer-admission change is judged on: it reports, per
    configuration, how many files the peer served per cycle, the worst per-cycle
    count of saturation refusals, and how many full downloads ended up on the
    master. Each configuration runs with and without the peer path on the same
    file set and worker count, so the wall time comparison is apples to apples.

    Returns:
        ``(results, outcomes)`` where each outcome row is ``(label, workers,
        size_bytes, avg_peer_ok_per_cycle, worst_cycle_busy, total_master_full)``.
    """
    resolver = requester.resolver()
    recorder = DownloadOutcomeRecorder()
    outcome_patcher = install_outcome_recorder(recorder)
    failure_patcher = install_failure_recorder(recorder)
    results: list[BenchResult] = []
    outcomes: list[tuple[str, int, float, int, int]] = []
    try:
        for size_label, size in CYCLE_SIZES:
            files, candidates, relpaths = _build_cycle_fixture(
                master, seeder, resolver, size_label, size
            )
            for workers in CYCLE_WORKERS:
                for use_peer in (True, False):
                    label = "peer" if use_peer else "master"
                    per_cycle: list[tuple[int, int, int, int]] = []
                    results.append(
                        measure_with_setup(
                            f"cycle_{label}_{size_label}({CYCLE_FILES}f,{workers}w)",
                            partial(requester.remove_destinations, relpaths),
                            partial(
                                _drain_cycle,
                                requester,
                                files,
                                candidates,
                                resolver,
                                relpaths,
                                size=size,
                                workers=workers,
                                use_peer=use_peer,
                                recorder=recorder,
                                per_cycle=per_cycle,
                            ),
                            count=count,
                        )
                    )
                    cycles = max(1, len(per_cycle))
                    outcomes.append(
                        (
                            f"{label}_{size_label}({workers}w)",
                            size,
                            round(sum(row[0] for row in per_cycle) / cycles, 1),
                            max(row[1] for row in per_cycle),
                            sum(row[3] for row in per_cycle),
                        )
                    )
    finally:
        failure_patcher.stop()
        outcome_patcher.stop()
    return results, outcomes


def _build_cycle_fixture(
    master: P2PServerHarness,
    seeder: P2PServerHarness,
    resolver: P2PSourceResolver,
    size_label: str,
    size: int,
) -> tuple[list[File], dict[tuple[str, str], tuple[P2PPeerSource, ...]], list[str]]:
    """Materialize one cycle's files on both nodes plus their candidate map."""
    files: list[File] = []
    candidates: dict[tuple[str, str], tuple[P2PPeerSource, ...]] = {}
    relpaths: list[str] = []
    for index in range(CYCLE_FILES):
        relpath = f"cycle/{size_label}/f-{index:02d}.bin"
        content, digest = write_payload(seeder.bucket_dir, relpath, size)
        write_payload(master.bucket_dir, relpath, size)
        file = build_file(relpath, content, digest)
        key = resolver._identity(file)
        assert key is not None, relpath
        files.append(file)
        relpaths.append(relpath)
        candidates[key] = (seeder.peer_source(),)
    return files, candidates, relpaths


def _drain_cycle(
    requester: P2PRequesterEnv,
    files: list[File],
    candidates: dict[tuple[str, str], tuple[P2PPeerSource, ...]],
    resolver: P2PSourceResolver,
    relpaths: list[str],
    *,
    size: int,
    workers: int,
    use_peer: bool,
    recorder: DownloadOutcomeRecorder,
    per_cycle: list[tuple[int, int, int, int]],
) -> None:
    """One timed cycle sample: drain the files and record where they came from."""
    recorder.reset()
    _drain_files(
        requester,
        files,
        candidates,
        resolver,
        workers=workers,
        use_peer=use_peer,
    )
    per_cycle.append(
        (
            recorder.peer_ok,
            recorder.peer_busy,
            recorder.peer_other_failures,
            recorder.master_full,
        )
    )
    for relpath in relpaths:
        target = requester.bucket_dir / relpath
        assert target.is_file() and target.stat().st_size == size, target


def _drain_cycle_on_pool(
    files: list[File],
    worker: Callable[[File], None],
    *,
    pool: ThreadPoolExecutor,
    relay: LatencyRelay,
    recorder: DownloadOutcomeRecorder,
    handshakes: list[int],
    outcomes: list[tuple[int, int]],
) -> None:
    """One cycle on a long-lived pool, recording the handshakes it paid.

    The pool is reused across samples exactly as production reuses its transfer
    executor, so the count is a steady-state number: the relay's
    accepted-connection delta around the cycle is the TLS handshakes it needed.
    """
    recorder.reset()
    before = relay.connections
    list(pool.map(worker, files))
    handshakes.append(relay.connections - before)
    outcomes.append((recorder.peer_ok, recorder.master_full))


def _measure_latency_configuration(
    *,
    label: str,
    delay_ms: int,
    workers: int,
    count: int,
    requester: P2PRequesterEnv,
    files: list[File],
    relpaths: list[str],
    size: int,
    worker: Callable[[File], None],
    relay: LatencyRelay,
    recorder: DownloadOutcomeRecorder,
) -> tuple[BenchResult, tuple[str, int, int, str, float, int, int]]:
    """Measure one configuration (label, injected RTT, worker count) end to end.

    Peer and master phases share this sequence — fresh pool per configuration (kept
    across the samples, exactly as production keeps its transfer executor), timed
    cycles, content assertion — instead of duplicating it per phase.
    """
    handshakes: list[int] = []
    outcomes: list[tuple[int, int]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bench_latency") as pool:
        result = measure_with_setup(
            f"{label}_{delay_ms}ms({len(files)}f,{workers}w)",
            partial(requester.remove_destinations, relpaths),
            partial(
                _drain_cycle_on_pool,
                files,
                worker,
                pool=pool,
                relay=relay,
                recorder=recorder,
                handshakes=handshakes,
                outcomes=outcomes,
            ),
            count=count,
        )
    for relpath in relpaths:
        target = requester.bucket_dir / relpath
        assert target.is_file() and target.stat().st_size == size, target
    summary = (
        f"{label} {delay_ms}ms({workers}w)",
        delay_ms,
        workers,
        ",".join(str(value) for value in handshakes),
        result.p50,
        sum(row[0] for row in outcomes),
        sum(row[1] for row in outcomes),
    )
    return result, summary


def _run_latency_cycles(
    count: int,
    *,
    master: P2PServerHarness,
    seeder: P2PServerHarness,
    requester: P2PRequesterEnv,
) -> tuple[list[BenchResult], list[tuple[str, int, int, str, float, int, int]]]:
    """Handshake cost and bulk parallelism with an injected RTT, on a real pool.

    The size matrix prices one transfer; this prices two decisions. For the peer
    path it answers what worker-to-seeder affinity would have to save (production
    keeps ONE transfer pool, so per-thread sessions stay warm and a cycle pays a
    handshake only at first contact). For the master path it prices bounded *bulk*
    parallelism, which a sequential download loop wastes: small files are
    latency-bound, one request round trip each.

    Returns:
        ``(results, rows)`` where each row is ``(label, delay_ms, workers,
        handshakes_per_cycle, p50_ms, peer_ok, master_full)``. The handshake list
        includes the warmup cycles: its first entry is the coldest one.
    """
    resolver = requester.resolver()
    recorder = DownloadOutcomeRecorder()
    outcome_patcher = install_outcome_recorder(recorder)
    failure_patcher = install_failure_recorder(recorder)
    results: list[BenchResult] = []
    rows: list[tuple[str, int, int, str, float, int, int]] = []
    try:
        files, fixture_candidates, relpaths = _build_cycle_fixture(
            master, seeder, resolver, LATENCY_LABEL, LATENCY_SIZE
        )
        for delay_s in LATENCY_DELAYS_S:
            delay_ms = round(delay_s * 2000.0)
            with LatencyRelay("127.0.0.1", seeder.port, delay_s=delay_s) as relay:
                candidates = {
                    key: (seeder.relayed_peer_source(relay.port),) for key in fixture_candidates
                }
                for workers in LATENCY_WORKERS:
                    result, summary = _measure_latency_configuration(
                        label="latency peer",
                        delay_ms=delay_ms,
                        workers=workers,
                        count=count,
                        requester=requester,
                        files=files,
                        relpaths=relpaths,
                        size=LATENCY_SIZE,
                        worker=partial(
                            _download_one, requester, candidates=candidates, resolver=resolver
                        ),
                        relay=relay,
                        recorder=recorder,
                    )
                    results.append(result)
                    rows.append(summary)
            with LatencyRelay("127.0.0.1", master.port, delay_s=delay_s) as relay:
                requester.retarget_api(f"https://127.0.0.1:{relay.port}")
                try:
                    for workers in LATENCY_MASTER_WORKERS:
                        result, summary = _measure_latency_configuration(
                            label="latency master",
                            delay_ms=delay_ms,
                            workers=workers,
                            count=count,
                            requester=requester,
                            files=files,
                            relpaths=relpaths,
                            size=LATENCY_SIZE,
                            worker=partial(_master_sample, requester),
                            relay=relay,
                            recorder=recorder,
                        )
                        results.append(result)
                        rows.append(summary)
                finally:
                    requester.retarget_api(master.base_url)
    finally:
        failure_patcher.stop()
        outcome_patcher.stop()
    return results, rows


def _mib_per_s(size: int, p50_ms: float) -> float:
    """Effective MiB/s from a body size and a p50 latency in ms (0 if unmeasurable)."""
    if p50_ms <= 0:
        return 0.0
    return size / (p50_ms / 1000.0) / (1024 * 1024)


def _bracket_crossover(
    rows: list[tuple[str, int, float, float, float]],
    peer_index: int,
) -> tuple[int, str, str] | None:
    """First size where the peer p50 drops below the master p50, interpolated.

    ``rows`` entries are ``(label, size, peer_cold_p50, peer_warm_p50,
    master_warm_p50)``; ``peer_index`` selects cold (2) or warm (3).
    """
    for low, high in zip(rows, rows[1:], strict=False):
        low_delta = low[peer_index] - low[4]
        high_delta = high[peer_index] - high[4]
        if low_delta <= 0:
            return low[1], low[0], low[0]
        if high_delta < 0:
            span = low_delta - high_delta
            fraction = low_delta / span if span > 0 else 0.0
            size = int(low[1] + fraction * (high[1] - low[1]))
            return size, low[0], high[0]
    return None


def _print_derived(results_by_name: dict[str, BenchResult]) -> None:
    """Print per-size throughput and the peer/master crossing size.

    The crossing is where the peer line meets the master line: it quantifies the
    fixed setup cost the peer path pays (and therefore what the peer admission
    gate has to amortize), not an eligibility threshold.
    """
    rows = [
        (
            label,
            size,
            results_by_name[_scenario_name(PEER_PREFIX, COLD_INFIX, label)].p50,
            results_by_name[_scenario_name(PEER_PREFIX, WARM_INFIX, label)].p50,
            results_by_name[_scenario_name(MASTER_PREFIX, WARM_INFIX, label)].p50,
        )
        for label, size in SIZES
    ]
    print("derived: p50 per path and effective body throughput (size / p50)")
    header = (
        f"{'size':<8}{'peer_cold_ms':>14}{'peer_warm_ms':>14}{'master_warm_ms':>15}"
        f"{'peer_MiB/s':>12}{'master_MiB/s':>13}"
    )
    print(header)
    print("-" * len(header))
    for label, size, cold, warm, master in rows:
        print(
            f"{label:<8}{cold:>14.3f}{warm:>14.3f}{master:>15.3f}"
            f"{_mib_per_s(size, warm):>12.1f}{_mib_per_s(size, master):>13.1f}"
        )
    print()
    candidates = (
        (f"{PEER_PREFIX}_{WARM_INFIX}", 3),
        (f"{PEER_PREFIX}_{COLD_INFIX}", 2),
    )
    for path_label, peer_index in candidates:
        crossing = _bracket_crossover(rows, peer_index)
        if crossing is None:
            print(
                f"derived: {path_label} never beats {MASTER_PREFIX}_{WARM_INFIX} "
                f"within {SIZES[0][0]}-{SIZES[-1][0]}"
            )
            continue
        size, low_label, high_label = crossing
        if low_label == high_label:
            print(
                f"derived: {path_label} is already at least as fast as "
                f"{MASTER_PREFIX}_{WARM_INFIX} at {low_label} — no crossover inside "
                "the measured range"
            )
            continue
        print(
            f"derived: {path_label} break-even vs {MASTER_PREFIX}_{WARM_INFIX}: "
            f"~{size} bytes (bracket {low_label}-{high_label})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--count",
        type=int,
        default=40,
        help="timed samples per scenario (wire scenarios are far slower than L1)",
    )
    parser.add_argument(
        "--latency",
        action="store_true",
        help="also price the per-cycle TLS handshakes behind an injected RTT "
        "(worker-to-seeder affinity question)",
    )
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count
    if count < 1:
        parser.error("--count must be >= 1")

    # Both servers log one INFO line per served file plus DEBUG request lines;
    # hundreds of samples would bury the results table.
    from loguru import logger

    logger.remove()

    with tempfile.TemporaryDirectory(prefix="hsync_bench_peer_") as tmp:
        root = Path(tmp)
        with (
            P2PServerHarness(P2PServerMode.master, root, name="master", cid=MASTER_CID) as master,
            P2PServerHarness(
                P2PServerMode.seeder,
                root,
                name="seeder",
                cid=SEEDER_CID,
                peer_read_cap=PEER_READ_CAP,
            ) as seeder,
            P2PRequesterEnv(root, master=master, cid=REQUESTER_CID) as requester,
        ):
            results = _run_size_matrix(count, master=master, seeder=seeder, requester=requester)
            cap_results, cap_summary = _run_read_cap_sweep(
                count, seeder=seeder, requester=requester
            )
            results.extend(cap_results)
            results.extend(
                _run_production_fallback(count, master=master, seeder=seeder, requester=requester)
            )
            cycle_results, cycle_outcomes = _run_cycle_scenarios(
                count, master=master, seeder=seeder, requester=requester
            )
            results.extend(cycle_results)
            latency_rows: list[tuple[str, int, int, str, float, int, int]] = []
            if args.latency:
                latency_results, latency_rows = _run_latency_cycles(
                    count, master=master, seeder=seeder, requester=requester
                )
                results.extend(latency_results)

    print(f"P2P peer transfer wire benchmark (count={count}, loopback, real TLS)")
    print_table(results)
    _print_derived({result.name: result for result in results})
    print()
    print(f"seeder read cap (HSYNC_P2P_PEER_READ_CAP={PEER_READ_CAP}) saturation sweep:")
    for workers, served, busy in cap_summary:
        total = served + busy
        print(f"  {workers:>2} concurrent: {served}/{total} served, {busy}/{total} 503 (busy)")
    print()
    print(
        f"cycle outcomes with {CYCLE_FILES} files per cycle (cap={PEER_READ_CAP}, "
        "warmup cycles included):"
    )
    for label, size, avg_peer_ok, worst_busy, total_master in cycle_outcomes:
        print(
            f"  {label:>22} size={size:>7} worst_cycle_503={worst_busy:<4} "
            f"peer_ok/cycle={avg_peer_ok:<7} master_full={total_master}"
        )
    if latency_rows:
        print()
        print(
            "per-cycle TLS handshakes behind an injected RTT "
            "(long-lived pool, warmup cycles included, first value = coldest):"
        )
        for label, delay_ms, workers, per_cycle, p50_ms, peer_ok, master_full in latency_rows:
            print(
                f"  {label:>14} rtt={delay_ms:>2}ms workers={workers} "
                f"p50={p50_ms:>7.1f}ms handshakes/cycle=[{per_cycle}] "
                f"peer_ok={peer_ok} master_full={master_full}"
            )
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
