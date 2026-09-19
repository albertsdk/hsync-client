# Performance Reference

Measured throughput of HSYNC by operating mode, workload size and sync phase.
This page is a **reference**: it states what the current implementation achieves
and under which conditions. For how these numbers are produced (harness design,
methodology, measurement pitfalls), see the
[Stress Test Plan](internal/stress-test-plan.md) and the
[Test Suite Guide](internal/testing.md).

> All figures are medians of 3 runs on the reference host described below.
> They are reproducible with the stress harness, but absolute numbers are
> host-specific: use them to set expectations and compare configurations, not
> as guarantees.

## Reference conditions

| Item | Value |
| --- | --- |
| Host | single Linux workstation, 16 cores, ext4 (state on ecryptfs) |
| Runtime | CPython 3.14.4 (GIL build), both peers on the same host for LAN |
| Client version | current `main` at measurement date (2026-09) |
| Server-mode tree | 2000 files / 300 dirs, random sizes up to 512 KiB (~70 KiB average) |
| LAN-mode tree | 6000 files / 400 dirs, random sizes up to 512 KiB (~255 KiB average) |
| Phases | *initial burst* = first sync of an empty replica; *mutation wave* = all files re-written while realtime sync is connected |

## Server Mode

2000 files / 300 dirs (~137 MiB), client ↔ central server over HTTPS on
localhost. Medians of 3 runs:

| Phase | t_sync | Throughput | Bandwidth |
| --- | --- | --- | --- |
| Initial burst (autotuned concurrency) | 9.3 s | **214 files/s** | 14.5 MiB/s |
| Mutation wave (realtime events) | 3.1 s | **647 files/s** | 48.8 MiB/s |
| Initial burst, `--max-concurrent-transfers=1` | 26.6 s | 75 files/s | 4.9 MiB/s |
| Initial burst, `--max-concurrent-transfers=4` | 11.7 s | 171 files/s | 11.6 MiB/s |
| Initial burst, `--max-concurrent-transfers=8` | 9.5 s | 211 files/s | 14.0 MiB/s |

Readings:

- Server Mode reconciles from a listing, and its transfers **scale with
  concurrency**: raising `--max-concurrent-transfers` from 1 to 8 triples the
  burst throughput (75 → 211 files/s).
- The realtime mutation wave is the fastest path in the product (647 files/s):
  after the initial sync, only changed files are transferred and the event
  pipeline skips the full listing round trip.

## LAN Mode (peer-to-peer)

6000 files / 400 dirs (~1.5 GiB), master and slave on the same host, master
elected with `--force-master`, replica connected over the pinned WSS+HTTPS
channel. Medians of 3 runs:

| Phase | t_sync | Throughput | Bandwidth |
| --- | --- | --- | --- |
| Initial burst (event-driven) | 49.7 s | **121 files/s** | 30.3 MiB/s |
| Mutation wave (realtime events) | 26.8 s | **224 files/s** | 56.7 MiB/s |
| Initial burst, full listing reconcile¹ | ~21 s | ~115-145 files/s | — |

¹ Measured on a 2500-file probe where the replica starts *after* the tree is
generated, so convergence is driven by the initial listing instead of the
realtime event stream (observed download concurrency: peak 5, mean ~3).

Known scaling characteristics:

- **The event-driven burst is CPU-bound, not I/O-bound.** Per transferred file
  the replica spends ~11-15 ms of CPU (validation, hashing, TLS, serialization),
  and the Python side serialises on one core (GIL). The practical ceiling is
  therefore `1 core / CPU-per-file` ≈ 90-150 files/s on this class of host, and
  **raising `--max-concurrent-transfers` does not increase it** (measured flat
  between 5 and 20).
- **The listing-driven reconcile does benefit from concurrency** (see the probe
  row above and the server sweep), so a full first sync drains faster than a
  same-size realtime wave suggests from event-path numbers alone.
- Peer-to-peer offloading (downloading from another peer instead of the master)
  currently adds overhead on the burst path: disabling it with
  `--no-peer-download` was measured at **+38 %** burst throughput in the same
  probe (101 vs 73 files/s at the time of measurement).

## LAN Mode before/after the current tuning (history)

For continuity with earlier measurements, the LAN initial burst at the documented
full scale moved from **74 files/s / t_sync 80.7 s** to **121 files/s /
t_sync 49.7 s** after removing two per-file wastes (environment-proxy resolution
on pinned sessions and discarded stdlib log records). Details and the isolated
per-waste costs are in the [Test Suite Guide](internal/testing.md). Cross-run
comparisons at that scale carry a ±15 % host-state spread; treat direction as
confirmed and magnitude as approximate.

## Cross-mode comparison — read with care

The two stress configurations are **not** apples-to-apples:

- Server Mode: 2000 small files (~70 KiB average), reconcile transfers driven by
  a listing with parallel uploads.
- LAN Mode: 6000 files (~255 KiB average), one WSS event plus one pinned HTTPS
  download per file, both processes sharing the same host's cores.

With that caveat, the observed ordering is: LAN mutation wave (224 files/s) ≳
server initial burst (214) ≫ LAN event burst (121) > LAN event burst at
concurrency 1 (75). The realtime paths of both modes are the fast paths; the
initial-sync burst is bounded by per-file Python work and, in LAN mode, by the
single-core GIL ceiling.

## What limits throughput today

| Factor | Effect |
| --- | --- |
| GIL (CPython build) | per-file Python work serialises on ~1 core; dominates the LAN event path |
| Per-file validation & hashing | ~55 lstat/stat calls + lexical validation per file (security semantics, not removable as-is) |
| `--max-concurrent-transfers` | helps reconcile/listing transfers; no effect on the LAN event burst |
| Peer offloading on the burst path | currently costs ~38 % of burst throughput when enabled |
| TLS/hashing | release the GIL partially; contribute but are not the bottleneck |

Changes to the LAN trust model or sync engine may move these numbers.
To re-run the stress see
[Stress Test Plan](internal/stress-test-plan.md) for the exact commands.
