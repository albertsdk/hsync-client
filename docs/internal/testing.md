# HSYNC Client — Testing Guide

This guide covers the test suite for the HSYNC Client: structure, how to run
tests, how to write new tests, and cross-project testing with `hsync-common`.

> For development setup, see [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Table of Contents

- [Running tests](#running-tests)
- [Test structure](#test-structure)
- [Fixtures](#fixtures)
- [Writing new tests](#writing-new-tests)
- [Mocking the server](#mocking-the-server)
- [LAN mode testing](#lan-mode-testing)
- [Black‑box tests (`test_bb_*.py`)](#blackbox-tests-test_bb_py)
- [Cross-project testing](#cross-project-testing)

---

## Running tests

```bash
# Install dev dependencies (already set up via committed virtualenv)
# If needed: poetry install

# Run the full suite
.venv/bin/python -m pytest

# Run a specific file
.venv/bin/python -m pytest tests/test_sync_local.py

# Run with verbose output
.venv/bin/python -m pytest -v

# Run with coverage
.venv/bin/python -m pytest --cov=hsync_client --cov-report=term-missing

# Run only tests matching a pattern
.venv/bin/python -m pytest -k "conflict"

# Stop on first failure
.venv/bin/python -m pytest -x
```

> For LAN black-box tests, prefer `--maxfail=N` over `-x`: a single mDNS
> election timeout is expected intermittently and should not abort the other
> independent LAN files. See [LAN‑mode Black‑Box tests](#lanmode-blackbox-tests).

## Test structure

```
tests/
├── __init__.py
├── conftest.py                         # Shared fixtures and test configuration
│                                          (autouse fixtures, Black‑Box harness,
│                                           live_server, ClientGlobalsGuard, …)
├── lan_helpers.py                      # Shared LAN test helpers
├── test_autotune.py                    # Autotune concurrency heuristics
├── test_bb_server_remote_sync.py       # Black‑Box: client‑server sync
├── test_bb_server_continuous_sync.py   # Black‑Box: continuous sync
├── test_bb_server_offline_changes.py   # Black‑Box: offline change reconciliation
├── test_bb_server_safety_guards.py     # Black‑Box: mass‑delete guard, disk‑space
├── test_bb_server_cli_surface.py       # Black‑Box: CLI help / error messages
├── test_bb_server_corrected_mtime.py   # Black‑Box: ADR-0023 tie‑break (server mode)
├── test_bb_lan_advanced.py             # Black‑Box: LAN advanced scenarios
├── test_bb_lan_auth_direction.py       # Black‑Box: LAN auth and sync direction
├── test_bb_lan_core.py                 # Black‑Box: LAN core sync cycle
├── test_bb_lan_delta.py                # Black‑Box: LAN delta transfer (REST)
├── test_bb_lan_delta_apply.py          # Black‑Box: LAN delta‑apply upload (REST)
├── test_bb_lan_features.py             # Black‑Box: LAN feature flags
├── test_bb_lan_files.py                # Black‑Box: LAN file operations
├── test_bb_lan_resumable.py            # Black‑Box: LAN resumable upload (REST)
├── test_bb_lan_security.py             # Black‑Box: LAN security (TOFU, certs)
├── test_bb_lan_upload_negatives.py     # Black‑Box: LAN upload rejection paths (REST)
├── test_bb_lan_websocket_relay.py      # Black‑Box: LAN WebSocket relay & handshake
├── test_bb_lan_corrected_mtime.py      # Black‑Box: ADR-0023 tie‑break (LAN mode)
├── test_sync_local.py                  # Local file comparison and action generation
├── test_sync_remote.py                 # Remote file comparison
├── test_sync_update.py                 # File update detection
├── test_sync_workers.py                # Parallel transfer workers
├── test_sync_permanent_failures.py     # Permanent-failure skip-set
├── test_transfer_download.py           # Download operations
├── test_transfer_upload_advanced.py    # Upload operations: advanced scenarios
├── test_transfer_upload_base.py        # Upload operations: basic flow
├── test_transfer_upload_lifecycle.py   # Upload operations: lifecycle/resume
├── test_transfer_status.py             # Transfer status tracking
├── test_websocket_events.py            # WebSocket event processing
├── test_events.py                      # Event models and processing
├── test_event_queue.py                 # Event queuing and retry logic
├── test_event_dispatch_delete.py       # Inbound delete event dispatch
├── test_event_dispatch_rename.py       # Inbound rename event dispatch
├── test_event_dispatch_symlink.py      # Inbound symlink event dispatch
├── test_file_event_handler_base.py     # Base file event handler logic
├── test_lan_peer.py                    # LAN peer model and logic
├── test_lan_peer_manager.py            # LAN peer manager
├── test_lan_handler_advanced.py        # Advanced LAN handler scenarios
├── test_lan_delta.py                   # LAN delta transfer
├── test_lan_network_tuning.py          # LAN network tuning parameters
├── test_lan_sync.py                    # LAN sync operations
├── test_lan_server_endpoints_advanced.py  # LAN server: advanced endpoints
├── test_lan_server_endpoints_base.py      # LAN server: base endpoints
├── test_lan_server_endpoints_mutations.py # LAN server: mutation endpoints
├── test_lan_server_https.py            # LAN server HTTPS setup
├── test_lan_server_security.py         # LAN server security
├── test_lan_server_thread_cap.py       # LAN server thread cap
├── test_lan_service_listener.py        # LAN service discovery listener
├── test_lan_utils.py                   # LAN utility functions
├── test_lan_websocket_handle.py        # LAN WebSocket handle
├── test_lan_websocket_handler.py       # LAN WebSocket handler
├── test_lan_websocket_server.py        # LAN WebSocket server
├── test_lan_websocket_slave.py         # LAN WebSocket slave client
├── test_config.py                      # Configuration loading and CLI arg parsing
├── test_compression.py                 # Zstandard compression/decompression
├── test_cli.py                         # CLI argument parsing
├── test_exceptions.py                  # Exception hierarchy
├── test_main_logic_deletion_risk.py    # Core sync orchestration: deletion risk
├── test_main_logic_lan.py              # Core sync orchestration: LAN mode
├── test_main_logic_preview.py          # Core sync orchestration: preview mode
├── test_main_logic_sync.py             # Core sync orchestration: sync flow
├── test_logger.py                      # Logging configuration
├── test_edge_cases.py                  # Edge cases and corner scenarios
├── test_stdin_commands.py              # Stdin protocol commands
├── test_coverage_gaps.py               # Coverage gap fillers
├── test_install_script.py              # install.sh script tests
├── test_models_file.py                 # File model wrapper
├── test_models_dir.py                  # Directory model wrapper
├── test_models_event.py                # Event model wrapper
├── test_models_sync_action.py          # SyncAction model wrapper
├── test_models_symlink.py              # Symlink model wrapper
├── test_security_inbound.py            # Inbound path validation
├── test_pinning.py                     # TOFU certificate pinning
├── test_ssl_utils.py                   # SSL utilities
├── test_retry.py                       # Retry logic
├── test_utils.py                       # General utilities
└── test_delta_transfer.py              # Delta transfer (rsync-style)
```

## Fixtures

`conftest.py` provides shared fixtures in three layers:

### Global autouse fixtures

| Fixture | Scope | Purpose |
|---------|-------|---------|
| `reset_sync_state_stores` | function | Clears the in‑memory `sync_state._stores` before each test so a previous test's `LOCAL_BUCKET` does not leak into the current one. |
| `deterministic_cli_output` | function | Forces plain (uncoloured) CLI output so assertions on `result.output` are stable across CI and local runs. |

### Black‑Box harness fixtures (server‑mode)

Used by `test_bb_server_remote_sync.py`, `test_bb_server_continuous_sync.py`,
`test_bb_server_safety_guards.py` and `test_bb_server_cli_surface.py`.

| Fixture | Scope | Purpose |
|---------|-------|---------|
| `live_server` | module | Starts the **real `hsync-server`** as a separate subprocess on loopback HTTPS with a self‑signed certificate; readiness is probed via `GET /health`. Runs with isolated `HSYNC_BUCKETS`/`HSYNC_STATE_DIR`. Yields `(base_url, api_key)`. Disables rate‑limiting and docs. |
| `bb_bucket` | function | Creates a uniquely‑named bucket on the live server for one test and deletes it afterwards. |
| `local_dir` | function | Creates a temp directory used as the client's local sync path. |
| `run_sync_once` | function | Returns a callable that runs a **one‑shot** client sync via `CliRunner` (Typer) with `--yes --insecure --min-free-disk-space 0`. Wraps the invocation in `_ClientGlobalsGuard`. |
| `run_cli` | function | Like `run_sync_once` but adds no safety flags of its own — used by tests that must control `--yes` / `--sync-direction` / `--min-free-disk-space` precisely. |
| `watch_sync` | function | Returns a callable that starts the client in **continuous mode** as a subprocess. The caller receives the `Popen` handle and must terminate it. |
| `bb_server_upload` | function | Helper to upload a file directly to the live server (skips the client). |
| `bb_server_files` | function | Helper to list files on the live server for a bucket. |

### Unit / integration test fixtures

| Fixture | Purpose |
|---------|---------|
| `tmp_path` (pytest built‑in) | Temporary directory for isolated test data. |
| `sample_files` | Pre‑built file tree with known content and hashes. |
| `mock_server` | Mock HTTP server (responses library) for API tests. |
| `mock_websocket` | Mock WebSocket connection for event tests. |
| `test_config` | Minimal configuration for test isolation. |

### Using fixtures

```python
def test_file_comparison(tmp_path, sample_files):
    """Compare local and remote file lists."""
    local = scan_directory(tmp_path)
    remote = create_remote_list(sample_files)
    actions = get_sync_actions(local, remote)
    assert len(actions) == expected_count
```

### `_ClientGlobalsGuard`

Every `run_sync_once` / `run_cli` call wraps the client invocation in
`_ClientGlobalsGuard`. It snapshots the process‑wide `CONFIG` dict,
the active `IgnoreMatcher`, the event emitter and debug mode before the
run and restores them after. This prevents the black‑box suite from
perturbing the existing (mock‑based) tests that share the same process.

## Writing new tests

### Sync engine tests

Test the core comparison logic with controlled file sets:

1. Create a known local file tree with `tmp_path`
2. Create a known remote file list (dictionary)
3. Call `get_sync_actions()` or the relevant sync function
4. Assert the correct actions are generated:
   - `SyncActionName.upload` for local-only or newer files
   - `SyncActionName.download` for remote-only or newer files
   - `SyncActionName.delete` for files only on the remote
   - `SyncActionName.nothing` for identical files

### Conflict resolution tests

Test the three-way merge with known hash inputs:

```python
def test_resolve_three_way_conflict():
    # Both changed since base
    result = resolve_three_way(
        local_hash="abc123",  # local changed
        remote_hash="def456",  # remote changed
        base_hash="xyz000",  # base is different from both
    )
    assert result == SyncDecision.conflict


def test_resolve_three_way_upload():
    # Only local changed
    result = resolve_three_way(
        local_hash="abc123",  # local changed
        remote_hash="xyz000",  # remote unchanged (same as base)
        base_hash="xyz000",
    )
    assert result == SyncDecision.upload
```

### Compression tests

Test Zstandard round-trip with various file types:

```python
def test_compression_roundtrip():
    data = b"Hello, World!" * 1000
    compressed = compress_data(data)
    decompressed = decompress_data(compressed)
    assert decompressed == data
    # Verify compression actually reduced size
    assert len(compressed) < len(data)
```

## Mocking the server

Most client tests should **not** require a running HSYNC Server. Use mocks:

### HTTP mocking with `responses`

```python
import responses


@responses.activate
def test_fetch_remote_files():
    responses.add(
        responses.GET,
        "https://server:9753/api/v1/buckets/test/files",
        json={"files": [{"name": "hello.txt", "sha256sum": "abc123", "size": 13}]},
        status=200,
    )
    files = fetch_remote_files("https://server:9753", "test")
    assert len(files) == 1
```

### WebSocket mocking

```python
from unittest.mock import AsyncMock, patch


@patch("hsync_client.websocket_handler.WebSocketHandler")
async def test_websocket_event(mock_ws):
    handler = mock_ws.return_value
    handler.receive.return_value = {"name": "added", "src": {...}}
    # Test event handling logic
```

## LAN mode testing

LAN mode tests require special setup because they involve mDNS discovery and
peer-to-peer connections:

1. Use `pytest-asyncio` for async LAN operations
2. Mock `zeroconf` service discovery to avoid needing a real mDNS daemon
3. Test master election logic independently with known peer lists
4. Test LAN server startup/shutdown lifecycle
5. Test fingerprint pinning with known certificate hashes

```python
@pytest.mark.asyncio
async def test_master_election():
    peers = [
        {"cid": "PeerCID1234567890abcde", "sync_direction": "bidirectional"},
        {"cid": "PeerCID2234567890abcde", "sync_direction": "upload-only"},
    ]
    master = elect_master(peers)
    assert master["cid"] == "PeerCID1234567890abcde"  # bidirectional preferred
```

## Black‑box tests (`test_bb_*.py`)

The `test_bb_*.py` files are **behavioural, use‑case‑driven** tests that
drive the **real** client through its **public Typer CLI** against real
infrastructure. No internal logic is mocked — only the filesystem (temp
directories) and the API key are test fixtures.

### Server‑mode Black‑Box tests

The server‑mode black‑box tests exercise the client against a real
`hsync-server` subprocess over loopback HTTPS. **They are disabled by
default** (opt‑in via ``HSYNC_BB_SERVER=1``), because they spin up a
live server and require a sibling ``hsync-server`` installation.

`hsync-server` is an **optional** Poetry group (`blackbox`) — it is not
installed by `poetry install` (which keeps CI, where the sibling repo is
absent, working). Install it before running the server‑mode black‑box
suite:

```bash
poetry install --with blackbox
```

| File | What it covers |
|------|---------------|
| `test_bb_server_remote_sync.py` | Full sync cycle: upload, download, deletion, rename, directory sync, Unicode filenames, feature flags (compression, delta, autotune), dry‑run, ignore patterns. |
| `test_bb_server_continuous_sync.py` | Continuous mode (default daemon): file creation, modification, and deletion are propagated while the client runs continuously. |
| `test_bb_server_offline_changes.py` | Continuous-mode resilience: local changes made during a server outage are reconciled after reconnection. |
| `test_bb_server_safety_guards.py` | Mass‑deletion guard, mass‑deletion percent guard, insufficient disk‑space abort, download‑only interactive prompts. |
| `test_bb_server_cli_surface.py` | CLI help output, error messages, and surface‑level argument validation. |
| `test_bb_server_corrected_mtime.py` | ADR-0023 corrected‑mtime tie‑break: the corrected mtime decides conflicts, never the raw clock; the losing content is preserved as a conflicted copy. |

To opt in:
```bash
HSYNC_BB_SERVER=1 .venv/bin/python -m pytest tests/test_bb_server_*
```

### LAN‑mode Black‑Box tests

Tests the LAN peer‑to‑peer sync use case: two real client instances that
discover each other via mDNS/zeroconf, elect a master, and sync over
HTTPS/WSS with TOFU certificate pinning. The suite is split across twelve
files:

| File | What it covers |
|------|---------------|
| `test_bb_lan_core.py` | Core LAN sync cycle: discovery, election, full sync. |
| `test_bb_lan_advanced.py` | Advanced LAN scenarios (edge cases, complex file trees). |
| `test_bb_lan_auth_direction.py` | LAN authentication and sync‑direction enforcement. |
| `test_bb_lan_delta.py` | LAN delta transfer over the public REST surface (reconstruct, 404s). |
| `test_bb_lan_delta_apply.py` | LAN delta‑apply (upload‑side delta) over the public REST surface (reconstruct, missing‑base 404). |
| `test_bb_lan_features.py` | LAN feature flags (compression, delta, autotune). |
| `test_bb_lan_files.py` | LAN file operations (create, modify, delete, rename). |
| `test_bb_lan_resumable.py` | LAN resumable upload over the public REST surface. |
| `test_bb_lan_security.py` | LAN security (TOFU pinning, certificate validation). |
| `test_bb_lan_upload_negatives.py` | LAN upload rejection paths over the public REST surface (checksum, traversal, engine artifacts, compressed+offset). |
| `test_bb_lan_websocket_relay.py` | LAN WebSocket relay, fan‑out, and handshake protocol. |
| `test_bb_lan_corrected_mtime.py` | ADR-0023 corrected‑mtime tie‑break in LAN mode: divergent edits converge on the newer corrected mtime with loser preservation; sync completes when NTP is unreachable. |

**These tests are disabled by default** because they are inherently
environment‑sensitive (multicast on loopback, self‑signed certificate
generation, election timing, multi‑process coordination).

To opt in:
```bash
HSYNC_BB_LAN=1 .venv/bin/python -m pytest --maxfail=3 tests/test_bb_lan_*
```

Run the LAN group **isolated** from the remote/server black-box suite: do not
combine the two in the same `pytest` invocation with `-x`, or a single LAN
timeout will abort the stable remote suite. `--maxfail` keeps one flaky LAN
file from masking the others.

The LAN tests start two subprocesses with the `lan` subcommand (and
`--force-master` / `--never-master`) via the `_lan_start` helper in
`tests/lan_helpers.py`. They use the `_HSYNC_COMMON_DIR`
constant to inject the `hsync-common` source directory into
`PYTHONPATH` so the subprocess can import shared models.

**Readiness contract** (how the harness knows a process is ready):

| Process | Readiness signal |
|---------|------------------|
| `hsync-server` subprocess | Poll `GET /health` until 200 (real readiness probe). |
| Client in continuous mode | `HSYNC_READY_FILE` PID file, written after the first completed sync cycle. |
| LAN master | `HSYNC_READY_FILE` PID file, written once its HTTP+WSS servers are up. |
| LAN slave | `HSYNC_READY_FILE` PID file, written after the initial sync. |

The ready file is written **atomically** (temp + rename) by the client and
is accepted by `_wait_for_client_ready` **only when its content equals the
expected subprocess PID** — stale files, PID reuse and foreign content are
never trusted, and the harness never deletes the file (the right process
overwrites it). Each instance gets its own ready file as a *sibling* of its
synced directory: a file inside the bucket would be synced to the peer
(`.hsync_ready` is not an engine-internal name and dotfiles are synced by
design).

**Isolation contract** (no dirty data across tests or on the host):

- Every LAN subprocess gets its own `HSYNC_DEVICE_DIR` (device ID + keypair)
  and `HSYNC_LAN_PIN_FILE` (TOFU pins) under the test's `tmp_path` — nothing
  ever touches the developer's real `~/.local/state/hsync`. Two peers in one
  test are *distinct devices* by construction (same host + same bucket +
  same device ID would be ignored as "self" in discovery). Restarting a peer
  on the same directory reuses its identity; deleting the `*.pem` files
  rotates the certificate while keeping the device ID (used by the
  fingerprint-changed test).
- Subprocess environments are scrubbed of every pre-existing `HSYNC_*`
  variable before test overrides are applied (`_lan_env`, `watch_sync`, and
  `_server_env` for the `hsync-server` subprocess).
- The in-process `tests/conftest.py` autouse fixture `_scrub_hsync_env`
  removes every `HSYNC_*` inherited from the developer's shell before each
  test (`monkeypatch.delenv` restores them on teardown), so the client/server
  configuration surface is hermetic: a shell-exported `HSYNC_API_KEY` /
  `HSYNC_MAX_CONCURRENT_TRANSFERS` / … can never leak into assertions such as
  `EnvSettings` defaults or `AUTOTUNE_MANAGED_KEYS`. Tests that set their own
  `HSYNC_*` (via `monkeypatch.setenv` or directly) still work because they run
  after the scrub.
- The `hsync-server` subprocess gets its own `HSYNC_BUCKETS` and
  `HSYNC_STATE_DIR` temp dirs (`_server_env`), so neither bucket content nor
  the shared `syncstate.db` account store is reused across tests — a shared
  `syncstate.db` caused startup `SQLITE_BUSY`/schema collisions when several
  server modules ran in one session.
- The per-directory instance lock (`hsync_client.identity.InstanceLock`)
  makes two live clients on one synced directory impossible; its lock files
  live in a per-test `HSYNC_LOCKS_DIR`.
- `bb_bucket` deletes its bucket after each test, so the module-scoped
  server never accumulates stale buckets.
- The `tests/conftest.py` autouse fixture `_kill_orphan_hsync_processes`
  terminates orphaned `hsync_client` subprocesses (direct children of the
  pytest PID, via `pgrep -P`) after each test, preventing stale mDNS peers
  from cascading into subsequent tests.

**Known limitations**:
- The tests are timing-sensitive by design (multi‑process + mDNS + election
  handshake). A single timeout or port conflict can cause a cascade of
  failures.
- Subprocess stdout is captured via `subprocess.PIPE` (stderr merged into
  it; loguru logs to stdout) and polled for log markers (`MASTER`,
  `Pinned LAN master certificate`, etc.) with `_wait_for_text_in_stderr`,
  which also drains the final output of fast-exiting processes.
- Do **not** combine LAN and remote/server black-box tests in the same
  `pytest` invocation with `-x`: a LAN timeout will abort the stable
  remote suite. Run the two groups separately, or use `--maxfail`.

### Shared Black‑Box constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `BB_API_KEY` | `test-bb-api-key-0123456789abcdef` | API key used by the live server and the client. |
| `BB_CLIENT_CID` | `TestClientX1234567890` | Well‑formed 21‑character client ID for server uploads. |

## Stress suites

`tests/test_bb_server_stress.py` and `tests/test_bb_lan_stress.py` exercise the
server-mode and LAN P2P paths under burst load (many files created in one
wave, then mutated, deleted and re-verified). They are **opt-in black-box
suites** gated by `HSYNC_BB_STRESS=1`, with bounded scale by default so
`make ci` stays fast:

```bash
# Bounded run (default knobs: 300 files / 50 dirs / 1 iteration)
cd hsync-client && HSYNC_BB_STRESS=1 .venv/bin/python -m pytest -q tests/test_bb_server_stress.py
cd hsync-client && HSYNC_BB_STRESS=1 .venv/bin/python -m pytest -q tests/test_bb_lan_stress.py

# Full-scale benchmark (long)
make -C .. test-stress
```

All knobs are `HSYNC_BB_STRESS_*` environment variables
(`FILES`, `DIRS`, `ITERATIONS`, `WAVES`, `POLL_INTERVAL`, `TIMEOUT`, …); the
single source of truth for syntax, defaults and the verification model is
[docs/internal/stress-test-plan.md](stress-test-plan.md).

Notes:

- **Server mode runs in-process by default** (``CliRunner``) so timings stay
  comparable; set `HSYNC_BB_STRESS_SUBPROCESS=1` to drive the client as a real
  wall-clock subprocess instead (server mode only — the LAN suite already runs
  real peer subprocesses).
- At the end of a stress session the suite prints an **aggregate table with
  median + min/max per phase** (and per concurrency value for the sweep),
  computed across the `ITERATIONS` runs.
- The harness generates the tree on the master through the *public contract
  only* (filesystem events are the input), then compares the replica against a
  manifest computed locally — no client internals are imported.
- LAN runs are timing-sensitive like the rest of the LAN BB suite (see above);
  run them separately from the server-mode group and prefer `--maxfail`.
- Peer logs are captured on per-test log **files** (not pipes) inside the
  pytest tmp dir, so verbose peers can never block the harness on a full pipe.

**Defects surfaced by full-scale runs (fixed)**:

- *Concurrent zstd decompression corrupted streams.* A single shared
  `ZstdDecompressor` in `hsync-common` was used by all parallel transfer
  threads; under LAN burst load this produced `Unknown frame descriptor` /
  `Data corrupted` errors and, with zstandard ≥ 0.25, could crash the
  interpreter. Fixed by making decompressors per-thread (`threading.local()`).
  Regression: `hsync-common/tests/test_compression.py::TestThreadSafety`.
- *Directory permissions never converged on the LAN.* Directories created by
  a download cycle's `mkdir(parents=True)` inherit the process umask (e.g.
  `0o775` instead of `0o750`), and the one-shot `converge_directory_permissions`
  pass ran before the transfers and skipped not-yet-existing directories — so
  burst-created directories stayed divergent. Fixed by moving the converge
  pass after `_execute_parallel_transfers` and gating it for LAN mode.
  Regression: `hsync-client/tests/test_permissions_dirs.py`.
- *LAN download serving hashed every file 2–3 times.* Fixed with a bounded
  checksum cache on the LAN master (invalidated on any stat identity change).
  Regression: `hsync-client/tests/test_lan_server_checksum_cache.py`.

These fixes took the LAN full-scale suite (6000 files / 400 dirs / ~1.5 GB)
from a 600 s convergence timeout with 163 mismatches to `1 passed in ~138 s`
with 0 mismatches (burst ≈ 67 file/s, mutation wave ≈ 147 file/s).

**Throughput follow-ups (fixed)** — attribution via
`scripts/benchmark_lan_transfers.py` + a throwaway HTTPS micro-bench showed
the LAN master serves 200–600 req/s, so the burst ceiling was client-side:

- *The slave's WSS receive loop serialized downloads.* Every inbound file
  event was `await`ed inline (~10–26 ms each), capping a burst at ~60
  files/s. Inbound file downloads are now fanned out per-path-chained,
  bounded by `MAX_CONCURRENT_TRANSFERS` (`event_dispatch._spawn_file_download`).
  Regression: `tests/test_event_dispatch_file_fanout.py`.
- *Every sync cycle re-hashed the whole local tree.* The SHA-256 cache is now
  keyed on the nanosecond stat identity (mtime/ctime/size) in `hsync-common`
  and is no longer cleared per cycle: continuous-mode resyncs are
  metadata-only. Regressions:
  `hsync-common/tests/test_models_file.py::TestFileGetSha256Cache`,
  `hsync-client/tests/test_sync_local.py::TestSyncPreservesSha256Cache`.
- *An inventory ACK timeout used to kill the WSS channel.* The publisher now
  stops publishing on that connection without closing the socket, so the
  event flow survives a backlogged master receiver.
  Regression: `tests/test_lan_inventory.py::test_ack_timeout_stops_publisher_without_closing_channel`.
- *Synchronous log sinks throttled the master's watcher thread.* Each
  `[LOCAL CREATE]`-style line paid format + write + flush on the emitting
  thread (measured ~2.5× slower event drain in an in-process replica).
  Both sinks (`--log-file` and stdout) are now asynchronous (`enqueue=True`):
  identical lines, written by a dedicated loguru thread; readers that need
  the file immediately wait via `logger.complete()`.
  Regression: `tests/test_logger.py::TestSetupLogging::test_setup_with_logfile`.
- *The master's own downloads flooded its watcher.* Every file the LAN master
  serves is opened and closed without a write, and the default inotify mask
  parsed/queued/dispatched those no-op events (measured ~7× more events
  parsed, ~6× slower dispatch with downloads running — the actual cause of
  the ~60 files/s burst ceiling). The inotify watch now excludes
  `FileOpenedEvent`/`FileClosedNoWriteEvent` via an event filter (the
  file-ready `FileClosedEvent` stays); the replica drains ~2.7× faster under
  the same download load.
  Regression: `tests/test_watcher_start.py::TestInotifyEventFilter`.

### Server-mode black-box suite (first full run)

The 83 server-mode black-box tests (`remote_sync`, `continuous_sync`,
`cli_surface`, `safety_guards`, `corrected_mtime`, `offline_changes`) are gated
behind `HSYNC_BB_SERVER=1` and had never been executed in this workspace. First
full run: **80 passed, 1 failed** — and the failure was a real, user-visible
defect introduced by the asynchronous log sinks:

- *The instance-lock refusal printed nothing.* A second client on the same
  directory exited non-zero (correct) and logged the reason, but the record was
  still queued on loguru's writer thread when the process left — loguru reported
  `ValueError: I/O operation on closed file` from its queued writer and the
  user saw a bare failure with no explanation. The test invokes the CLI
  in-process through `CliRunner`, whose captured stream is torn down before the
  writer flushes, which made the race deterministic in CI.
  Fixed with `logger.flush_logs()` (a blocking, best-effort `logger.complete()`
  wrapper) drained by every fatal exit: the lock refusal itself, the one-shot
  run's `finally` (before *and* after the shutdown steps, so the diagnostic
  cannot be lost if they fail) and `cli.run` as the process-level net.
  Regressions: `tests/test_logger.py::TestSetupLogging::test_flush_logs_drains_the_async_sink`,
  `...::test_flush_logs_never_raises`, plus the end-to-end
  `tests/test_bb_server_safety_guards.py::TestInstanceLock::test_second_process_on_same_directory_refused`.
  The first attempt wired the flush through a module-level import in
  `hsync_client/cli.py`, which the **startup-import guard**
  (`tests/test_startup_imports.py`, `FORBIDDEN` list) rejected: `import
  hsync_client.cli` must not pull `loguru`/`asyncio`, because `--version` and
  `--help` only need the argparse surface. `cli.run` therefore drains
  **lazily and only when `hsync_client.logger` is already in `sys.modules`** —
  a good example of an existing guard catching a performance regression that no
  behavioural test would have noticed.

### Stress-found correctness defects (fixed)

- *The per-cycle leftover sweep deleted `.part` files of live transfers.*
  `sync._cleanup_partial_files` walks the whole bucket at the start of every
  cycle to clear what a *previous* process left behind, but inbound downloads
  run **outside** the cycle that owns the sweep (LAN fan-out, remote event
  loop), so a RESYNC / reconnect landing mid-transfer removed the artifact a
  stream was still writing. The writer keeps a valid descriptor, so the failure
  surfaced only at the final atomic rename — logged as "Error saving file",
  with no retry and no transfer-failure counted, leaving the path unsynced until
  the next cycle (up to `RESYNC`, default 300 s). Writers now register their
  artifacts in a lock-guarded registry (`utils.register_inflight_partial`) and
  the sweep skips them.
  Regression: `tests/test_transfer_download.py::TestDownloadFileResume::test_inflight_partial_survives_cycle_cleanup`
  (fails without the fix with exactly that log sequence).
- *A stale delete could destroy a re-created file on **both** peers.* The delete
  coalescing window (`DELETE_COALESCE_WINDOW`) can outlive the path it refers
  to: `rm <f>` followed by an immediate rewrite of the same name (the atomic
  pattern the mutation wave uses) left the delete queued while the fresh file
  already existed. The flush then broadcast a delete for a live file, the peer
  removed it, and the peer's own delete echo came back through the master's
  DELETE API — removing the source as well. Both flushes now re-check existence
  at flush time and drop the stale entry (`split_stale_deletes`); the slave also
  clears the tombstone recorded when buffering. Regressions:
  `tests/test_file_event_handler_base.py::TestFileEventHandler::test_stale_delete_dropped_when_path_recreated_inside_window`,
  `...::test_stale_delete_does_not_swallow_live_descendants`,
  `tests/test_lan_websocket_slave.py::TestSlaveFileEventHandler::test_stale_delete_dropped_when_path_recreated_inside_window`.

### Measured remaining limit (next target, not fixed)

The full-scale LAN burst drains at **~90 files/s** and no single hot function
explains it. The hypotheses below were each measured and **eliminated**:

| Candidate | Measurement | Verdict |
| --- | --- | --- |
| Master's HTTP capacity | 1 vs 5 vs 10 **process-isolated** clients, 70 KiB: ~2900/3300/1180/820 req/s | Not the limit (field needs ~450/s) |
| Master's event origination | 6400 `LOCAL CREATE` logged in ~9 s (711 ev/s, peak 1331/s) | Not the limit |
| Master checksum cache lock | held only for the dict op, never during hashing | Not the limit |
| Slave watchdog echo per downloaded file | `on_created`/`on_closed`/`on_moved` all test `is_receiving` **before** any hashing; only 329 `LOCAL CREATE` for 6779 downloads | Not the limit |
| Inventory publisher | `upsert` = dict insert + `call_soon_threadsafe` | Not the limit |
| Per-event handler CPU | hash 0.09 ms + SQLite `note_local_edit`/`flush` 0.03 ms (tmpfs) | Not the limit |

**What the measurements do show is negative scaling on both peers.** In an
in-process replica of the slave's apply path (`sync.update_file`, master in a
separate process, destination on tmpfs), concurrency makes things *worse*:

```
workers=1:  501.6 files/s  per-call mean  2.0 ms  median 1.7 ms
workers=5:  269.0 files/s  per-call mean 18.5 ms  median 12.4 ms   (eff. conc 4.97)
```

and the master degrades the same way under concurrent clients even for a
trivial endpoint (`/health`: 9186 → 1708 req/s in-process; 6800 → ~4100 req/s
with process-isolated clients), while heavy 70 KiB downloads degrade least
because hashlib/zstd release the GIL. `sys._is_gil_enabled()` is **True** in
this environment (CPython 3.14.4, standard build), so this is the classic GIL
convoy: many threads doing short CPU bursts share one interpreter lock, and
the per-request latency inflates faster than the parallelism grows.

Two consequences worth recording before anyone "optimises" the knobs:

- **Raising `MAX_CONCURRENT_TRANSFERS` is counterproductive** on this workload
  (1 worker already beats 5); the limit is not a queue-depth problem.
- Throughput must come from **less per-file Python work** (fewer statements,
  fewer locks touched, fewer encode/decode round trips per file), not from more
  threads or processes on the same host.

The gap between the replica (269 files/s) and the field (~90 files/s) is *not*
attributable to any component in isolation: in the field the master runs its
own watcher over the same 6000 files, serves the downloads and drives the WSS
sends while the slave applies them, and the two processes share the host's
cores. Reproducing it needs a profile of **both** peers during one real run
(and ideally two hosts); the single-component benches above cannot capture it.

## Cross-project testing

The client depends on `hsync-common` models. When changing models in
`hsync-common`, always run both test suites:

```bash
# From hsync-common
cd ../hsync-common && .venv/bin/python -m pytest

# From hsync-client
cd ../hsync-client && .venv/bin/python -m pytest
```

Model changes that break the client are caught by sync engine tests and
compression tests.

## See also

- [CONTRIBUTING.md](../../CONTRIBUTING.md) — development setup and workflow.
- [Developer Guide](developer.md) — internal implementation details.
- [Sync Flow](../sync-flow.md) — client-side sync flow.
