# Performance Reference

Measured throughput of HSYNC by operating mode, workload size and sync phase.
This page is a **reference**: it states what the current implementation achieves
and under which conditions. For how these numbers are produced (harness design,
methodology, measurement pitfalls), see the
[Stress Test Plan](internal/stress-test-plan.md) and the
[Test Suite Guide](internal/testing.md).

> **Terminology:** in this page **LAN** denotes the p2p lane measured over mDNS
> on the reference host. p2p mode also supports the bootnode discovery plan,
> which reuses the same data plane (its cost is discovery/relay, not the transfer
> path measured below).

> Figures are medians over the runs noted in each table, on the reference host
> described below.
> They are reproducible with the stress harness, but absolute numbers are
> host-specific: use them to set expectations and compare configurations, not
> as guarantees.

## Reference conditions

| Item | Value |
| --- | --- |
| Host | single Linux workstation, 16 cores (client and venv on ecryptfs, harness trees and sync state under `/tmp`, tmpfs) |
| Runtime | CPython 3.14.4 (GIL build), both peers on the same host for LAN |
| Client version | current `main` at the measurement date (2026-09-26) |
| Server-mode tree | 2000 files / 300 dirs, mixed size distribution capped at 512 KiB (`HSYNC_BB_STRESS_MAXFILESIZE=524288`, ~70 KiB average) |
| p2p-mode tree | 6000 files / 400 dirs, default mixed distribution with 8 MiB outliers (`HSYNC_BB_STRESS_FILES=6000 HSYNC_BB_STRESS_DIRS=400`, ~255 KiB average) |
| Phases | *initial burst* = first sync of an empty replica; *mutation wave* = all files re-written while realtime sync is connected |

## Server Mode

2000 files / 300 dirs (~137 MiB), client ↔ central server over HTTPS on
localhost. Medians of 3 runs:

| Phase | t_sync | Throughput | Bandwidth |
| --- | --- | --- | --- |
| Initial burst (autotuned concurrency) | 8.5 s | **236 files/s** | 16.9 MiB/s |
| Mutation wave (realtime events) | 2.6 s | **782 files/s** | 56.8 MiB/s |
| Initial burst, `--max-concurrent-transfers=1` | 12.9 s | 156 files/s | 10.8 MiB/s |
| Initial burst, `--max-concurrent-transfers=4` | 8.7 s | 231 files/s | 16.0 MiB/s |
| Initial burst, `--max-concurrent-transfers=8` | 7.8 s | 255 files/s | 17.8 MiB/s |

Readings:

- Server Mode reconciles from a listing, and its transfers **scale with
  concurrency**: raising `--max-concurrent-transfers` from 1 to 8 raises the
  burst throughput from 156 to 255 files/s.
- The realtime mutation wave is the fastest path in the product (782 files/s):
  after the initial sync, only changed files are transferred and the event
  pipeline skips the full listing round trip.

## p2p Mode (peer-to-peer)

6000 files / 400 dirs (~1.5 GiB), master and slave on the same host, master
elected with `--prefer-master`, replica connected over the pinned WSS+HTTPS
channel. Medians of 6 runs (two sessions; t_sync varies with host state across
sessions):

| Phase | t_sync | Throughput | Bandwidth |
| --- | --- | --- | --- |
| Initial burst (event-driven) | 52.9 s | **113 files/s** | 29.4 MiB/s |
| Mutation wave (realtime events) | 34.1 s | **176 files/s** | 47.2 MiB/s |
| Initial burst, full listing reconcile¹ | 13.9 s | 180 files/s | — |

¹ Measured on a 2500-file (200-dir) probe where the replica starts *after* the
tree is generated, so convergence is driven by the initial listing instead of
the realtime event stream.

Known scaling characteristics:

- **The event-driven burst is CPU-bound, not I/O-bound.** Per transferred file
  the replica spends a large share of a core (validation, hashing, TLS,
  serialization), and the Python side serialises on one core (GIL). The
  practical ceiling is `1 core / CPU-per-file` ≈ 90-150 files/s on this class
  of host — the observed burst (~113 files/s) sits inside it — and **raising
  `--max-concurrent-transfers` does not increase it** (measured flat between 5
  and 20).
- **The listing-driven reconcile does benefit from concurrency** (see the probe
  row above and the server sweep), so a full first sync drains faster than a
  same-size realtime wave suggests from event-path numbers alone.
- Peer-to-peer offloading (downloading from another peer instead of the master)
  adds overhead on the burst path: disabling it with `--no-peer-download`
  raises burst throughput by **~44 %** in the same-session A/B probe (188 vs
  130 files/s at the full 6000-file scale).

## Cross-mode comparison — read with care

The two stress configurations are **not** apples-to-apples:

- Server Mode: 2000 small files (~70 KiB average), reconcile transfers driven by
  a listing with parallel uploads.
- p2p Mode: 6000 files (~255 KiB average), one WSS event plus one pinned HTTPS
  download per file, both processes sharing the same host's cores.

With that caveat, the observed ordering is: server initial burst (236 files/s)
≳ LAN mutation wave (176) > LAN event burst (113). The realtime server path
(782 files/s) is the fastest of all; the initial-sync bursts are bounded by
per-file Python work and, in p2p mode, by the single-core GIL ceiling.

## What limits throughput today

| Factor | Effect |
| --- | --- |
| GIL (CPython build) | per-file Python work serialises on ~1 core; dominates the LAN event path |
| Per-file validation & hashing | ~55 lstat/stat calls + lexical validation per file (security semantics, not removable as-is) |
| `--max-concurrent-transfers` | helps reconcile/listing transfers (156 → 255 files/s in the 1-8 sweep); no effect on the LAN event burst |
| Peer offloading on the burst path | costs ~44 % of burst throughput when enabled (188 vs 130 files/s, same-session A/B) |
| TLS/hashing | release the GIL partially; contribute but are not the bottleneck |

Changes to the p2p trust model or sync engine may move these numbers.
To re-run the stress see
[Stress Test Plan](internal/stress-test-plan.md) for the exact commands.
