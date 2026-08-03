# autotune — Adaptive tuning of timeout, retry & concurrency

> Source: `hsync_client/autotune.py:1` · Design: `docs/internal/autotune-design.md`

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
| RTT | `measure_rtt(host, port)` `autotune.py:132` | `float \| None` (ms) | `None` + warning |
| Network class | `classify_network(rtt_ms, is_mobile)` `autotune.py:146` | `NetClass` | `UNKNOWN` |
| Bandwidth (passive) | `update_bw_ema(bytes, elapsed)` `autotune.py:164` | updates `_bw_ema` | ignored on `elapsed<=0` |
| Bandwidth (active) | `measure_bandwidth_active(server_url)` `autotune.py:173` | `float \| None` (Mbps) | `None` + warning |
| Bandwidth class | `classify_bandwidth(mbps, nic, active)` `autotune.py:190` | `BwClass` | `UNKNOWN` |
| Storage type | `detect_storage_type(path)` `autotune.py:213` | `StorageType` | `UNKNOWN` (HDD-class) |
| Queue profile | `profile_transfer_queue(files)` `autotune.py:273` | `QueueProfile` | `MIXED` |

### Module-level state

`_bw_ema` (`autotune.py:123`) is a module-level float updated by
`update_bw_ema()` after each transfer. Callers must read it via the module
attribute (`autotune_mod._bw_ema`), not via a value import, to avoid stale
snapshots.

## Policy (pure functions)

| Function | Location | Returns |
| --- | --- | --- |
| `derive_profile(results)` | `autotune.py:296` | `TuningProfile` |
| `derive_timeout(rtt, net, min, max)` | `autotune.py:346` | `int` |
| `derive_retry(rtt, net, rmin, rmax)` | `autotune.py:361` | `(min, max, reconnect)` |
| `derive_concurrency(storage, bw, queue, cmin, cmax)` | `autotune.py:379` | `int` |

## Integration points

### Startup (`main_logic.run_sync`)

`main_logic.py:779-814` — when autotune is enabled, probes run and
`derive_profile()` produces a `TuningProfile`. Each field is passed as the
`autotune` parameter to the `_get_*_value` resolvers.

### Per sync cycle (`sync._refresh_concurrency_from_queue`)

`sync.py:316-348` — re-derives concurrency from the live transfer queue
profile (zero I/O). Reads `CONFIG["AUTOTUNE_STORAGE_TYPE"]` (set at startup
from `tuning.storage_type.value`) and `autotune_mod._bw_ema`.

### WS reconnect (`remote_websocket.py`)

`remote_websocket.py:288-329` — re-evaluates RTT on reconnect to catch
network changes.

## Resolver chain

```
CLI flag  >  HSYNC_* env var  >  autotuned value  >  static default constant
```

Resolvers in `main_logic.py`:

| Resolver | Location |
| --- | --- |
| `_get_http_timeout_value` | `main_logic.py:291` |
| `_get_reconnect_delay_value` | `main_logic.py:312` |
| `_get_max_concurrent_transfers_value` | `main_logic.py:271` |
| `_get_transfer_retry_min_wait_value` | `main_logic.py:334` |
| `_get_transfer_retry_max_wait_value` | `main_logic.py:346` |

## Kill switch

`--no-autotune` (CLI) / `HSYNC_AUTOTUNE=false` (env) disables auto-tuning
entirely and restores static defaults.

## Tests

`tests/test_autotune.py` — policy unit tests, probe tests with fakes,
precedence tests (CLI > env > autotune > default), integration tests, and
thread-safety tests.
