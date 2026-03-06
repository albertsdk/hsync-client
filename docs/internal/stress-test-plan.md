# HSYNC Client — Stress Test Plan (LAN P2P + Client-Server)

> **Status: Draft — design approved and code-reviewed, implementation NOT started.**
> This document is the handoff point for resuming the activity. Follow §16
> (Definition of Done checklist) and the code references in §15 to pick the work
> back up. All claims about engine behaviour below were verified against the
> codebase at commit `a5cf57e` (workspace root) — do not re-derive them blindly.
>
> **Rev. 2 (2026-08-29) — accuracy review pass.** All §13/§15 anchors re-verified
> against the `hsync-client` working tree at HEAD `da9b1f6` (three line-drifts
> corrected). Changes from the review: server-bucket artifact whitelist extended
> — `.backup`, `.delta.part.lit`, single-writer / conflict-copies stance
> (§6.2, §13.8); LAN convergence measurement reworked to quiet-poll + one full
> verify (§7); per-iteration / per-sweep isolation and seed derivation specified
> (§7, §9, §16); server-mode verification hierarchy REST-primary /
> FS-secondary made explicit (§4, §6, §16); generator depth semantics and leaf
> invariant tightened (§5); `HSYNC_STRESS_POLL_INTERVAL` knob added (§10).
>
> Scope: `hsync-client` (Python). Reuses the existing black-box harnesses; no
> changes to engine code are required.

> **Related tooling (implemented)** — `scripts/benchmark_lan_transfers.py`
> is the lightweight HTTP-level micro-benchmark that complements this plan:
> it measures the LAN master's per-request overhead (keep-alive vs
> connection-per-request, TCP_NODELAY effect), the per-file upload-status
> round trip and compressed/uncompressed download throughput, against an
> in-process master on loopback. This full e2e stress harness remains the
> tool for convergence/throughput under real two-peer topologies.

## 1. Goal & acceptance criteria

A repeatable, deterministic **load/stress harness** that:

1. Generates a large, heterogeneous tree (files, directories, nested
   sub-directories) with **always-different names, sizes, contents, permissions
   and mtimes** (seeded PRNG → reproducible).
2. Drives the real client through its **public CLI** in both run modes:
   - **Server mode** (client ↔ `hsync-server`, REST+WS);
   - **LAN P2P mode** (master/slave, mDNS + HTTPS/WSS).
3. Verifies that the replica matches the source **byte-for-byte**, including
   structure, size, content digests, permissions and (within tolerance) mtime.
4. Measures and reports **replication time and throughput** (files/s, MiB/s) per
   mode, plus a mutation phase and a concurrency sweep.
5. Is **opt-in** (env gate, like the BB suites) so it never runs in the default
   `make test`/CI.

## 2. Contract ground truth (verified against the code)

| Characteristic | Wire model | SSOT / reference | Notes |
| --- | --- | --- | --- |
| Structure (files/dirs) | POSIX bucket-relative paths | `hsync-common/models/file.py`, `models/dir.py` | Hidden/ignored entries are excluded at scan time (`sync.py:get_files_local`) |
| File content | `sha256sum` + `size` | `models/file.py:FileDict`, `to_dict()` | Re-verify locally by streaming hash of the received bytes |
| File permissions | octal string e.g. `"644"` | `hsync-common/permissions.py` (`mode_to_octal`) | Receiver strips special bits (`REMOTE_PERMISSION_MASK = 0o777`) |
| Directory permissions | octal string via dedicated `/dirs` listing or LAN dir events | `permissions.py:OWNER_DIRECTORY_ACCESS = 0o700` floor; server `api/files/list.py:162` (`GET /dirs`) | **Mode parity is guaranteed only in LAN (event-driven); see §13.3** |
| File mtime | epoch seconds (`mtime` field) + `version.corrected_mtime` | `models/file.py:FileDict`; `models/version.py`; `clock.clamp_incoming_corrected_mtime` | Receiver sets `utime(mtime, mtime)` (`transfer.py:452,674`); **assert within tolerance** |
| Symlinks | structural pair (path, target) | `wire_symlink_target`, `is_safe_symlink_target` | **Out of scope for v1** (see §14) |

## 3. Verifiable assertions per mode

| Assertion | Server mode | LAN mode |
| --- | --- | --- |
| All files present on the replica, no extra user files | **strict** | **strict** |
| All directories present on the replica | **strict** (dirs implied by file parents) | **strict** |
| File size equal | **strict** | **strict** |
| File content digest (streaming SHA-256 of replica bytes) equal | **strict** | **strict** |
| File mode: `stat().st_mode & 0o777 == source_mode & 0o777` | **strict** | **strict** |
| Directory mode: `stat().st_mode & 0o777 == (source & 0o777)` after floor | **floor-only** (`≥ 0o700`) | **strict** (generate after slave `[READY]`) |
| File mtime: `abs(actual - source) <= MTIME_TOLERANCE_S` | **soft** (default ±2 s) | **soft** (default ±2 s) |
| No sync-engine artifacts leaked into the replica (`/files`, `/dirs` listings) | **strict** (whitelist engine names) | **strict** (whitelist engine names) |

Expected *applied* directory mode on any receiver:
`(source_mode & 0o777) | 0o700` (the `OWNER_DIRECTORY_ACCESS` floor).

## 4. Harness layout (files to create)

All new code lives in `hsync-client` and mirrors the existing BB conventions
(MPL header, `pytestmark` env gate, helpers module like `lan_helpers.py`).

| File | Purpose |
| --- | --- |
| `hsync-client/tests/stress_tree.py` | Shared, importable-only helper: deterministic generator, manifest, verifier, metrics. **No tests inside.** |
| `hsync-client/tests/test_bb_server_stress.py` | Server-mode stress (one-shot + mutation + concurrency sweep). Verification is **REST-primary** (listings + per-file download re-hash); FS cross-check on the bucket is **secondary** (§6). Gate: `HSYNC_STRESS=1`. |
| `hsync-client/tests/test_bb_lan_stress.py` | LAN-mode stress (initial burst + mutation). Gate: `HSYNC_STRESS=1`. |
| `Makefile` (workspace root) | New target `test-stress` (see §11). |

## 5. Tree generator specification (`stress_tree.generate`)

Signature:

```python
def generate(root: Path, *, seed: int, n_files: int, n_dirs: int,
             max_depth: int = 5, max_file_size: int = 8 << 20,
             allow_empty_dirs: bool = False) -> Manifest
```

- **Determinism**: a single `random.Random(seed)` drives layout; every file
  content stream is derived from a per-path `random.Random(f"{seed}:{relpath}")`
  so the manifest can be re-derived during verification without holding all
  content in memory.
- **Layout / depth semantics**: path depth = `len(relpath.parts) - 1`.
  Directories are built at depths `0..max_depth - 1` only; files are placed at
  depths `0..max_depth` (bucket-root files allowed). **Leaf invariant: every
  leaf is a file** — every directory contains at least one *file somewhere in
  its subtree* (a dir whose only child is another dir is fine; a dir with no
  file below it is never emitted). This is strictly stronger than "no empty
  directories" and is what makes the server-mode **strict** dir assertion
  sound: server-side dirs materialize only as ancestors of uploaded files
  (§13.2). It also avoids `.hsynckeep` sentinels.
- `allow_empty_dirs` is **reserved** for the deferred empty-dir scenario
  (§14, LAN `.hsynckeep` path); v1 always passes `False`.
- **Names**: random combination from a safe pool, e.g. alphanumeric lower/upper,
  Unicode (`foto-ñ`, `dati-č`), spaces, dots, dashes, and long names (up to
  128 chars); **no leading dots** unless colliding with the forbidden set below.
  Uniqueness enforced per relative path (suffix `-<n>` on collision).
- **Forbidden names (must never be generated)**: the always-active default
  ignore patterns and engine-owned names — see §13.1 for the exact list.
- **File sizes** (mixed distribution to cover both filesystem "thighs"):
  ~10% empty (0 B), ~25% 1 B – 1 KiB, ~35% 1–64 KiB, ~25% 64–256 KiB,
  ~5% 256 KiB – `max_file_size` (default 8 MiB outliers).
- **Content**: two families — **incompressible** random bytes (stresses SHA-256,
  zstd-no-op path) and **compressed** repetitive patterns (zeros, repeating
  blocks — stresses the zstd transfer path). Record family per path.
- **Permissions**: files drawn from `600, 644, 700, 755, 666, 200, 640`
  (all ≤ `0o777`; **never setuid/setgid/sticky** — they are stripped on receipt
  by design). Directories drawn from `700, 750, 755`.
- **mtime**: random within a window (e.g. `1_700_000_000 .. now`), applied with
  `os.utime`. Stored as integer seconds in the manifest.
- **Manifest**: `{relpath: {"type": "file"|"dir", "size": int,
  "sha256": str | None, "mode": int, "mtime": int}}` returned to the caller and
  persisted to a JSON sidecar (`<root>.<name>.manifest.json`) for cross-run
  comparison and debugging.

### `stress_tree.apply_mutation`

Applies an incremental change wave on an existing tree while keeping the
manifest current; returns the list of primitives applied per path
(`create | modify | delete | chmod`). Fraction configurable (default 20% of the
file set, see §8). The wave RNG is `random.Random(f"{seed}:wave{w}")` —
deterministic and independent of the generation stream, so the post-mutation
manifest is exactly reproducible. `chmod` primitives target **files**; a
directory chmod propagates only in LAN mode (§13.3 — the server-mode
floor-only dir assertion is unaffected either way).

### `stress_tree.verify_tree`

Compares a replica root against a manifest and returns
`list[Mismatch(path, field, expected, actual)]` plus `VerificationStats`
(objects, bytes, mismatches). Never asserts; the tests assert on the result.

## 6. Verifier specification

Inputs: replica root, manifest, mode (`"server"` / `"lan"`), engine-artefact
whitelist.

**Server-mode verification hierarchy**: the **primary** path is wire-level —
REST listings for structure/metadata parity + per-file download re-hash for
content (true black-box). The FS sweep below on the server bucket
(`bb_bucket`) is a **secondary / fast cross-check** only: it reads the
server's storage layout directly (as the existing BB fixtures already do),
but it must never be the sole verdict.

1. **Existence both ways**: every manifest path exists on the replica; every
   non-whitelisted on-disk entry below the replica root exists in the manifest.
2. **Engine-artefact whitelist** — never appear in `/files` / `/dirs`
   listings and are ignored by the path-exact check:
   - client/common engine names (`hsync-common/constants.py`,
     `path_validation.py:413`): `.hsyncfolder`, `.hsyncignore`, `.hsyncstate`,
     `.hsynctombstones`, `.hsynckeep`;
   - partial-transfer leftovers: `<name>.part`, `<name>.delta.part` and the
     parked literal suffix `<name>.delta.part.lit` (`models/file.py:116`,
     `is_partial_file`);
   - server-bucket upload artifact `<file>.backup`
     (`hsync-server/constants.py:42 BACKUP_SUFFIX`, created by `upload.py:316`
     before every overwrite, skipped by the `/files` listing at `list.py:279`,
     removed/restored on the success/failure paths).
   Whitelisted entries found on disk are **counted** in `VerificationStats`
   (never silently dropped) so lingering artifacts stay observable in the
   report. **Single-writer assumption**: each scenario drives exactly one
   client per bucket, so server-side conflicted copies
   (`conflicted_copy_name`, `upload.py:525,549`) must never exist — they are
   deliberately **not** whitelisted and surface as extra-file mismatches,
   which is the correct verdict for a violated assumption (§13.8).
3. **Type**: `file` vs `dir` via `Path.is_dir()`.
4. **File**: `stat().st_size == size`; streaming SHA-256 of the replica file
   (`hashlib.sha256` over 1 MiB chunks) `== sha256`.
5. **File mode**: `stat().st_mode & 0o777 == manifest mode & 0o777`.
6. **Directory mode**: server mode → assertion that
   `stat().st_mode & 0o700 == 0o700` (floor only); LAN mode → equality with
   `(manifest mode & 0o777) | 0o700`.
7. **mtime**: `abs(stat().st_mtime - manifest mtime) <= MTIME_TOLERANCE_S`
   (default 2; configurable via `HSYNC_STRESS_MTIME_TOLERANCE`).
8. Returns sorted mismatch list; the test prints the first N mismatches with
   `expected/actual` pairs for diagnosis.

## 7. Timing & metrics

All durations from `time.perf_counter()`; measured **only around engine work**
(no measurement jitter from generators):

- `t_generate` — tree creation.
- `t_sync` — **server**: full one-shot CLI run (in-process `CliRunner`) **or**
  subprocess wall-clock (see §13.5). **LAN**: convergence time, measured from
  write-complete on the source side to the replica being **quiet**: poll a
  cheap signal only (file count + total size via an `os.scandir` walk — no
  hashing) every `HSYNC_STRESS_POLL_INTERVAL` (default 0.5 s) until unchanged
  for 2 consecutive polls, then run the **single** full `verify_tree`.
  `t_sync` stops at quiet-detection (measurement resolution = the poll
  interval); the full verify is accounted in `t_verify`. On mismatch, resume
  quiet-polling until `HSYNC_STRESS_TIMEOUT`, then fail. Never poll with the
  full verifier: hashing ~1500 files per poll makes `t_sync`
  verification-bound and steals disk IO from the engine under measurement.
- `t_verify` — verification pass.
- Derived: `files/s`, `MiB/s`, `dirs`, totals.

### Run isolation (iterations & sweep)

- Iterations are **parametrized** (`@pytest.mark.parametrize("iteration",
  range(HSYNC_STRESS_ITERATIONS))`) so each gets fresh function-scoped
  fixtures: new `live_server` + `bb_bucket` + `local_dir` (server mode) or
  fresh `lan_master_dir`/`lan_slave_dir`/`lan_bucket` (LAN). **No bucket/dir
  reuse across iterations** — a reused bucket has nothing left to transfer
  and yields degenerate timings.
- **Seed derivation**: iteration `i` uses `seed_i = HSYNC_STRESS_SEED + i`
  (effective seed goes into the report JSON); mutation wave `w` draws from
  `random.Random(f"{seed_i}:wave{w}")`. With a fixed base seed the repeated
  iterations measure engine variance, not data variance — intentional.
- Peak disk ≈ one tree + one replica per run; pytest retains the last 3
  `tmp_path` trees. §16 includes the teardown check.

Per-run report (printed table + optional JSON at `HSYNC_STRESS_REPORT` path):

```json
{"mode": "server|lan", "seed": int, "n_files": int, "n_dirs": int,
 "total_bytes": int, "t_generate_s": float, "t_sync_s": float,
 "t_verify_s": float, "files_per_s": float, "mib_per_s": float,
 "max_concurrent_transfers": int, "iteration": int, "mismatches": []}
```

Phases run **3 iterations** by default (`HSYNC_STRESS_ITERATIONS`) and report
median + min/max for every metric.

## 8. Incremental mutation phase

After the initial burst converges:

1. Keep a **live manifest** (copy of the original + applied mutations).
2. Apply one wave via `apply_mutation` (defaults: 20% of the file set, split
   roughly `40% modify / 25% create / 25% delete / 10% chmod` — all within the
   existing tree, never touching engine artefacts).
3. **Server mode**: re-run the one-shot sync (delta diff picks up content/mode
   changes and tombstones for deletes). Measure `t_sync` again.
4. **LAN mode**: source-side watcher picks the wave up over the event channel;
   measure convergence time again.
5. Re-verify with the updated manifest (strict for files, floor for server dirs).
6. Optionally repeat for `HSYNC_STRESS_WAVES` waves (default 1).

This exercises the **delta path**, **tombstone path**, **permission-only
chmod detection** (`file_event_handler.py` `[PERMISSION]` branch), and the
**reconcile fast-path** under load.

## 9. Concurrency sweep

For each value in `HSYNC_STRESS_CONCURRENCY` (default: `"1,4,8"`):

- Run the server-mode initial-burst scenario with
  `--no-autotune --max-concurrent-transfers <N>` (flags exist at `cli.py:350`
  and are validated `MIN..MAX`; `main_logic.py:165` resolves precedence
  CLI > env > autotune > default).
- Each N is its own parametrized test case → **fresh tree + fresh bucket**
  per N (same isolation rules as §7); N lands in the report JSON
  (`max_concurrent_transfers`).
- Record `t_sync` + throughput per N; print the scaling table.

LAN stress runs with the existing `_lan_start` args (`--no-autotune`), keeping
the default concurrency unless `HSYNC_MAX_CONCURRENT_TRANSFERS` is set inside
the test body (§13.5).

## 10. Environment knobs

| Variable | Default | Meaning |
| --- | --- | --- |
| `HSYNC_STRESS` | unset | Gate: `"1"` enables the stress tests (`pytestmark = skipif`) |
| `HSYNC_STRESS_SEED` | `20260828` | PRNG seed for generation |
| `HSYNC_STRESS_FILES` | `2000` (server) / `1500` (LAN) | Number of files |
| `HSYNC_STRESS_DIRS` | `300` (server) / `200` (LAN) | Number of directories |
| `HSYNC_STRESS_MAXDEPTH` | `5` | Max nesting depth |
| `HSYNC_STRESS_MAXFILESIZE` | `8388608` | Outlier file size cap |
| `HSYNC_STRESS_ITERATIONS` | `3` | Repetitions for medians |
| `HSYNC_STRESS_WAVES` | `1` | Mutation waves |
| `HSYNC_STRESS_MUTATION_FRACTION` | `0.2` | Mutation wave size |
| `HSYNC_STRESS_CONCURRENCY` | `"1,4,8"` | Server-mode concurrency sweep |
| `HSYNC_STRESS_TIMEOUT` | `300` | Convergence/run timeout (s) |
| `HSYNC_STRESS_POLL_INTERVAL` | `0.5` | LAN quiet-poll interval (s); also the convergence-time measurement resolution |
| `HSYNC_STRESS_MTIME_TOLERANCE` | `2` | mtime tolerance (s) |
| `HSYNC_STRESS_REPORT` | unset | Path for the JSON report (optional) |

Prefer **CLI flags** over `HSYNC_*` env for per-run tuning (see §13.5 — the
autouse env scrub).

## 11. Makefile integration

Add to the workspace-root `Makefile` (mirrors `test-bb-client`, which keeps
server and LAN invocations separate):

```make
test-stress:
	@echo "==> stress: server-mode"
	@cd hsync-client && HSYNC_STRESS=1 $(VENV_PY) -m pytest -q tests/test_bb_server_stress.py
	@echo "==> stress: LAN"
	@cd hsync-client && HSYNC_STRESS=1 $(VENV_PY) -m pytest -q --maxfail=3 tests/test_bb_lan_stress.py
```

`make test-stress` must NOT be part of `test`/`ci`.

## 12. Execution workflow

```bash
# Server mode only
cd hsync-client && HSYNC_STRESS=1 .venv/bin/python -m pytest -q tests/test_bb_server_stress.py
# LAN only (loopback mDNS; separate run as per BB conventions)
cd hsync-client && HSYNC_STRESS=1 .venv/bin/python -m pytest -q --maxfail=3 tests/test_bb_lan_stress.py
# Both (root orchestrator)
make test-stress
# Lint/type gates before committing (Python project rules)
cd hsync-client && .venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .
cd hsync-client && .venv/bin/python -m mypy hsync_client
```

Scale up for a real benchmark (`HSYNC_STRESS_FILES=20000 HSYNC_STRESS_DIRS=3000`),
scale down for CI (`HSYNC_STRESS_FILES=300 HSYNC_STRESS_DIRS=50`,
`HSYNC_STRESS_ITERATIONS=1`).

## 13. Contract caveats (verified against the code)

1. **Always-active default ignore patterns** (`hsync-common/constants.py:93`
   `DEFAULT_IGNORE_PATTERNS`): `.DS_Store`, `._*`, `.Spotlight-V100`,
   `.Trashes`, `Thumbs.db`, `Thumbs.db:encryptable`, `ehthumbs.db`,
   `ehthumbs_vista.db`, `desktop.ini`, `~$*`, `.*.sw?`, `*~`, `4913`,
   `.~lock.*#`, `.goutputstream-*`, `*.crdownload`, `*.tmp`, `*.temp`,
   `*.bak`, `*-????-??-??-??????`. The generator must never emit names matching
   these — such files are silently excluded from sync and would fail the
   "exact replica" check. Also excluded always: `INTERNAL_FILENAMES`
   (`path_validation.py:413`: `.hsyncignore`, `.hsyncfolder`, `.hsyncstate`,
   `.hsynctombstones`) and partial-transfer suffixes (`.part`, `.delta.part`).
2. **Empty directories are not replicated in server mode.** Server storage is
   file-backed: dirs materialize as parents of uploaded files
   (`upload.py:311 mkdir(parents=True)`), so a dir with no file in the tree has
   no server-side representation. LAN mode preserves empty dirs via the
   `.hsynckeep` sentinel (`constants.py:74`). → generator: **no empty dirs**;
   verifier: whitelist `.hsynckeep`.
3. **Directory-mode parity is LAN/event-driven only.** In server mode the
   one-shot bidirectional sync does **not** push directory permissions: the
   `/dirs` listing (`list.py:162`) reflects server-on-disk modes (default mkdir
   modes) and the client-side converge runs only when
   `SYNC_DIRECTION == "download-only"` (`sync.py:362-367`,
   `converge_directory_permissions:1247`). Permissions ARE applied from inbound
   directory events in LAN continuous mode (event handler → `set_permissions_on_dir`,
   `utils.py:635`). → server-mode dir assertion = **floor-only**; LAN dir
   assertion = **strict** and generation must happen **after** the slave is
   `[READY]` (§14 LAN flow).
4. **mtime is rounded/clamped, never asserted exactly.** Receiver applies
   `utime(mtime, mtime)` on download (`transfer.py:452,674`) but the wire value
   and the local corrected clock interact through
   `clamp_incoming_corrected_mtime` (`clock.py:448`) for the version metadata,
   and filesystems can truncate sub-second precision. → tolerance-based assert.
5. **Env scrub and process vs in-process**: client conftest autouse fixtures
   scrub every `HSYNC_*` env var per test (`tests/conftest.py:_scrub_hsync_env`)
   and re-isolate per-test device/state dirs. Consequences:
   - Prefer **CLI flags** (`--max-concurrent-transfers`, `--no-autotune`) over
     env for tuning; if env must be used, set it **inside the test body** and
     clear the env-settings cache (`get_env_settings.cache_clear()`, as
     `run_sync_once` does).
   - `run_sync_once` runs the client **in-process** (CliRunner) — engine speed,
     no process boot noise. For **wall-clock realism**, add a subprocess driver
     modeled on `lan_helpers._lan_start` but with
     `<hostname> --port <port> -b <bucket> -p <path> --yes --insecure --once
     --no-autotune --min-free-disk-space 0` and `HSYNC_API_KEY` in the env.
     Default the stress suite to in-process for comparability; expose
     `HSYNC_STRESS_SUBPROCESS=1` for the wall-clock variant.
6. **Server BB fixture already removes rate-limit noise**:
   `live_server` (`conftest.py:462`) runs the real server as a **subprocess**
   with `HSYNC_RATE_LIMIT_*` raised to 1 M/min — stress runs are not throttled.
7. **File metadata keys on the wire** (server `/files`): `filepath`, `name`,
   `ctime`, `mtime`, `size`, `sha256sum`, `permissions`, optional `version`.
8. **Server-bucket artifacts** (`hsync-server`): the upload path writes
   `<file>.backup` before every overwrite (`upload.py:316`,
   `constants.py:42 BACKUP_SUFFIX`); the `/files` listing skips it
   (`list.py:279`) but a direct-FS `verify_tree` on the bucket would see it →
   whitelisted in §6.2 (counted, never silently dropped). Conflicted copies
   (`conflicted_copy_name`, `upload.py:525,549`) are **not** whitelisted: the
   stress scenarios are single-writer, so one surfacing is a real failure.
   Tombstones and version clocks persist in the server state DB
   (`tombstones.py:41`, `versions.py:61`), never inside the bucket — no
   whitelist entry needed.

## 14. Out of scope / deferred (v1)

- **Symlinks** (structural replication with safety checks exists but adds edge
  cases: escaping-target paths are dropped). Add a dedicated symlink scenario
  later.
- **`.hsyncignore` rule testing** (exclusion semantics are a separate concern
  from replication parity).
- **Server sidecar persistence / OpenAPI/drift** — untouched.
- **Android** parity — untouched.
- **Empty-dir replication** in server mode (engine design; out of contract).

## 15. Code reference index (verification anchors)

| Concern | Reference |
| --- | --- |
| BB server fixtures | `hsync-client/tests/conftest.py:462` (`live_server`), `:733` (`bb_bucket`), `:760` (`bb_server_upload`), `:788` (`bb_server_files`), `:808` (`bb_server_download`), `:829` (`local_dir`), `:1007` (`run_sync_once`) |
| BB LAN helpers | `hsync-client/tests/lan_helpers.py` — `_lan_env:96`, `_lan_start:131`, `_terminate:187`, `_wait_for_file:229`, `_wait_for_text_in_stderr:239`, `_wait_for_slave_events:258` (`[READY]` marker), `_wait_for_master:358`; fixtures `lan_master_dir:392`, `lan_slave_dir:400`, `lan_bucket:408` |
| CLI flags | `hsync-client/hsync_client/cli.py` — subcommands `lan`/`server`, `--once`, `--insecure`, `--no-autotune`, `--max-concurrent-transfers`, `--min-free-disk-space`, `--force-master`, `--never-master`, `--sync-direction` |
| Concurrency resolution | `hsync-client/hsync_client/main_logic.py:165` (`_get_max_concurrent_transfers_value`) |
| Permissions SSOT | `hsync-common/hsync_common/permissions.py` (`mode_to_octal`, `sanitize_remote_permissions:85`, `REMOTE_PERMISSION_MASK`, `OWNER_DIRECTORY_ACCESS`) |
| Permissions apply (client) | `hsync-client/hsync_client/utils.py:616` (`set_permissions`), `:635` (`set_permissions_on_dir`); download `transfer.py:452,678`; dir converge `sync.py:1213-1282` |
| mtime | `transfer.py:452,674` (`utime`); `hsync-common/hsync_common/clock.py:448` (`clamp_incoming_corrected_mtime`); `hsync-common/models/version.py` |
| Wire file model | `hsync-common/hsync_common/models/file.py` (`FileDict`, `to_dict`) |
| Server listing | `hsync-server/hsync_server/api/files/list.py:162` (`GET /dirs`), `:220` (`GET /files`) |
| Server upload storage | `hsync-server/hsync_server/api/files/upload.py:270` (`update_file_timestamp`), `:311` (`mkdir parents=True`) |
| Ignore defaults / engine names | `hsync-common/hsync_common/constants.py:93`; `hsync-common/hsync_common/path_validation.py:413`; `hsync-common/hsync_common/ignore.py` |
| LAN readiness markers | `lan_handler.py:343` (`[READY] Master ready`), `:525` (`Initial sync complete, connecting to WebSocket…`) |

## 16. Definition of Done — resume checklist

- [ ] `hsync-client/tests/stress_tree.py` implementing §5 and §6 (generator,
      mutation, verifier, metrics; no asserts inside).
- [ ] `hsync-client/tests/test_bb_server_stress.py` (gate `HSYNC_STRESS=1`):
      initial burst → primary wire verification: REST listings (`/files` for
      metadata, `/dirs` for dirs) + per-file download re-hash → secondary FS
      cross-check of the bucket via `verify_tree` (`bb_bucket`, §6.2 whitelist
      incl. `.backup`; whitelist hits counted in the report) → mutation wave →
      concurrency sweep → report.
- [ ] `hsync-client/tests/test_bb_lan_stress.py` (gate `HSYNC_STRESS=1`):
      start master+slave (`_lan_start`), `_wait_for_master`, generate **after**
      `_wait_for_slave_events` (`[READY]`), poll convergence, verify, mutate,
      re-verify; always `_terminate` both peers in `finally`.
- [ ] Isolation per §7: iterations and sweep steps parametrized (fresh
      server+bucket / LAN dirs each), derived seeds (`seed_i = seed + i`, wave
      RNG `f"{seed_i}:wave{w}"`), no bucket/dir reuse; teardown leaves no
      trees or buckets behind (confirmed in the final report).
- [ ] Type-check scope: `mypy hsync_client` covers the package only (project
      convention); the new `tests/` modules are ruff-checked but intentionally
      not mypy-checked — state this in the final report.
- [ ] Workspace-root `Makefile` target `test-stress` (§11), **not** wired into
      `test`/`ci`.
- [ ] Docs: this plan stays as-is; implementation notes go to
      `docs/internal/testing.md` (client) and `llm-wiki` only if behaviour
      changes are discovered.
- [ ] Gates before commit (English, Conventional Commits):
      `ruff check . && ruff format --check .` and `mypy hsync_client` in
      `hsync-client`; BB suites unaffected; cite env-gate reason in the message.
- [ ] Final report must list: commands executed and outcomes per mode,
      measured timings for the chosen scale, checks not run and why, residual
      risks.
