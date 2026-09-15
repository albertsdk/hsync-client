# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""L1 — autotune policy micro-benchmark (pure, no I/O).

The policy layer of :mod:`hsync_client.autotune` converts environment probes
into the adaptive timeout/retry/concurrency values used every sync cycle. It is
deliberately pure (the probes perform the I/O; the ``derive_*``/``classify_*``
functions do not) so it is fully micro-benchmarkable:

* ``classify_network`` / ``classify_bandwidth`` — the per-probe classification
  gates (RTT → net class, Mbps → bandwidth class);
* ``derive_timeout`` / ``derive_retry`` / ``derive_concurrency`` — the per-cycle
  policy derivations, clamped to the configured bounds;
* ``derive_profile`` — the end-to-end ``ProbeResults`` → ``TuningProfile``
  conversion (imports the constants only, still no I/O);
* ``profile_transfer_queue`` — the single-pass queue-shape classifier that runs
  over the file list of every sync cycle.

Measured ops assert the returned enum/int so a silent fallback or a dropped
branch cannot be reported as fast.

Usage:
    cd hsync-client && .venv/bin/python bench/bench_autotune.py [--count N] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow ``from common import ...`` when run from the bench/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    maybe_write_json,
    measure,
    print_table,
)
from hsync_common.models.file import File  # noqa: E402

from hsync_client.autotune import (  # noqa: E402
    SAFE,
    BwClass,
    NetClass,
    ProbeResults,
    QueueProfile,
    StorageType,
    TuningProfile,
    classify_bandwidth,
    classify_network,
    derive_concurrency,
    derive_profile,
    derive_retry,
    derive_timeout,
    profile_transfer_queue,
)
from hsync_client.constants import (  # noqa: E402
    HTTP_TIMEOUT_MAX,
    HTTP_TIMEOUT_MIN,
    MAX_CONCURRENT_TRANSFERS_MAX,
    MAX_CONCURRENT_TRANSFERS_MIN,
    RECONNECT_DELAY_MAX,
    RECONNECT_DELAY_MIN,
)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _file(size: int, index: int) -> File:
    return File(
        filepath=f"/srv/hsync/bucket/file_{index:06d}.dat",
        mtime=1_700_000_000,
        size=size,
        sha256sum="a" * 64,
        permissions="644",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    rtts: list[float | None] = [None, 10.0, 60.0, 200.0]
    rtt_expected = [NetClass.UNKNOWN, NetClass.LAN, NetClass.BROADBAND, NetClass.MOBILE]
    assert [classify_network(r) for r in rtts] == rtt_expected

    mbps: list[float | None] = [None, 10.0, 100.0, 1000.0, 3000.0]
    bw_expected = [
        BwClass.UNKNOWN,
        BwClass.LOW,
        BwClass.MID,
        BwClass.GIGABIT,
        BwClass.TEN_G,
    ]
    assert [classify_bandwidth(m) for m in mbps] == bw_expected

    rtt_state = {"i": 0}
    mbps_state = {"i": 0}

    def op_classify_network() -> None:
        i = rtt_state["i"] % len(rtts)
        rtt_state["i"] += 1
        assert classify_network(rtts[i]) is rtt_expected[i]

    def op_classify_bandwidth() -> None:
        i = mbps_state["i"] % len(mbps)
        mbps_state["i"] += 1
        assert classify_bandwidth(mbps[i]) is bw_expected[i]

    def op_derive_timeout() -> None:
        out = derive_timeout(10.0, NetClass.LAN, HTTP_TIMEOUT_MIN, HTTP_TIMEOUT_MAX)
        assert out == _clamp(30, HTTP_TIMEOUT_MIN, HTTP_TIMEOUT_MAX)

    def op_derive_timeout_unknown() -> None:
        out = derive_timeout(None, NetClass.UNKNOWN, HTTP_TIMEOUT_MIN, HTTP_TIMEOUT_MAX)
        assert out == SAFE.http_timeout

    def op_derive_retry() -> None:
        out = derive_retry(10.0, NetClass.LAN, RECONNECT_DELAY_MIN, RECONNECT_DELAY_MAX)
        assert isinstance(out, tuple) and len(out) == 3
        assert all(isinstance(v, int) for v in out)

    def op_derive_concurrency() -> None:
        out = derive_concurrency(
            StorageType.SSD,
            BwClass.MID,
            QueueProfile.MIXED,
            MAX_CONCURRENT_TRANSFERS_MIN,
            MAX_CONCURRENT_TRANSFERS_MAX,
        )
        assert MAX_CONCURRENT_TRANSFERS_MIN <= out <= MAX_CONCURRENT_TRANSFERS_MAX

    profile_results = ProbeResults(
        rtt_ms=10.0,
        net_class=NetClass.LAN,
        bw_class=BwClass.MID,
        storage_type=StorageType.SSD,
        queue_profile=QueueProfile.MIXED,
    )

    def op_derive_profile() -> None:
        out = derive_profile(profile_results)
        assert isinstance(out, TuningProfile)
        assert out.http_timeout > 0 and out.concurrency > 0

    many_small = [_file(100_000, i) for i in range(120)]
    few_large = [_file(300_000_000, i) for i in range(10)]
    mixed = [_file(100_000 if i % 2 == 0 else 300_000_000, i) for i in range(60)]
    assert profile_transfer_queue(many_small) is QueueProfile.MANY_SMALL
    assert profile_transfer_queue(few_large) is QueueProfile.FEW_LARGE
    assert profile_transfer_queue(mixed) is QueueProfile.MIXED

    def op_profile_many_small() -> None:
        assert profile_transfer_queue(many_small) is QueueProfile.MANY_SMALL

    def op_profile_few_large() -> None:
        assert profile_transfer_queue(few_large) is QueueProfile.FEW_LARGE

    def op_profile_mixed() -> None:
        assert profile_transfer_queue(mixed) is QueueProfile.MIXED

    results: list[BenchResult] = [
        measure("classify_network(mixed)", op_classify_network, count=count),
        measure("classify_bandwidth(mixed)", op_classify_bandwidth, count=count),
        measure("derive_timeout(lan)", op_derive_timeout, count=count),
        measure("derive_timeout(unknown)", op_derive_timeout_unknown, count=count),
        measure("derive_retry(lan)", op_derive_retry, count=count),
        measure("derive_concurrency(ssd,mid)", op_derive_concurrency, count=count),
        measure("derive_profile", op_derive_profile, count=count),
        measure("profile_queue(many_small)", op_profile_many_small, count=count),
        measure("profile_queue(few_large)", op_profile_few_large, count=count),
        measure("profile_queue(mixed)", op_profile_mixed, count=count),
    ]

    print(f"autotune policy micro-benchmark (ops={count}, pure CPU, no I/O)")
    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
