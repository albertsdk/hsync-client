# Benchmarks — hsync-client

One tier today, **L1: pure in-process micro-benchmarks** of the client-specific
hot paths. The shared library primitives (`hsync_common` hashing, compression,
delta, models, path validation, stores) are already covered by
`hsync-common/bench`; this directory measures only what the client adds on top:

| Tier | Status here | Why |
|---|---|---|
| L1 — in-process micro-benchmark | **implemented** (this directory) | Measures the client wrappers the sync engine calls per path/per event |
| L2/L3 — transport load + soak | deferred | Spawning a LAN master/slave pair over loopback (wire-only) is a follow-on; decided after this L1 baseline |
| L4 — profiling diagnostic | n/a (yet) | Added only when a specific anomaly needs a cost breakdown |
| L5 — CPU-saturation sweep | n/a | Multi-process LAN topology, not a single Python call — revisit with L2 |

Each script is a **plain script, not a test**: it is never collected by pytest
(`testpaths = ["tests"]`, `python_files = ["test_*.py"]`), never runs in CI, and
asserts only that the measured operation really took the intended branch.

## Scripts

| Script | Measures |
|---|---|
| `bench_autotune.py` | `classify_network`, `classify_bandwidth`, `derive_timeout`, `derive_retry`, `derive_concurrency`, `derive_profile`, `profile_transfer_queue` (pure policy, no I/O) |
| `bench_utils.py` | `http_to_ws`, `redact_headers`, `request_headers`, `inbound_size_cap`, `http_connect_timeout`, `upload_timeout`, `is_same_paths`, `ensure_str_path`, `normalize_bucket_relpath` (pure per-request/path helpers) |
| `bench_event_queue.py` | `QueuedEvent.get_backoff_delay`, `EventQueue.enqueue`, `EventQueue._send_event(success)` (async, in-memory socket stub) |
| `bench_pinning.py` | `PinStore.get` / `check_and_record` / `set` (durable TOFU, temp pin file) |
| `bench_sync_state.py` | `get_sync_state_store`, `get_base_hash`, `record_base`, `upload_version_fields` (same/ticked), `merge_and_record`, `flush` (temp bucket + state dir) |
| `bench_tombstones.py` | `get_tombstone_store`, `get_payload`, `record_deletion` (persist), `merge_payload` (dominant), `mint_50(memory)` / `mint_50_then_flush` (batched offline mint) |
| `bench_arbitration.py` | `arbitrate_upload` on the first / dominant / superseded / conflict branches, `conflicted_copy_name` |
| `bench_reconcile.py` | reconcile core: `_compare_files` (5 branches), `_tombstone_clock_dominates`/`_tombstone_deletes_local`, `_parse_advertised_version`, and the full `get_sync_actions` bulk diff over an in-sync listing |
| `run_all.py` | Chained runner: runs every script above and writes `bench_result.txt` at the repo root (committed baseline, diff-friendly) |
| `bench_result.txt` | **Committed baseline** (default `--count 2000`, this machine): compare revisions with `git diff bench_result.txt` |

## Running

```bash
cd hsync-client

# the whole suite in one go, writing the versioned baseline file
.venv/bin/python bench/run_all.py --count 2000            # -> ../bench_result.txt
.venv/bin/python bench/run_all.py --count 2000 --out /tmp/bench-after.txt

# or one script at a time
.venv/bin/python bench/bench_autotune.py --count 2000
.venv/bin/python bench/bench_utils.py --count 2000
.venv/bin/python bench/bench_event_queue.py --count 2000
.venv/bin/python bench/bench_pinning.py --count 2000
.venv/bin/python bench/bench_sync_state.py --count 2000
.venv/bin/python bench/bench_tombstones.py --count 2000
.venv/bin/python bench/bench_arbitration.py --count 2000

# machine-readable results (one JSON array per run)
.venv/bin/python bench/bench_autotune.py --count 2000 --json-out /tmp/autotune.json
```

`--count` is the number of timed samples for the cheap operations. Heavier
operations are capped per scenario (the persist-per-op tombstone/record paths run
`record_deletion`/`merge_payload` and `flush` at a lower sample count) so a
default run stays short; the reported `n` column shows the effective sample
count.

## Methodology

* **Warmup outside the samples**: warmup iterations run before timing and are
  scaled to `min(200, count // 10)`.
* **Setup outside the timed region**: fixtures (CONFIG, temp bucket/state dir,
  seeded stores, pin files, stub sockets) are prepared once per scenario.
* **Branch assertions inside the measured op**: every sample asserts the
  returned outcome/clock/value for that branch — a silent fallback or a
  short-circuited store cannot be reported as a fast success.
* **One scenario, one fixture lifecycle**: state-dependent scenarios run in a
  `TemporaryDirectory` (bucket + `HSYNC_STATE_DIR`) and clear the module store
  caches, so no scenario is faster because a previous one warmed shared state.
* **Deterministic CIDs**: counter-based `make_cid` (spec `^[A-Za-z0-9]{21}$`),
  no `random` seed juggling.
* **Comparisons are relative**: numbers are valid for this machine and a warm
  OS page cache; they compare implementations, not production SLAs.

## Shared helpers

`bench/common.py` is stdlib-only and **never imports `hsync_client`**: the
harness must stay usable if the client internals are rewritten.

* measurement core — `BenchResult`, `measure`, `measure_async`, `percentile`,
  `scaled_warmup`, `print_table`, `add_json_out`, `maybe_write_json`;
* deterministic CIDs — `make_cid`, `make_cids`, `ALPHABET`.
