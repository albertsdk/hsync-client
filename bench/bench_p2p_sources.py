# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""L1/L2 — P2P source lookup benchmark (eligibility filter, batching, wire lookup).

Measures the requester-side half of ADR-0028 against a real master on loopback:

* ``P2PSourceResolver._identity`` on the eligible / malformed branches — the
  gate that turns a listed file into a lookup; size is not part of it, so a file
  of any size is peer-eligible when a live seeder holds its exact hash;
* ``P2PSourceResolver._source_batches`` over 10 / 100 / 500 / 1000 identities
  (the protocol maximum is 500 per request), which shows how much of the lookup
  cost is amortized for many small files;
* ``P2PSourceResolver.resolve`` — the real ``POST /files/sources`` round trip
  against a master with a live (empty) holder index. Candidate lists stay empty
  on purpose: populating them requires live WSS publisher sessions, which is a
  master-side state machine, not a requester cost;
* ``P2PSourceTelemetry.rank`` / ``record_success`` — the per-file, per-peer
  requester bookkeeping the peer path adds on top of the transfer.

Usage:
    cd hsync-client && .venv/bin/python bench/bench_p2p_sources.py [--count N] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from functools import partial
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
from p2p_peer_common import (  # noqa: E402
    BENCH_BUCKET,
    P2PRequesterEnv,
    P2PServerHarness,
    env_override,
)

from hsync_client.p2p_inventory import P2PInventoryStore  # noqa: E402
from hsync_client.p2p_server import P2PServerMode  # noqa: E402
from hsync_client.p2p_sources import (  # noqa: E402
    P2PPeerSource,
    P2PSourceResolver,
    P2PSourceService,
    P2PSourceTelemetry,
)

MASTER_CID = make_cid(201)
REQUESTER_CID = make_cid(202)

# Protocol maximum identities per request (``P2P_SOURCES_MAX_FILES``); 1000 forces
# the splitter to emit two requests.
BATCH_SIZES = (10, 100, 500, 1000)
EXPECTED_BATCHES = {10: 1, 100: 1, 500: 1, 1000: 2}
WIRE_BATCH_SIZES = (100, 500)
# A wire scenario is ~10^4 slower than an in-process one: cap its sample count.
WIRE_SAMPLE_CAP = 50

SMALL_SIZE = 4 * 1024
# A file far below the retired 1 MiB threshold, kept to show the gate no longer
# depends on size.
TINY_SIZE = 1024
# Valid lowercase SHA-256 hex; identical hashes at distinct paths are valid on
# the wire, which keeps the fixture small without weakening the request shape.
DIGEST = "a" * 64
TELEMETRY_SOURCES = 4


def _no_peers() -> tuple[()]:
    """Empty peer snapshot: a bench has no live WSS publisher sessions."""
    return ()


def _files(count: int, size: int = SMALL_SIZE) -> list[File]:
    """``count`` distinct bucket-relative identities sharing one digest."""
    return [
        File(filepath=f"data/f-{index:04d}.bin", size=size, sha256sum=DIGEST)
        for index in range(count)
    ]


def _identities(resolver: P2PSourceResolver, files: list[File]) -> list[tuple[str, str]]:
    """Production eligibility filter over *files* (asserts every file is kept)."""
    identities = []
    for file in files:
        key = resolver._identity(file)
        assert key is not None, file.filepath
        identities.append(key)
    return identities


def _check_batches(
    resolver: P2PSourceResolver,
    identities: list[tuple[str, str]],
    expected_batches: int,
    expected_identities: int,
) -> None:
    """Assert the splitter keeps every identity and honours the wire batch size."""
    batches = resolver._source_batches(identities)
    assert len(batches) == expected_batches, len(batches)
    assert sum(len(batch) for batch in batches) == expected_identities


def _check_lookup(resolver: P2PSourceResolver, files: list[File], expected: int) -> None:
    """Assert one wire lookup returned a strict, identity-preserving response."""
    resolved = resolver.resolve(files)
    assert len(resolved) == expected, len(resolved)
    assert all(not sources for sources in resolved.values())


def _run_identity_scenarios(count: int, requester: P2PRequesterEnv) -> list[BenchResult]:
    """The eligibility gate: size no longer routes a file to the master."""
    tiny = _files(1, TINY_SIZE)[0]
    small = _files(1)[0]
    malformed = _files(1)[0]
    malformed.sha256sum = "not-a-sha256"
    resolver = requester.resolver()
    key = resolver._identity(small)
    assert key == ("data/f-0000.bin", DIGEST), key
    assert resolver._identity(tiny) == key

    def op_tiny() -> None:
        assert resolver._identity(tiny) == key

    def op_small() -> None:
        assert resolver._identity(small) == key

    def op_malformed() -> None:
        assert resolver._identity(malformed) is None

    return [
        measure("identity(eligible_1KiB)", op_tiny, count=count),
        measure("identity(eligible_4KiB)", op_small, count=count),
        measure("identity(rejected_bad_hash)", op_malformed, count=count),
    ]


def _run_batch_scenarios(count: int, requester: P2PRequesterEnv) -> list[BenchResult]:
    """Request-splitting cost: one lookup request per up to 500 identities."""
    resolver = requester.resolver()
    results: list[BenchResult] = []
    for size in BATCH_SIZES:
        identities = _identities(resolver, _files(size))
        results.append(
            measure(
                f"batch_split({size})",
                partial(_check_batches, resolver, identities, EXPECTED_BATCHES[size], size),
                count=count,
            )
        )
    return results


def _run_wire_lookup_scenarios(wire_count: int, requester: P2PRequesterEnv) -> list[BenchResult]:
    """Real ``POST /files/sources`` round trips against the loopback master."""
    resolver = requester.resolver()
    results: list[BenchResult] = []
    for size in WIRE_BATCH_SIZES:
        files = _files(size)
        results.append(
            measure(
                f"source_lookup({size})",
                partial(_check_lookup, resolver, files, size),
                count=wire_count,
            )
        )
    return results


def _run_telemetry_scenarios(count: int) -> list[BenchResult]:
    """Requester-side ranking/bookkeeping cost per file and per peer CID."""
    telemetry = P2PSourceTelemetry()
    sources = tuple(
        P2PPeerSource(
            cid=make_cid(300 + index),
            download_url=(
                f"https://127.0.0.1:{8443 + index}/api/v1/buckets/{BENCH_BUCKET}/files/download"
            ),
            cert_fingerprint="b" * 64,
            compression=False,
            serve_slots=2,
        )
        for index in range(TELEMETRY_SOURCES)
    )
    probe = P2PPeerSource(
        cid=make_cid(400),
        download_url=sources[0].download_url,
        cert_fingerprint="b" * 64,
        compression=False,
        serve_slots=2,
    )

    def op_rank() -> None:
        assert len(telemetry.rank(sources)) == TELEMETRY_SOURCES

    def op_record_success() -> None:
        telemetry.record_success(
            probe.cid,
            rtt_seconds=0.002,
            bytes_transferred=SMALL_SIZE,
            body_seconds=0.002,
        )
        assert telemetry._metrics[probe.cid].rtt_ema is not None

    return [
        measure("telemetry_rank(4)", op_rank, count=count),
        measure("telemetry_record_success", op_record_success, count=count),
    ]


def _print_derived(results_by_name: dict[str, BenchResult]) -> None:
    """Print the per-identity lookup cost implied by the wire scenarios."""
    print("derived: source lookup cost per reported identity")
    for size in WIRE_BATCH_SIZES:
        result = results_by_name[f"source_lookup({size})"]
        print(
            f"  batch {size:>4}: p50 {result.p50:.3f} ms -> "
            f"{result.p50 / size * 1000:.1f} us/identity"
        )
    print(
        f"  note: size no longer gates eligibility: a {TINY_SIZE}-byte and a "
        f"{SMALL_SIZE}-byte file pass the same filter, so small-file bursts keep "
        f"the peer path available."
    )
    print(
        "  note: 500 identities per request is the protocol maximum; a file whose exact "
        "(path, sha256) has no indexed holder comes back with an empty source list."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count
    if count < 1:
        parser.error("--count must be >= 1")
    wire_count = min(count, WIRE_SAMPLE_CAP)

    # The loopback master logs per request; silence the sink so the table is readable.
    from loguru import logger

    logger.remove()

    with tempfile.TemporaryDirectory(prefix="hsync_bench_sources_") as tmp:
        root = Path(tmp)
        (root / "state").mkdir(parents=True, exist_ok=True)
        # The master's holder index resolves through ``default_device_dir()``
        # (HSYNC_DEVICE_DIR), not the state dir: override both, or the bench would
        # create a real ``lan-index`` SQLite in the user's device directory.
        with (
            env_override("HSYNC_DEVICE_DIR", str(root / "inventory-device")),
            env_override("HSYNC_STATE_DIR", str(root / "state")),
        ):
            inventory = P2PInventoryStore.for_bucket(BENCH_BUCKET)
            inventory.activate()
        try:
            service = P2PSourceService(
                bucket=BENCH_BUCKET,
                master_cid=MASTER_CID,
                inventory_store=inventory,
                peer_snapshot_provider=_no_peers,
            )
            with (
                P2PServerHarness(
                    P2PServerMode.master,
                    root,
                    name="master",
                    cid=MASTER_CID,
                    source_service=service,
                ) as master,
                P2PRequesterEnv(root, master=master, cid=REQUESTER_CID) as requester,
            ):
                results = _run_identity_scenarios(count, requester)
                results.extend(_run_batch_scenarios(count, requester))
                results.extend(_run_wire_lookup_scenarios(wire_count, requester))
                results.extend(_run_telemetry_scenarios(count))
        finally:
            inventory.close()

    print(f"P2P source lookup benchmark (count={count}, wire_count={wire_count}, loopback master)")
    print_table(results)
    _print_derived({result.name: result for result in results})
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
