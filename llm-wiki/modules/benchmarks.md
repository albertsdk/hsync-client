# HSYNC Client — Benchmarks (L1)

Internal micro-benchmark suite for the client-specific hot paths. Developer
tooling: plain scripts under `bench/`, never collected by pytest, never run in
CI, no user-facing behaviour.

## Why one tier (today)

`hsync-client` is a sync daemon with a transport (LAN P2P master/slave +
server REST), but this directory ships only the **L1 in-process
micro-benchmarks**: the shared library primitives are already covered by
`hsync-common/bench`, so here we measure only what the client adds on top of
`hsync_common` (the wrappers in `sync_state`, `tombstones`, `arbitration`,
`pinning`, `event_queue`, `autotune`, `utils`).

L2/L3 (spawn the `hsync` binary over a loopback LAN master/slave pair,
wire-only) is deferred until this baseline is reviewed — mirroring the bootnode
tier split, but gated on the L1 numbers first. L4/L5 are added only when a
concrete anomaly needs a cost breakdown / CPU saturation sweep.

## Files

| Path | Role |
| --- | --- |
| `bench/README.md` | Tier table, per-script scope, run commands, methodology |
| `bench/common.py` | Stdlib-only measurement core + deterministic CIDs; never imports `hsync_client` |
| `bench/bench_autotune.py` | `hsync_client/autotune.py` policy scenarios |
| `bench/bench_utils.py` | `hsync_client/utils.py` per-request/path helper scenarios |
| `bench/bench_event_queue.py` | `hsync_client/event_queue.py` outbound-event scenarios |
| `bench/bench_pinning.py` | `hsync_client/pinning.py` TOFU store scenarios |
| `bench/bench_sync_state.py` | `hsync_client/sync_state.py` wrapper scenarios |
| `bench/bench_tombstones.py` | `hsync_client/tombstones.py` wrapper scenarios |
| `bench/bench_arbitration.py` | `hsync_client/arbitration.py` upload-gate scenarios |
| `bench/bench_reconcile.py` | `hsync_client/sync.py` reconcile-core scenarios (decision leaves + bulk `get_sync_actions`) |
| `bench/bench_startup.py` | **Startup** — wall-clock of every fast `hsync` invocation (`--version`, `--capabilities-json`, `--help`, `--help-symbols`) plus `import hsync_client.cli` / `import hsync_client.main_logic`, from source and from the packaged `--onefile`/`--onedir` binary; standalone (spawns processes), compared with `compare.py` |
| `bench/run_all.py` | Chained runner → `bench_result.txt` (committed baseline) |

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
| `send_event(success)` | `event_queue.EventQueue._send_event` (`:160`) — `json.dumps` + awaited send |
| `PinStore.get(hit)` / `check_and_record(match)` / `set(repin)` | `pinning.PinStore` (`hsync_client/pinning.py:185`,`:200`,`:190`) |
| `get_sync_state_store(cached)` | `sync_state.get_sync_state_store` (`hsync_client/sync_state.py:50`) |
| `get_base_hash(hit)` | `sync_state.get_base_hash` (`:69`) |
| `record_base` | `sync_state.record_base` (`:78`) — includes `os.stat` corrected-mtime fallback |
| `upload_version_fields(same\|ticked)` | `sync_state.upload_version_fields` (`:283`) → `local_version_for_compare` (`:164`) |
| `local_version_and_base(same\|ticked)` | `sync_state.local_version_and_base` (`:175`) — effective version + stored base in one store read |
| `merge_and_record` | `sync_state.merge_and_record` (`:317`) — convergence merge D11 |
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

## Methodology

Same as `hsync-common/bench` (warmup outside samples, setup outside the timed
region, per-branch assertions inside the measured op, one
`TemporaryDirectory`-isolated fixture per scenario, deterministic counter-based
CIDs). State-dependent scenarios set a `HSYNC_STATE_DIR` override + a seeded
`CONFIG` and clear the module store caches. Results are comparative (this
machine, warm page cache), never production SLAs.

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
* **TOFU pinning is flock+fsync dominated** — `get`/`check_and_record` ~0.0156
  ms, the durable `set` ~0.0733 ms (atomic 0o600 temp-write + replace + fsync).
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

## Running

```bash
cd hsync-client
.venv/bin/python bench/run_all.py --count 2000            # -> bench_result.txt
.venv/bin/python bench/bench_autotune.py --count 2000 [--json-out FILE]
# ... (one script per module; see bench/README.md)

# startup benchmark (standalone: spawns processes, depends on binary paths)
.venv/bin/python bench/bench_startup.py --count 30 --no-binary [--json-out FILE]
.venv/bin/python bench/bench_startup.py --count 30 --binary dist/linux-amd64/hsync [--json-out FILE]
```

Lint: `cd hsync-client && .venv/bin/python -m ruff check bench` — the directory
is covered by the project ruff config, so the Quality Gate applies to `bench/`
too.
