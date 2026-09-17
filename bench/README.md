# Benchmarks — hsync-client

Two tiers today: **L1 in-process micro-benchmarks** of the client-specific hot
paths, and an **L2 loopback wire benchmark** of the LAN peer-download path
(real `LANServer` seeder + master, real TLS). The shared library primitives
(`hsync_common` hashing, compression, delta, models, path validation, stores) are
already covered by `hsync-common/bench`; this directory measures only what the
client adds on top:

| Tier | Status here | Why |
|---|---|---|
| Startup — process spawn cost | **implemented** (`bench_startup.py`) | Wall-clock of every fast `hsync` invocation (`--version`, `--capabilities-json`, `--help`, `--help-symbols`) plus the import-only cases, from source and from the packaged PyInstaller binary |
| L1 — in-process micro-benchmark | **implemented** (this directory) | Measures the client wrappers the sync engine calls per path/per event |
| L2 — transport load (peer-download path) | **implemented** (`bench_lan_peer_transfer.py`, `bench_lan_sources.py`) | The two full-download paths and the source lookup, end to end over loopback with real servers; measures what JSON-level fixtures cannot (TLS handshake, keep-alive reuse, seeder read cap) |
| L2/L3 — full-fleet load + soak | deferred | Spawning the `hsync` binary as a multi-node fleet, wire-only; decided after this peer-download baseline |
| L4 — profiling diagnostic | n/a (yet) | Added only when a specific anomaly needs a cost breakdown |
| L5 — CPU-saturation sweep | n/a | Multi-process LAN topology, not a single Python call — revisit with the fleet tier |

Each script is a **plain script, not a test**: it is never collected by pytest
(`testpaths = ["tests"]`, `python_files = ["test_*.py"]`), never runs in CI, and
asserts only that the measured operation really took the intended branch.

The L2 scripts are deliberately **not** part of `run_all.py`: they are one to two
orders of magnitude slower than the L1 scenarios and their numbers depend on
loopback/TLS behaviour, so folding them into the committed `bench_result.txt`
baseline would make it noisy without adding regression signal. Run them
standalone (see below) and compare with `--json-out` + `compare.py`.

## Scripts

| Script | Measures |
|---|---|
| `bench_startup.py` | **Startup** — wall-clock of every fast `hsync` invocation (`--version`, `--capabilities-json`, `--help`, `--help-symbols`) plus `import hsync_client.cli` / `import hsync_client.main_logic`, from source (venv console script / `python -m hsync_client`) and from the packaged `--onefile`/`--onedir` binary |
| `bench_autotune.py` | `classify_network`, `classify_bandwidth`, `derive_timeout`, `derive_retry`, `derive_concurrency`, `derive_profile`, `profile_transfer_queue` (pure policy, no I/O) |
| `bench_utils.py` | `http_to_ws`, `redact_headers`, `request_headers`, `inbound_size_cap`, `http_connect_timeout`, `upload_timeout`, `is_same_paths`, `ensure_str_path`, `normalize_bucket_relpath` (pure per-request/path helpers) |
| `bench_event_queue.py` | `QueuedEvent.get_backoff_delay`, `EventQueue.enqueue`, `EventQueue._send_event(success/failure/unserializable)` (async, in-memory socket stub) |
| `bench_pinning.py` | `PinStore.get(hit/miss)` / `check_and_record(match/changed/first-use)` / `set` (durable TOFU, temp pin file) |
| `bench_sync_state.py` | `get_sync_state_store`, `get_base_hash`, `record_base`, `upload_version_fields` (same/ticked), `merge_and_record`, `flush` (temp bucket + state dir) |
| `bench_tombstones.py` | `get_tombstone_store`, `get_payload`, `record_deletion` (persist), `merge_payload` (dominant), `mint_50(memory)` / `mint_50_then_flush` (batched offline mint) |
| `bench_arbitration.py` | `arbitrate_upload` on the first / dominant / superseded / conflict branches, `conflicted_copy_name` |
| `bench_reconcile.py` | reconcile core: `_compare_files` (5 branches), `_tombstone_clock_dominates`/`_tombstone_deletes_local`, `_parse_advertised_version`, and the full `get_sync_actions` bulk diff over an in-sync listing |
| `bench_lan_peer_transfer.py` | **L2** — cold/warm peer download vs warm master download per size (4 KiB…8 MiB) through the production `download_file`, seeder read-cap saturation sweep, concurrent production fallback |
| `bench_lan_sources.py` | **L2** — `LanSourceResolver._identity` eligibility branches, `_source_batches` splitting, real `POST /files/sources` round trips, `LanSourceTelemetry` rank/record cost |
| `lan_peer_common.py` | Shared L2 fixtures (loopback seeder/master harness, temp cert + bucket + pin file + state dir, `measure_with_setup`); also `LatencyRelay`, the unprivileged RTT injector that counts TLS handshakes (`tc netem` needs root). Imports `hsync_client`, so it stays out of `common.py`, which is stdlib-only |
| `run_all.py` | Chained runner: runs every **L1** script above and writes `bench_result.txt` at the repo root (committed baseline, diff-friendly); `--json-out-dir DIR` also dumps one `<script>.json` per scenario for A/B comparison |
| `compare.py` | A/B comparator of two `--json-out` runs (single file or directory): flags median regressions over a noise floor, `exit 1` on any regression |
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
.venv/bin/python bench/bench_reconcile.py --count 2000

# L2 wire benchmark (standalone: not part of run_all.py / bench_result.txt)
.venv/bin/python bench/bench_lan_peer_transfer.py --count 40 --json-out /tmp/peer.json
.venv/bin/python bench/bench_lan_peer_transfer.py --count 6 --latency   # handshake/RTT scenario
.venv/bin/python bench/bench_lan_sources.py --count 2000 --json-out /tmp/sources.json

# startup benchmark (standalone: spawns processes, depends on binary paths)
.venv/bin/python bench/bench_startup.py --count 30 --no-binary
.venv/bin/python bench/bench_startup.py --count 30 --binary dist/linux-amd64/hsync
.venv/bin/python bench/bench_startup.py --count 30 --binary-dir /usr/lib/hsync --json-out /tmp/onedir.json

# machine-readable results (one JSON array per run)
.venv/bin/python bench/bench_autotune.py --count 2000 --json-out /tmp/autotune.json

# A/B comparison: run the whole suite into two directories, then diff them
.venv/bin/python bench/run_all.py --count 2000 --json-out-dir /tmp/before
.venv/bin/python bench/run_all.py --count 2000 --json-out-dir /tmp/after
.venv/bin/python bench/compare.py /tmp/before /tmp/after --threshold 1.10
```

`--count` is the number of timed samples for the cheap operations. Heavier
operations are capped per scenario (the persist-per-op tombstone/record paths run
`record_deletion`/`merge_payload` and `flush` at a lower sample count) so a
default run stays short; the reported `n` column shows the effective sample
count.

The L2 benchmarks follow the same rule from the other direction: their default is
`--count 40` (one wire sample costs 1–30 ms, not 2 µs) and `bench_lan_sources.py`
caps its `POST /files/sources` scenarios at `min(--count, 50)`. Both need no
external service: they start their own loopback servers, generate their own
self-signed certificates, and keep every state file in a `TemporaryDirectory`.

**See also**: `scripts/benchmark_lan_transfers.py` is the older HTTP-level
micro-benchmark of the master's *raw* per-request overhead (keep-alive vs
connection-per-request, TCP_NODELAY, compressed/uncompressed throughput) using
`http.client` directly. It shares no scope with the L2 scripts here: those drive
the real client transfer path (`download_file`, source lookup, resolver and
telemetry) and the peer seeder, which the raw script never touches.

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
* **Full latency stats, not only central tendency**: every result reports
  `mean`/`p50`/`p99` plus `stdev`/`min`/`max`, so a later comparison can tell a
  genuine slowdown from a noisy tail.
* **Comparisons are relative**: numbers are valid for this machine and a warm
  OS page cache; they compare implementations, not production SLAs.
* **Realistic RTT without privileges**: `--latency` puts a `LatencyRelay` in
  front of the seeder *and* the master instead of asking for `tc netem` (root-only
  here). The relay copies bytes and delays every chunk, so TLS stays end to end and
  the requester still pins the real fingerprint, while the connections it accepts
  count exactly the TLS handshakes a cycle paid. That scenario also drives the
  production path through **one long-lived `ThreadPoolExecutor`**, because
  production reuses its transfer pool for the whole run and a per-cycle pool would
  pay handshakes that production never pays. The master phase sweeps 1 vs 4 workers
  to price bounded bulk parallelism against a sequential loop.

## Shared helpers

`bench/common.py` is stdlib-only and **never imports `hsync_client`**: the
harness must stay usable if the client internals are rewritten.

* measurement core — `BenchResult`, `measure`, `measure_async`, `percentile`,
  `scaled_warmup`, `print_table`, `add_json_out`, `maybe_write_json`;
* deterministic CIDs — `make_cid`, `make_cids`, `ALPHABET`.

`bench/lan_peer_common.py` is the L2 counterpart and **does** import
`hsync_client` (it stands up real servers and configures the real transfer path,
which is the point of the tier). It lives outside `common.py` on purpose so the
stdlib-only contract of the measurement core holds:

* `LanServerHarness` — one loopback node (`LANServer` seeder or master, own temp
  bucket, own device dir and self-signed cert); `peer_source()` yields the
  immutable source a master would return for it;
* `LanRequesterEnv` — requester `CONFIG`, temp state dir / pin file, cache resets,
  `resolver()`, and destination removal with optional session eviction;
* `measure_with_setup` — per-sample setup outside the timed region;
* `concurrent_peer_gets` / `peer_get` — production-shaped exact-hash peer GETs,
  one pinned session per worker thread, returning `(latency, status, bytes)`.

`bench/compare.py` is equally stdlib-only and never imports `hsync_client`; its
pure `compare()` decision function is pinned by `tests/test_bench_compare.py`.
