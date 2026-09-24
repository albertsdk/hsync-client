# HSYNC Client — Benchmarks (L1 + L2 peer path)

Internal micro-benchmark suite for the client-specific hot paths. Developer
tooling: plain scripts under `bench/`, never collected by pytest, never run in
CI, no user-facing behaviour.

## Why two tiers (today)

`hsync-client` is a sync daemon with a transport (p2p master/slave +
server REST). This directory ships:

* **L1 in-process micro-benchmarks** — the shared library primitives are already
  covered by `hsync-common/bench`, so here we measure only what the client adds
  on top of `hsync_common` (the wrappers in `sync_state`, `tombstones`,
  `arbitration`, `pinning`, `event_queue`, `autotune`, `utils`);
* **L2 loopback wire benchmark of the p2p peer-download path** (ADR-0028/0030) —
  a real `P2PServer` seeder and master on loopback with real self-signed TLS,
  driven through the production `download_file` entry point. Added to size the
  peer path's fixed cost: the question it answers cannot be answered by mocks,
  because the cost under test is TLS handshake, keep-alive reuse, TOFU pin reads
  and the seeder read cap. Its cycle scenario is what admitted or rejected the
  admission designs and retired the size-threshold heuristic.

The full-fleet load + soak tier (spawn the `hsync` binary as a multi-node fleet,
wire-only) is still deferred, as are L4/L5. L4/L5 are added only when a concrete
anomaly needs a cost breakdown / CPU saturation sweep.

Out of scope by design: `scripts/benchmark_p2p_transfers.py` (not in this
directory) measures the master's *raw* HTTP overhead with `http.client`
(keep-alive, TCP_NODELAY, compression throughput) — it never enters the client
transfer path. The L2 scripts here measure the opposite end: the production
requester (`download_file`, `P2PSourceResolver`, telemetry) and the peer seeder.

## Files

| Path | Role |
| --- | --- |
| `bench/README.md` | Tier table, per-script scope, run commands, methodology |
| `bench/common.py` | Stdlib-only measurement core (mean/p50/p99/stdev/min/max) + deterministic CIDs; never imports `hsync_client` |
| `bench/bench_autotune.py` | `hsync_client/autotune.py` policy scenarios |
| `bench/bench_utils.py` | `hsync_client/utils.py` per-request/path helper scenarios |
| `bench/bench_event_queue.py` | `hsync_client/event_queue.py` outbound-event scenarios |
| `bench/bench_pinning.py` | `hsync_client/pinning.py` TOFU store scenarios |
| `bench/bench_sync_state.py` | `hsync_client/sync_state.py` wrapper scenarios |
| `bench/bench_tombstones.py` | `hsync_client/tombstones.py` wrapper scenarios |
| `bench/bench_arbitration.py` | `hsync_client/arbitration.py` upload-gate scenarios |
| `bench/bench_reconcile.py` | `hsync_client/sync.py` reconcile-core scenarios (decision leaves + bulk `get_sync_actions`) |
| `bench/bench_p2p_peer_transfer.py` | **L2** p2p peer-download wire scenarios: peer cold/warm vs master warm per size, seeder read-cap sweep, concurrent production fallback, cycle acceptance, and (`--latency`) the per-cycle TLS-handshake question behind an injected RTT |
| `bench/bench_p2p_sources.py` | **L2** source-lookup scenarios: eligibility filter, batch splitting, wire `POST /files/sources`, telemetry rank/record |
| `bench/bench_startup.py` | **Startup** — wall-clock of every fast `hsync` invocation (`--version`, `--capabilities-json`, `--help`, `--help-symbols`) plus `import hsync_client.cli` / `import hsync_client.main_logic` and the two mode stacks (`import hsync_client.p2p_handler` / `import hsync_client.remote_websocket`), from source and from the packaged `--onefile`/`--onedir` binary; standalone (spawns processes), compared with `compare.py` |
| `bench/p2p_peer_common.py` | Shared L2 fixtures: loopback seeder/master harness (`P2PServer` + temp cert + bucket), requester env (CONFIG/state dir/pin file/session cache), `measure_with_setup`, `LatencyRelay` (unprivileged RTT injection that counts TLS handshakes) |
| `bench/run_all.py` | Chained runner → `bench_result.txt` (committed baseline, **L1 scripts only**); `--json-out-dir` also dumps one `<script>.json` per scenario |
| `bench/compare.py` | A/B comparator over two `--json-out` runs (file or dir); flags median regressions above a noise floor, `exit 1` on regression |

## Scenario → code anchors

| Scenario | Public API measured |
| --- | --- |
| `classify_network(mixed)` / `classify_bandwidth(mixed)` | `autotune.classify_network` (`hsync_client/autotune.py:179`), `classify_bandwidth` (`:265`) |
| `derive_timeout(lan\|unknown)` | `autotune.derive_timeout` (`:460`) |
| `derive_retry(lan)` | `autotune.derive_retry` (`:475`) |
| `derive_concurrency(ssd,mid)` | `autotune.derive_concurrency` (`:493`) |
| `derive_profile` | `autotune.derive_profile` (`:383`) |
| `profile_queue(many_small\|few_large\|mixed)` | `autotune.profile_transfer_queue` (`:347`) |
| `http_to_ws(http\|https)` | `utils.http_to_ws` (`hsync_client/utils.py:84`) |
| `redact_headers(auth\|plain)` | `utils.redact_headers` (`:105`) — log-secret masking gate |
| `request_headers` | `utils.request_headers` (`:73`) |
| `inbound_size_cap(declared\|capped\|unknown)` | `utils.inbound_size_cap` (`:550`) — decompression-bomb bound |
| `http_connect_timeout` / `upload_timeout` | `utils.http_connect_timeout` (`:95`), `upload_timeout` (`:100`) |
| `is_same_paths` / `ensure_str_path` | `utils.is_same_paths` (`:347`), `ensure_str_path` (`:393`) |
| `normalize_bucket_relpath(in\|out)` | `utils.normalize_bucket_relpath` (`:418`) — wire-path gate before upload |
| `get_backoff_delay(mixed)` | `event_queue.QueuedEvent.get_backoff_delay` (`hsync_client/event_queue.py:103`) |
| `enqueue` | `event_queue.EventQueue.enqueue` (`:149`) |
| `send_event(success\|failure\|unserializable)` | `event_queue.EventQueue._send_event` (`:160`) — `json.dumps` + awaited send; transient raise → held for retry (`False`); permanent drop → `True` without touching the socket |
| `PinStore.get(hit\|miss)` / `check_and_record(match\|changed\|first_use)` / `set(repin)` | `pinning.PinStore` (`hsync_client/pinning.py:83`,`:98`,`:88`) |
| `get_sync_state_store(cached)` | `sync_state.get_sync_state_store` (`hsync_client/sync_state.py:50`) |
| `get_base_hash(hit)` | `sync_state.get_base_hash` (`:69`) |
| `record_base` | `sync_state.record_base` (`:78`) — includes `os.stat` corrected-mtime fallback |
| `upload_version_fields(same\|ticked)` | `sync_state.upload_version_fields` (`:283`) → `local_version_for_compare` (`:164`) |
| `local_version_and_base(same\|ticked)` | `sync_state.local_version_and_base` (`:175`) — effective version + stored base in one store read |
| `merge_and_record` | `sync_state.merge_and_record` (`:317`) — convergence merge (ADR-0022 §5) |
| `flush` | `sync_state.flush` (`:145`) — once-per-cycle SQLite save |
| `get_tombstone_store(cached)` / `get_payload` | `tombstones.get_tombstone_store` (`hsync_client/tombstones.py:53`), `get_payload` (`:174`) |
| `record_deletion(persist)` | `tombstones.record_deletion` (`:74`) — realtime, flushes per deletion |
| `record_deletion_by_key(persist=False)` / `flush` | `tombstones.record_deletion_by_key` (`:103`), `flush` (`:140`) — deferred mint + batched persist |
| `mint_50(memory)` / `mint_50_then_flush` | offline-deletion mint loop end-to-end (`record_deletion_by_key(persist=False)` × 50 + one `flush`) |
| `merge_payload(dominant)` | `tombstones.merge_payload` (`:164`) — causal merge + persist |
| `arbitrate_upload(first\|dominant\|superseded\|conflict)` | `arbitration.arbitrate_upload` (`hsync_client/arbitration.py:69`) — client store/cid binding over `resolve_three_way` |
| `conflicted_copy_name` | `arbitration.conflicted_copy_name` (`:180`) — ADR-0021 loser-path builder |
| `compare_files(in_sync\|download\|upload\|conflict_*)` | `sync._compare_files` (`hsync_client/sync.py:1721`) — per-file causal decision over `resolve_three_way` |
| `tombstone_clock_dominates(dom\|sub\|invalid)` | `sync._tombstone_clock_dominates` (`:1328`) — deletion proof via `compare_vclocks` |
| `tombstone_deletes_local(delete\|keep)` | `sync._tombstone_deletes_local` (`:1345`) |
| `parse_advertised_version(valid\|absent\|malformed)` | `sync._parse_advertised_version` (`:1358`) — wire `version` decode |
| `get_sync_actions(100 in_sync)` | `sync.get_sync_actions` (`:1466`, `persist_state=False`) — full steady-state diff (divisibile per 100 per il costo per-file) |
| `identity(eligible_1KiB\|eligible_4KiB\|rejected_bad_hash)` | `p2p_sources.P2PSourceResolver._identity` (`hsync_client/p2p_sources.py:573`) — the lookup gate; size is deliberately not part of it (ADR-0030), so a 1 KiB and a 4 KiB file take the same branch |
| `batch_split(10\|100\|500\|1000)` | `p2p_sources.P2PSourceResolver._source_batches` (`:474`) — identity, byte-budget and 500-per-request splitting |
| `source_lookup(100\|500)` | `p2p_sources.P2PSourceResolver.resolve` (`:522`) against a live master (`p2p_server._handle_sources`, `hsync_client/p2p_server.py:649`) |
| `telemetry_rank(4)` / `telemetry_record_success` | `p2p_sources.P2PSourceTelemetry.rank` (`:330`), `record_success` (`:358`) |
| `peer_cold_*` / `peer_warm_*` / `master_warm_*` | `transfer.download_file` (`hsync_client/transfer.py:471`) with a real seeder (`transfer._download_file_attempt`, `:1104`) vs the master fallback |
| `peer_warm_4KiB_no_bookkeeping` | same, with `transfer._record_synced_base` patched out, to size the per-file sync-state write |
| `seeder_readcap_batch(1\|2\|4\|8)` | `p2p_server` seeder route + read-cap semaphore (`P2PServer(peer_read_cap=…)`, `hsync_client/p2p_server.py:518-527`) |
| `peer_cap2_fallback(4 files)` | production `download_file` under the same cap: peer `503` → failure record → master fallback |
| `latency_peer_{0\|40}ms(24f,2\|8w)` | the production peer path driven through ONE long-lived `ThreadPoolExecutor`, behind `p2p_peer_common.LatencyRelay` (counts accepted connections = TLS handshakes): the worker-to-seeder affinity question |
| `latency_master_{0\|40}ms(24f,1\|4w)` | the production master path (API retargeted through the relay) on the same pool shape: prices bounded bulk parallelism against the sequential loop, the mobile sync's concurrency decision |

## Methodology

Same as `hsync-common/bench` (warmup outside samples, setup outside the timed
region, per-branch assertions inside the measured op, one
`TemporaryDirectory`-isolated fixture per scenario, deterministic counter-based
CIDs). State-dependent scenarios set a `HSYNC_STATE_DIR` override + a seeded
`CONFIG` and clear the module store caches. Every result reports
`mean`/`p50`/`p99` plus `stdev`/`min`/`max`. Results are comparative (this
machine, warm page cache), never production SLAs.

L2 additions: `bench/p2p_peer_common.py::measure_with_setup` runs per-sample setup
(dropping the destination, evicting this thread's HTTP session) outside the timed
region; the harness patches the server-role `CONFIG` of `hsync_client.p2p_server`
so one process hosts both a seeder and a master with separate buckets, exactly as
two processes would; `HSYNC_DEVICE_DIR` / `HSYNC_STATE_DIR` / `HSYNC_P2P_PIN_FILE`
are pointed at the temp fixture so no real device identity, sync-state DB or pin
file is touched, and every patch/env override is restored on exit. A wire sample
costs 1–30 ms, so wire scenarios are capped (`--count` default 40; lookups capped
at 50) and the L2 scripts stay out of `run_all.py` / `bench_result.txt`.

`compare.py` flags an operation only when its median grew beyond `--threshold`
(default 1.10) **and** its before-run mean sat at or above `--min-mean-ms`
(default 0.001 ms): the floor keeps the sub-microsecond ops (where
`perf_counter` resolution alone moves the numbers by tens of percent) from
producing false positives. A scenario present in `before` but missing from
`after` is always flagged.

## Initial findings (baseline `bench_result.txt`, count=2000, python 3.14.4)

* **Pure policy and helpers are sub-microsecond** — `classify_*`, `derive_*`,
  `redact_headers`, `inbound_size_cap`, timeouts: ~0.0001–0.0007 ms; the
  per-cycle `derive_profile` is ~0.0024 ms and `profile_transfer_queue` is
  ~0.0005 ms (few-large) / ~0.0040 ms (many-small — the linear scan over 120
  entries is the visible cost).
* **Folder resolution memoized on the raw bucket path (candidate A, fixed)** —
  the wrapper used to re-run `resolve_bucket_folder` (`.hsyncfolder` read +
  `get_state_db` folder SELECT **and** an `INSERT … ON CONFLICT DO UPDATE`
  upsert) on every per-file call even when the store was already cached.
  `resolve_bucket_folder` now memoizes the `(folder_id, db_path)` result keyed
  by the raw `bucket_dir` string (`hsync_client/state_backend.py`), so the
  steady-state call is a bare dict lookup (no `Path.resolve()` syscall, no
  `HSYNC_STATE_DIR` env read). `get_sync_state_store(cached)` dropped
  **~0.0554 ms → ~0.0003 ms (~185x)** and the whole column followed:
  `get_base_hash` ~0.0614→0.0020, `record_base` ~0.0642→0.0030,
  `arbitrate_upload` ~0.07→0.006, `get_tombstone_store` ~0.0559→0.0003,
  `record_deletion` ~0.241→0.108, `merge_payload` ~0.220→0.094. Re-anchor
  semantics (move/copy/delete-marker) are unchanged, pinned by
  `tests/test_state_backend.py`; the cache is invalidated by
  `reset_bucket_folder_cache()` (wired into the `reset_sync_state_stores`
  autouse fixture).
* **TOFU pinning is flock+fsync dominated** — `get(hit/miss)` and
  `check_and_record(match/changed)` all sit ~0.016 ms (advisory flock + JSON
  load; the trust decision itself adds nothing measurable). The durable paths
  are an order of magnitude up: `set(repin)` ~0.075 ms and
  `check_and_record(first_use)` ~0.12 ms — both pay the atomic 0o600
  temp-write + fsync + replace, the latter also the verification read-back.
  The first-use scenario grows a **separate** pin file so its 200 fresh pins
  never inflate the JSON that `set`/`get`/`check_and_record` re-read.
* **Event send: failure is marginally costlier, still µs-scale** —
  `send_event(success)` ~0.0017 ms vs `send_event(failure)` ~0.0020 ms (the
  transient raise caught → held for retry) and `send_event(unserializable)`
  ~0.0023 ms (`json.dumps` raises `TypeError` → permanent drop); serialization
  is not the event path's hot spot — the backoff hold it triggers is.
* **Batched offline-deletion mint (candidate B, fixed)** — the sync-engine mint
  loop used `record_deletion_by_key` (persist per marker) for every file deleted
  while offline. `record_deletion_by_key` now takes `persist=False`
  (in-memory record) and the loop calls `tombstones.flush()` once after minting
  (`sync.py`), so a bulk mint pays one SQLite write instead of one per marker:
  `mint_50(memory)` ~0.078 ms, `mint_50_then_flush` ~0.425 ms vs the old
  50 × `record_deletion(persist)` ≈ 50 × 0.108 ms = ~5.4 ms (**~12.7x**; the
  A-followup also removed the 50 per-marker store resolves from the inner loop).
  Realtime deletions still persist immediately (`record_deletion`, ~0.108 ms), so
  a crash cannot lose a live watcher marker; the batched path is re-derivable on
  the next cycle from the persisted sync-state. Pinned by
  `test_record_by_key_batched_flush_persists`.
* **Arbitration branch-insensitive** — all four `arbitrate_upload` branches sit
  around ~0.006 ms (after A): the store read + `resolve_three_way` cost
  dominates, the outcome (accept/supersede/conflict) adds nothing measurable.

## Reconcile core (`bench_reconcile.py`) — what the new coverage found

* **The per-file decision logic is sub-microsecond** — `_compare_files` costs
  ~0.0002–0.0003 ms on the in-sync/download/upload branches (it routes straight
  through `resolve_three_way`); the conflict branches are ~0.0011–0.0014 ms
  (the corrected-mtime tie-break plus an **eager `log.debug` f-string** in the
  conflict-download branch, `sync.py:1770` — a candidate lazy-logging fix). The
  tombstone and `parse_advertised_version` helpers are ~0.0001–0.0018 ms.
* **The full steady-state reconcile is ~2.27 ms / 100 files ≈ ~23 µs/file**
  (`get_sync_actions(persist_state=False)` over an in-sync listing): the
  decision layer is a rounding error inside it. The remaining per-file cost is
  `validate_remote_relpath` + `File.from_dict` + `File.get` (`os.stat` + warm
  SHA-256 cache hit) + one `sync_state` read.
* **Double `sync_state` read folded (candidate D, fixed)** — the reconcile used
  `local_version_for_compare` *then* `get_base_hash` per file (two relpath
  resolutions + two lock acquisitions + two reads of the same store entry). A
  single `sync_state.local_version_and_base` (`sync_state.py:175`) now returns
  both the effective version and the stored base hash in one read;
  `get_sync_actions` and `update_file` use it (`sync.py`).
  `local_version_and_base(same)` costs ~0.0022 ms vs the old pair
  (`local_version_for_compare` ~0.0035 + `get_base_hash` ~0.0020 = ~0.0055 ms),
  and `get_sync_actions(100 in_sync)` dropped ~24.6 → ~22.7 µs/file.

## p2p peer download (`bench_p2p_peer_transfer.py`, `bench_p2p_sources.py`) — what the L2 coverage found

Run: `bench_p2p_peer_transfer.py --count 40` and `bench_p2p_sources.py --count 2000`,
loopback, real `P2PServer` seeder (cap 2) + master, self-signed TLS, one Python
3.14 process (both roles share the CPU — a loopback figure is a comparative
lower bound, not a Wi-Fi/LAN prediction).

* **The peer path's cost is a fixed connection cost, not a per-byte cost.** With a
  cold session the peer takes ~13–27 ms against 1.4–4.9 ms warm across the sizes:
  the TCP+TLS handshake + pin read dominate, so `peer_cold` never beats the warm
  master up to 8 MiB. `peer_warm` and `master_warm` stay within ~1.2 ms of each
  other at every size (4 KiB ~1.7 vs ~1.5 ms, 64 KiB ~1.9 vs ~1.6, 256 KiB ~2.3 vs
  ~2.1, 1 MiB ~3.7–4.9 vs ~3.7, 8 MiB ~19.0 vs ~19.4) and the sign flips between
  runs: on loopback the two paths are latency-equivalent once the session is warm.
  The interpolated "break-even" the script prints reflects that — it moved between
  ~4 KB and ~6.7 MB across runs, i.e. the crossover is noise-dominated, so the
  conclusion rests on the per-size p50 comparison, not on a single threshold value.
  The retired 1 MiB eligibility default was therefore **not** justified by the
  warm path's cost (its real effect was load distribution, which loopback cannot
  measure); ADR-0030 removed it in favour of advertised seeder capacity.
* **The per-file eligibility filter itself is free** (`_identity` ~2.8 µs on every
  branch) and so is telemetry (`rank(4)` ~1.8 µs, `record_success` ~0.5 µs): the
  gate's cost is entirely the transfer-side connection behaviour behind it, not
  the bookkeeping it skips. `bench_p2p_sources.py` now measures the eligible
  1 KiB and 4 KiB branches plus the malformed one, so the scenario states the
  current size-independent policy instead of a threshold that no longer exists.
* **The 4 KiB bookkeeping share is real but small**: patching out
  `_record_synced_base` moves `peer_warm_4KiB` from ~1.7–1.9 ms to ~1.27 ms
  (per-file sync-state write ≈ 0.5–0.6 ms); both paths pay it, so it does not
  change the ranking.
* **Source lookup is amortized by batching** (32-40 µs per identity: 100
  identities ≈ 4.0 ms, 500 ≈ 16.0 ms per request) and needs no per-file request
  at all — 5,000 small files ≈ 10 requests ≈ 160 ms total. Lookup cost can never
  justify a size threshold.
* **The real hazard for small files is the seeder read cap.** With
  `HSYNC_P2P_PEER_READ_CAP=2`, 1 and 2 concurrent requester workers get 40/40
  served and **0** 503s, but 4 workers produce ~20/40 and 8 workers ~24–26/40
  `503 busy` (p99 latency inflates by 1–2 orders of magnitude, ~0.1–1 s). Each 503
  used to cost `record_failure` → exponential cooldown (up to 60 s) → master
  fallback; ADR-0030 turned it into a `Retry-After`-bounded deferral and had
  seeders advertise their capacity, so the cap now bounds polite sharing instead
  of punishing a healthy seeder.
* **The cycle scenario is the acceptance test for admission** (24 files per cycle,
  cap 2, peer path against a master-only baseline): after ADR-0030 every
  configuration — 1 KiB / 4 KiB / 256 KiB at 4 and 8 workers — reports
  `worst_cycle_503=0`, `master_full=0` and **24.0 of 24 files served by the peer**,
  against up to 4 refusals and 23 of 168 master-served downloads before it.
* **Waiting for a slot beats declining it** (the rejected alternative, same
  scenario): non-blocking admission served 6.7 of 24 files from the peer and
  diverted 121 of 168 full downloads to the master, recreating the
  small-file-burst symptom the peer path exists to remove. Loopback wall time
  cannot separate the two variants (both roles share one CPU), so the decision
  rested on the distribution counters, not on elapsed time.

### Worker-to-seeder affinity: measured and rejected (`--latency`)

24 × 4 KiB per cycle, cap 2, the production download path driven through **one
long-lived pool** (`hsync_client.sync` reuses its executor for the whole run), a
transparent relay in front of the seeder counting every accepted connection, i.e.
every TLS handshake. 40 ms is the one-way delay per forwarded chunk, so a
request/response pair costs one realistic Wi-Fi RTT.

| Configuration | p50 per cycle | handshakes per cycle | peer_ok | master_full |
| --- | --- | --- | --- | --- |
| 0 ms RTT, 2 workers | 621.4 ms | `[2,0,0,0,0,0,0]` | 168 | 0 |
| 0 ms RTT, 8 workers | 325.4 ms | `[8,0,0,0,0,0,0]` | 168 | 0 |
| 40 ms RTT, 2 workers | 1066.6 ms | `[2,0,0,0,0,0,0]` | 168 | 0 |
| 40 ms RTT, 8 workers | 815.6 ms | `[8,0,0,0,0,0,0]` | 168 | 0 |

Warmup cycles included; the first value is the coldest cycle. Seven cycles each
(1 warmup + 6 timed) × 24 files = 168 peer downloads per configuration.

* **A cycle pays handshakes only at first contact, then zero.** The number of
  workers sets the one-time cost (2 vs 8, one connection per worker thread) and
  after that every cycle reuses the existing connections: `utils._http_session_cache`
  is per-thread (`threading.local()`), so each worker owns a warm keep-alive
  session and the process keeps it for the whole role lifetime.
* **Grouping downloads per seeder would lose more than it saves.** Forcing a
  cycle down to the advertised slot count (2 workers) is exactly what affinity
  would do, and under a 40 ms RTT it costs 1066.6 ms per cycle against 815.6 ms
  with 8 workers: affinity buys 6 fewer *one-time* handshakes and surrenders
  ~250 ms of parallelism per cycle, forever. The admission already bounds what
  matters (in-flight reads per seeder), so over-subscribing workers is strictly
  better than matching them to slots.
* **The RTT cost that does exist is the per-file request round trip**, not the
  handshake: 325–621 ms per 24-file cycle at 0 ms RTT against 815–1066 ms at
  40 ms. No download-scheduling change in `sync.py` can remove it; it would need
  request batching, which is a different feature (and would not help the file
  sizes the peer path is for).
* This is why the worker-to-seeder affinity phase of the ADR-0030 rollout was
  closed **on measurement**, with no scheduling change to `sync.py`.

### Bounded bulk parallelism: measured (`--latency`, master path)

The same relay and long-lived-pool shape in front of the **master** prices the
other concurrency decision, the one the mobile client needed: its download loop
was strictly sequential, and small files are latency-bound — one request round
trip each, so a cycle's wall time is `files × RTT` with no overlap at all.

| Configuration | p50 per cycle | speed-up | handshakes per cycle |
| --- | --- | --- | --- |
| 0 ms RTT, 1 worker (sequential) | 938.3 ms | — | `[1,0,0,0,0,0,0]` |
| 0 ms RTT, 4 workers | 307.0 ms | 3.1× | `[4,0,0,0,0,0,0]` |
| 40 ms RTT, 1 worker (sequential) | 2023.1 ms | — | `[1,0,0,0,0,0,0]` |
| 40 ms RTT, 4 workers | 560.6 ms | 3.6× | `[4,0,0,0,0,0,0]` |

168 master-served files per configuration (7 cycles × 24 files), peer path not
involved. The speed-up grows with the injected RTT, because the cost being hidden
is round-trip latency rather than bandwidth, and it saturates near the worker
count: past that point extra concurrency buys sockets and radio time, not
throughput. That is why the Android bulk bound is four and not a function of the
file count.

## Running

```bash
cd hsync-client
.venv/bin/python bench/run_all.py --count 2000            # -> bench_result.txt
.venv/bin/python bench/bench_autotune.py --count 2000 [--json-out FILE]
# ... (one script per module; see bench/README.md)

# L2 wire benchmark (standalone: not in run_all.py, baseline untouched)
.venv/bin/python bench/bench_p2p_peer_transfer.py --count 40 [--json-out FILE]
.venv/bin/python bench/bench_p2p_peer_transfer.py --count 6 --latency   # + handshake/RTT scenario
.venv/bin/python bench/bench_p2p_sources.py --count 2000 [--json-out FILE]

# startup benchmark (standalone: spawns processes, depends on binary paths)
.venv/bin/python bench/bench_startup.py --count 30 --no-binary [--json-out FILE]
.venv/bin/python bench/bench_startup.py --count 30 --binary dist/linux-amd64/hsync [--json-out FILE]

# A/B regression check between two full-suite JSON dirs
.venv/bin/python bench/run_all.py --count 2000 --json-out-dir /tmp/before
.venv/bin/python bench/run_all.py --count 2000 --json-out-dir /tmp/after
.venv/bin/python bench/compare.py /tmp/before /tmp/after --threshold 1.10
```

Lint: `cd hsync-client && .venv/bin/python -m ruff check bench` — the directory
is covered by the project ruff config, so the Quality Gate applies to `bench/`
too.
