# Auto-Tuning — Developer Guide

> **Source**: `hsync_client/autotune.py` · **Wiki**: `llm-wiki/modules/autotune.md`

## Overview

The auto-tuning subsystem derives adaptive defaults for HTTP timeout, retry
backoff, and transfer concurrency from the runtime environment. It is enabled
by default and can be disabled with `--no-autotune` or `HSYNC_AUTOTUNE=false`.

## Architecture

Three decoupled layers:

1. **Probes** — environment detection. Each probe is independent, returns a
   neutral value (`None` / enum fallback) on failure, and never raises.
2. **Policy** — `derive_profile()` converts `ProbeResults` into a
   `TuningProfile` via stateless `derive_*` functions (pure, no I/O).
3. **Integration** — `main_logic.run_sync` (startup) and
   `sync._refresh_concurrency_from_queue` (per-cycle) consume the profile.

## Probes

### Network latency (RTT)

`measure_rtt(host, port)` — median of 3 TCP-connect samples, 2 s per-sample
timeout. Returns `None` on failure.

`classify_network(rtt_ms)` maps RTT to `NetClass`:

| RTT | Class |
| --- | --- |
| `None` | `UNKNOWN` |
| < 50 ms | `LAN` |
| 50–150 ms | `BROADBAND` |
| > 150 ms | `MOBILE` |

### Bandwidth

**Passive (default)**: `update_bw_ema(bytes, elapsed)` maintains a module-level
EMA (`_bw_ema`) updated after each transfer. Callers must read it via the
module attribute (`autotune_mod._bw_ema`), not via a value import, to avoid
stale snapshots.

**Active (startup fallback)**: `measure_bandwidth_active(server_url, size_bytes)`
GETs `/health?size=<size_bytes>` (default 256 KB, server-capped at 256 KB) and
measures throughput from the returned payload. It runs **only at startup**,
when the passive EMA has no data yet (`_bw_ema is None`) and a real server URL
is configured (skipped for the LAN placeholder).

`classify_bandwidth(mbps, active)` maps to `BwClass`:

| Mbps | Class |
| --- | --- |
| `None` | `UNKNOWN` |
| ≥ 2000 Mbps | `TEN_G` |
| ≥ 800 Mbps | `GIGABIT` |
| ≥ 50 Mbps | `MID` |
| < 50 Mbps | `LOW` |

### Storage type

`detect_storage_type(path)` resolves the sync root to its block device via
`/proc/self/mountinfo`, then reads `/sys/class/block/<dev>/queue/rotational`.
NVMe is detected by `nvme*` prefix. Device-mapper/LVM is resolved via
`/sys/class/block/dm-*/slaves/`. Any error → `UNKNOWN` (treated as HDD-class).

**Scope**: only the sync root (`LOCAL_BUCKET`) is probed, never the system disk.

**Container caveat**: inside a container the device→sysfs mapping often breaks
(bind mounts, overlay). The probe fails → `UNKNOWN` → conservative defaults.

### Transfer-queue profile

`profile_transfer_queue(files)` classifies the current download/upload batch:

| Condition | Profile |
| --- | --- |
| ≥ 100 files, ≥ 70% < 1 MB | `MANY_SMALL` |
| ≤ 20 files, ≥ 50% > 256 MB | `FEW_LARGE` |
| otherwise | `MIXED` |

Re-evaluated each sync cycle (zero I/O cost).

## Policy (derivation)

`derive_profile(ProbeResults) -> TuningProfile` calls:

- `derive_timeout(rtt, net_class, min, max)` → 30/45/60 s
- `derive_retry(rtt, net_class, rmin, rmax)` → `(min_wait, max_wait, reconnect)`
- `derive_concurrency(storage, bw, queue, cmin, cmax)` → 5–20

### Timeout

| NetClass | Timeout |
| --- | --- |
| `UNKNOWN` | 30 s (SAFE) |
| `MOBILE` | 60 s |
| `BROADBAND` (RTT > 50) | 45 s |
| `LAN` / fast broadband | 30 s |

### Retry

When RTT is available: `factor = min(1 + rtt/100, 3)`, then
`min_wait = clamp(round(5 * factor), 1, 30)`, `max_wait = clamp(min_wait * 2, 1, 60)`,
`reconnect = clamp(round(5 * factor), rmin, rmax)`.

When RTT is `None` and `MOBILE`: `(10, 20, 10)`.
When RTT is `None` and `UNKNOWN`: SAFE defaults `(5, 10, 5)`.

### Concurrency

Base by storage type: HDD=5, SSD=10, NVME=15, UNKNOWN=5.

Adjustments:
- `TEN_G` bandwidth + SSD/NVMe → +5
- `LOW` bandwidth → cap at 4
- `MANY_SMALL` queue → +3
- `FEW_LARGE` queue on HDD → −2

Clamped to `[1, 20]`.

## Integration points

### Startup (`main_logic.run_sync`)

1. Probes run (RTT, bandwidth, storage).
2. `derive_profile()` produces a `TuningProfile`.
3. Each field is passed as `autotune` to the `_get_*_value` resolvers.
4. `CONFIG["AUTOTUNE_STORAGE_TYPE"]` is cached from `tuning.storage_type.value`
   for the per-cycle refresh.

**p2p mode**: without a configured server URL the client runs on a placeholder
URL until a master is discovered, so the startup RTT probe is skipped
(`rtt_ms=None` → SAFE values) instead of failing against the placeholder, and
the placeholder URL is not emitted as a `server` UI event.

### LAN post-discovery refresh (`p2p_handler._refresh_p2p_network_tuning`)

In p2p mode the real network endpoint is known only after master election.
Once the master is discovered and pinned, `_run_as_slave` measures the RTT
toward it and re-derives the timeout/retry/reconnect values **before** the
clock-offset probe, so every subsequent HTTP call uses the tuned timeout.

Only *autotune-managed* keys are updated: `CONFIG["AUTOTUNE_MANAGED_KEYS"]`
(computed at startup) lists the tuning keys that were resolved from
autotune/default — anything set explicitly via CLI flag or `HSYNC_*` env var
keeps its value, preserving the resolver-chain precedence. The refresh is a
no-op when auto-tuning is disabled or the RTT is not measurable.

### Per sync cycle (`sync._refresh_concurrency_from_queue`)

Re-derives concurrency from the live queue profile (zero I/O). Reads
`CONFIG["AUTOTUNE_STORAGE_TYPE"]` and `autotune_mod._bw_ema`. It is a no-op
unless `AUTOTUNE_ENABLED` is true **and** `MAX_CONCURRENT_TRANSFERS` is in
`AUTOTUNE_MANAGED_KEYS` — an explicit `--max-concurrent-transfers` /
`HSYNC_MAX_CONCURRENT_TRANSFERS` is preserved across cycles, so the per-cycle
refresh only refines a value that autotune (or the default) already set.

### WS reconnect (`remote_websocket.py`)

`remote_connect` re-evaluates RTT at the top of its loop — before every
`sync()`, including the first — so the first sync after a cold start (where
the startup probes failed because the server was not yet reachable) also runs
on a freshly measured RTT instead of the startup safe defaults. Each reconnect
likewise re-measures RTT to catch network changes (e.g. laptop moved from
Ethernet to Wi-Fi). When the passive bandwidth EMA is still empty (startup
active probe failed) and concurrency is autotune-managed, the same refresh
re-runs the active bandwidth probe and seeds the EMA, so the per-cycle
concurrency refresh gets a bandwidth class before the first transfer completes.

## Resolver chain

```
CLI flag  >  HSYNC_* env var  >  autotuned value  >  static default constant
```

Resolvers in `main_logic.py`:

| Resolver | Parameter | Returns |
| --- | --- | --- |
| `_get_http_timeout_value` | `http_timeout` | `(value, provenance)` |
| `_get_reconnect_delay_value` | `reconnect_delay` | `(value, provenance)` |
| `_get_max_concurrent_transfers_value` | `max_concurrent_transfers` | `(value, provenance)` |
| `_get_transfer_retry_min_wait_value` | `transfer_retry_min_wait` | `(value, provenance)` |
| `_get_transfer_retry_max_wait_value` | `transfer_retry_max_wait` | `(value, provenance)` |

The provenance tag (`cli` / `env` / `autotune` / `default`) is the single
encode site for the precedence chain: `AUTOTUNE_MANAGED_KEYS` is built from
the tags of all five tuning keys (only `autotune`/`default`-sourced keys are
managed), so explicit CLI/env overrides are never re-derived — by the LAN
post-discovery refresh nor the per-cycle concurrency refresh.

`TRANSFER_RETRY_ATTEMPTS` is always `3` (no env/CLI override in v1).

## Kill switch

`--no-autotune` (CLI) / `HSYNC_AUTOTUNE=false` (env) disables auto-tuning
entirely. All parameters fall back to static defaults.

## SAFE fallback

`SAFE` is a `TuningProfile` with conservative defaults (30 s timeout, 5/10 s
retry, 5 s reconnect, 5 concurrency, `StorageType.UNKNOWN`). Used when
auto-tuning is disabled or all probes fail.

## Tuned parameters summary

| Parameter | Static default | Auto-tuned range |
| --- | --- | --- |
| HTTP timeout | 30 s | 30 / 45 / 60 s |
| Retry min wait | 1 s | 5–15 s |
| Retry max wait | 10 s | 10–30 s |
| Reconnect delay | 5 s | 5–15 s |
| Concurrency | 5 | 5–20 |

## Testing

`tests/test_autotune.py` covers:
- Policy unit tests (full matrix, boundaries, clamping)
- Probe tests with fakes (socket stubs, sysfs fixtures, permission errors)
- Precedence tests (CLI > env > autotune > default)
- Integration tests (storage_type propagation, notes content, live _bw_ema)
- Thread-safety tests
