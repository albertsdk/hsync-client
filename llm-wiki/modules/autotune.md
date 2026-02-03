# autotune — Adaptive tuning of timeout, retry & concurrency

> Source: `hsync_client/autotune.py:1` · Design: `docs/internal/autotune.md`

## Purpose

Environment-aware adaptive defaults for HTTP timeout, retry backoff, and
transfer concurrency. Every probe is independent, returns a neutral value
(`None` / enum fallback) on failure, and never raises into the caller. The
policy layer is pure (no I/O) and fully unit-testable.

## Architecture (3 layers)

1. **Probes** — detect network class, bandwidth, storage type, queue profile.
2. **Policy** — `derive_profile()` converts `ProbeResults` into a
   `TuningProfile` via the stateless `derive_*` functions.
3. **SAFE** — `TuningProfile` with conservative static defaults for when
   auto-tuning is disabled or probes fail.

## Key types

| Type | Location | Notes |
| --- | --- | --- |
| `NetClass` | `autotune.py:42` | `UNKNOWN`, `LAN`, `BROADBAND`, `MOBILE` |
| `BwClass` | `autotune.py:51` | `UNKNOWN`, `LOW`, `MID`, `GIGABIT`, `TEN_G` |
| `StorageType` | `autotune.py:61` | `UNKNOWN`, `HDD`, `SSD`, `NVME` |
| `QueueProfile` | `autotune.py:70` | `MANY_SMALL`, `FEW_LARGE`, `MIXED` |
| `ProbeResults` | `autotune.py:84` | Aggregated probe results (dataclass) |
| `TuningProfile` | `autotune.py:95` | Derived config: `http_timeout`, `retry_min_wait`, `retry_max_wait`, `reconnect_delay`, `concurrency`, `storage_type`, `notes` |
| `SAFE` | `autotune.py:110` | Fallback profile (all safe defaults, `storage_type=UNKNOWN`) |

## Probes

| Probe | Function | Returns | Failure mode |
| --- | --- | --- | --- |
| RTT | `measure_rtt(host, port)` `autotune.py:150` | `float \| None` (ms) | `None` + warning |
| Network class | `classify_network(rtt_ms)` `autotune.py:175` | `NetClass` | `UNKNOWN` |
| Bandwidth (passive) | `update_bw_ema(bytes, elapsed)` `autotune.py:191` | updates `_bw_ema` | ignored on `elapsed<=0` |
| Bandwidth (seed) | `seed_bw_ema(mbps)` `autotune.py:200` | sets `_bw_ema` (if unset) | ignored on `mbps<=0` or already set |
| Bandwidth (active) | `measure_bandwidth_active(server_url, size_bytes)` `autotune.py:216` | `float \| None` (Mbps) | `None` + warning |
| Bandwidth class | `classify_bandwidth(mbps, active)` `autotune.py:261` | `BwClass` | `UNKNOWN` |
| Storage type | `detect_storage_type(path)` `autotune.py:283` | `StorageType` | `UNKNOWN` (HDD-class) |
| Queue profile | `profile_transfer_queue(files)` `autotune.py:343` | `QueueProfile` | `MIXED` |

### Module-level state

`_bw_ema` (`autotune.py:134`) is a module-level float updated by
`update_bw_ema()` after each transfer and seeded by `seed_bw_ema()` from an
active probe when no passive sample exists yet. Callers must read it via the
module attribute (`autotune_mod._bw_ema`), not via a value import, to avoid
stale snapshots.

## Policy (pure functions)

| Function | Location | Returns |
| --- | --- | --- |
| `derive_profile(results)` | `autotune.py:379` | `TuningProfile` |
| `derive_timeout(rtt, net, min, max)` | `autotune.py:428` | `int` |
| `derive_retry(rtt, net, rmin, rmax)` | `autotune.py:443` | `(min, max, reconnect)` |
| `derive_concurrency(storage, bw, queue, cmin, cmax)` | `autotune.py:461` | `int` |

## Integration points

### Startup (`main_logic.run_sync`)

`main_logic.py:835-874` — when autotune is enabled, probes run and
`derive_profile()` produces a `TuningProfile`. Each field is passed as the
`autotune` parameter to the `_get_*_value` resolvers.

**LAN placeholder**: in LAN mode without a configured server URL the startup
RTT probe is skipped (`rtt_ms=None`, `main_logic.py:858`) — there is no server
to measure yet, and probing the placeholder URL would always fail with a
spurious warning. The placeholder URL is also not emitted as a `server` UI
event (`main_logic.py:793`).

### LAN post-discovery refresh (`lan_handler._refresh_lan_network_tuning`)

`lan_handler.py:53`, called from `_run_as_slave` (`lan_handler.py:434`) after
the master is pinned and `set_config(config)` has run. Measures the real RTT
toward the elected master and re-derives `HTTP_TIMEOUT`,
`TRANSFER_RETRY_MIN_WAIT`, `TRANSFER_RETRY_MAX_WAIT` and `RECONNECT_DELAY` via
`derive_timeout`/`derive_retry` (it does not touch `MAX_CONCURRENT_TRANSFERS`).
Only keys listed in `CONFIG["AUTOTUNE_MANAGED_KEYS"]` are touched — keys set
via CLI/env keep their values (provenance tagged by the `_get_*_value`
resolvers themselves, `main_logic.py`; the managed check short-circuits before
any probing).
Updates are written to both the global `CONFIG` and the local config dict
(`set_config` copies values, so the two are distinct objects). No-op when
`AUTOTUNE_ENABLED` is false or the RTT is not measurable.

### Remote per-connection refresh (`remote_websocket._refresh_network_tuning`)

`remote_websocket.py:318`, called from `remote_connect` (`:425`) at the top of
its reconnect loop, **before every `sync()`** — including the first. Re-derives
`HTTP_TIMEOUT`, `TRANSFER_RETRY_MIN_WAIT`, `TRANSFER_RETRY_MAX_WAIT` and
`RECONNECT_DELAY` from a fresh `measure_rtt`, so a cold start whose startup
probes failed (server not yet reachable) still re-tunes once the server is up,
instead of keeping the startup safe defaults for the whole session. Like the
LAN refresh it only rewrites keys in `CONFIG["AUTOTUNE_MANAGED_KEYS"]`
(CLI/env overrides survive), and a failed/unmeasurable probe keeps the current
values rather than resetting to SAFE. When `MAX_CONCURRENT_TRANSFERS` is
managed and `_bw_ema` is still empty (startup active probe failed), it also
re-seeds the EMA from a fresh `measure_bandwidth_active` (offloaded to a
worker thread via `asyncio.to_thread`), so the per-cycle concurrency refresh
has a bandwidth class before the first transfer completes. No-op when
`AUTOTUNE_ENABLED` is false.

### Per sync cycle (`sync._refresh_concurrency_from_queue`)

`sync.py` — re-derives concurrency from the live transfer queue profile
(zero I/O). Reads `CONFIG["AUTOTUNE_STORAGE_TYPE"]` (set at startup from
`tuning.storage_type.value`) and `autotune_mod._bw_ema`. No-op unless
`AUTOTUNE_ENABLED` and `MAX_CONCURRENT_TRANSFERS ∈ AUTOTUNE_MANAGED_KEYS`:
an explicit `--max-concurrent-transfers` / `HSYNC_MAX_CONCURRENT_TRANSFERS`
survives the per-cycle refresh, preserving CLI > env > autotune > default.

### WS reconnect (`remote_websocket.py`)

`remote_websocket.py:288-329` — re-evaluates RTT on reconnect to catch
network changes.

## Resolver chain

```
CLI flag  >  HSYNC_* env var  >  autotuned value  >  static default constant
```

Resolvers in `main_logic.py` (all five return `(value, provenance)`; provenance
∈ `cli`/`env`/`autotune`/`default` feeds `AUTOTUNE_MANAGED_KEYS`):

| Resolver | Location | Returns |
| --- | --- | --- |
| `_get_http_timeout_value` | `main_logic.py:295` | `(int, TuningSource)` |
| `_get_reconnect_delay_value` | `main_logic.py:316` | `(int, TuningSource)` |
| `_get_max_concurrent_transfers_value` | `main_logic.py:277` | `(int, TuningSource)` |
| `_get_transfer_retry_min_wait_value` | `main_logic.py:335` | `(float, TuningSource)` |
| `_get_transfer_retry_max_wait_value` | `main_logic.py:351` | `(float, TuningSource)` |

## Kill switch

`--no-autotune` (CLI) / `HSYNC_AUTOTUNE=false` (env) disables auto-tuning
entirely and restores static defaults.

## Tests

`tests/test_autotune.py` — policy unit tests, probe tests with fakes,
precedence tests (CLI > env > autotune > default), integration tests, and
thread-safety tests.

`tests/test_lan_network_tuning.py` — LAN refresh (`_refresh_lan_network_tuning`
derivation, managed-keys precedence, dual-write, no-op paths), slave call
ordering (pin → refresh), and `run_sync` LAN-placeholder behaviour (probe
skipped, no placeholder `server` event, managed-keys provenance).
