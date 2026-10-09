# HSYNC Client — Testing Guide

This guide covers the test suite for the HSYNC Client: structure, how to run
tests, how to write new tests, and cross-project testing with `hsync-common`.

> **Terminology:** in this guide **LAN** denotes the p2p test lane (the black-box
> `test_bb_p2p_*` suite, which runs the **mDNS** discovery plan on loopback). p2p
> mode also supports the **bootnode** discovery plan; those tests do not exercise
> it.

> For development setup, see [CONTRIBUTING.md](../../CONTRIBUTING.md).

## Table of Contents

- [Running tests](#running-tests)
- [Test structure](#test-structure)
- [Fixtures](#fixtures)
- [Writing new tests](#writing-new-tests)
- [Mocking the server](#mocking-the-server)
- [p2p mode testing](#p2p-mode-testing)
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

> For p2p black-box tests, prefer `--maxfail=N` over `-x`: a single mDNS
> election timeout is expected intermittently and should not abort the other
> independent p2p files. See [p2p‑mode Black‑Box tests](#p2p-mode-black-box-tests).

## Test structure

```
tests/
├── __init__.py
├── conftest.py                         # Shared fixtures and test configuration
│                                          (autouse fixtures, Black‑Box harness,
│                                           live_server, ClientGlobalsGuard, …)
├── p2p_helpers.py                      # Shared p2p test helpers
├── test_autotune.py                    # Autotune concurrency heuristics
├── test_bb_server_remote_sync.py       # Black‑Box: client‑server sync
├── test_bb_server_continuous_sync.py   # Black‑Box: continuous sync
├── test_bb_server_offline_changes.py   # Black‑Box: offline change reconciliation
├── test_bb_server_safety_guards.py     # Black‑Box: mass‑delete guard, disk‑space
├── test_bb_server_cli_surface.py       # Black‑Box: CLI help / error messages
├── test_bb_server_corrected_mtime.py   # Black‑Box: ADR-0023 tie‑break (server mode)
├── test_bb_p2p_advanced.py             # Black‑Box: p2p advanced scenarios
├── test_bb_p2p_auth_direction.py       # Black‑Box: p2p auth and sync direction
├── test_bb_p2p_core.py                 # Black‑Box: p2p core sync cycle
├── test_bb_p2p_delta.py                # Black‑Box: p2p delta transfer (REST)
├── test_bb_p2p_delta_apply.py          # Black‑Box: p2p delta‑apply upload (REST)
├── test_bb_p2p_features.py             # Black‑Box: p2p feature flags
├── test_bb_p2p_files.py                # Black‑Box: p2p file operations
├── test_bb_p2p_resumable.py            # Black‑Box: p2p resumable upload (REST)
├── test_bb_p2p_security.py             # Black‑Box: p2p security (TOFU, certs)
├── test_bb_security.py                 # Black‑Box: p2p master pentest probes (opt‑in)
├── test_bb_p2p_upload_negatives.py     # Black‑Box: p2p upload rejection paths (REST)
├── test_bb_p2p_websocket_relay.py      # Black‑Box: p2p WebSocket relay & handshake
├── test_bb_p2p_corrected_mtime.py      # Black‑Box: ADR-0023 tie‑break (p2p mode)
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
├── test_p2p_peer.py                    # p2p peer model and logic
├── test_p2p_peer_manager.py            # p2p peer manager
├── test_p2p_handler_advanced.py        # Advanced LAN handler scenarios
├── test_p2p_delta.py                   # LAN delta transfer
├── test_p2p_network_tuning.py          # p2p network tuning parameters
├── test_p2p_sync.py                    # LAN sync operations
├── test_p2p_server_endpoints_advanced.py  # LAN server: advanced endpoints
├── test_p2p_server_endpoints_base.py      # LAN server: base endpoints
├── test_p2p_server_endpoints_mutations.py # LAN server: mutation endpoints
├── test_p2p_server_https.py            # LAN server HTTPS setup
├── test_p2p_server_security.py         # LAN server security
├── test_p2p_server_thread_cap.py       # LAN server thread cap
├── test_p2p_service_listener.py        # LAN service discovery listener
├── test_p2p_utils.py                   # LAN utility functions
├── test_p2p_websocket_handle.py        # LAN WebSocket handle
├── test_p2p_websocket_handler.py       # LAN WebSocket handler
├── test_p2p_websocket_server.py        # LAN WebSocket server
├── test_p2p_websocket_slave.py         # LAN WebSocket slave client
├── test_config.py                      # Configuration loading and CLI arg parsing
├── test_compression.py                 # Zstandard compression/decompression
├── test_cli.py                         # CLI argument parsing
├── test_exceptions.py                  # Exception hierarchy
├── test_main_logic_deletion_risk.py    # Core sync orchestration: deletion risk
├── test_main_logic_p2p.py              # Core sync orchestration: p2p mode
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
| `run_sync_once` | function | Returns a callable that runs a **one‑shot** client sync via `CliRunner` (Typer) with `--allow-deletions --insecure --min-free-disk-space 0`. Wraps the invocation in `_ClientGlobalsGuard`. |
| `run_cli` | function | Like `run_sync_once` but adds no safety flags of its own — used by tests that must control `--allow-deletions` / `--sync-direction` / `--min-free-disk-space` precisely. |
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

## p2p mode testing

p2p mode tests require special setup because they involve mDNS discovery and
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
| `test_bb_server_remote_sync.py` | Full sync cycle: upload, download, deletion, rename, directory sync, Unicode filenames, feature flags (compression, delta, autotune), dry‑run, ignore patterns, bucket isolation. |
| `test_bb_server_continuous_sync.py` | Continuous mode (default daemon): file creation, modification, and deletion are propagated while the client runs continuously. |
| `test_bb_server_offline_changes.py` | Continuous-mode resilience: local changes made during a server outage are reconciled after reconnection. |
| `test_bb_server_safety_guards.py` | Mass‑deletion guard, mass‑deletion percent guard, insufficient disk‑space abort, download‑only interactive prompts. |
| `test_bb_server_cli_surface.py` | CLI help output, error messages, and surface‑level argument validation. |
| `test_bb_server_corrected_mtime.py` | ADR-0023 corrected‑mtime tie‑break: the corrected mtime decides conflicts, never the raw clock; the losing content is preserved as a conflicted copy. |

To opt in:
```bash
HSYNC_BB_SERVER=1 .venv/bin/python -m pytest tests/test_bb_server_*
```

### p2p‑mode Black‑Box tests

Tests the p2p peer‑to‑peer sync use case: two real client instances that
discover each other via mDNS/zeroconf, elect a master, and sync over
HTTPS/WSS with TOFU certificate pinning. The suite is split across twelve
files:

| File | What it covers |
|------|---------------|
| `test_bb_p2p_core.py` | Core LAN sync cycle: discovery, election, full sync. |
| `test_bb_p2p_advanced.py` | Advanced LAN scenarios (edge cases, complex file trees). |
| `test_bb_p2p_auth_direction.py` | LAN authentication and sync‑direction enforcement. |
| `test_bb_p2p_delta.py` | LAN delta transfer over the public REST surface (reconstruct, 404s). |
| `test_bb_p2p_delta_apply.py` | LAN delta‑apply (upload‑side delta) over the public REST surface (reconstruct, missing‑base 404). |
| `test_bb_p2p_features.py` | LAN feature flags (compression, delta, autotune), group filtering and bucket isolation. |
| `test_bb_p2p_files.py` | LAN file operations (create, modify, delete, rename). |
| `test_bb_p2p_resumable.py` | LAN resumable upload and HTTP Range (206/416) download over the public REST surface. |
| `test_bb_p2p_security.py` | p2p security (TOFU pinning, certificate validation). |
| `test_bb_p2p_upload_negatives.py` | p2p upload rejection paths over the public REST surface (checksum, traversal, engine artifacts, compressed+offset). |
| `test_bb_p2p_websocket_relay.py` | p2p WebSocket relay, fan‑out, and handshake protocol. |
| `test_bb_p2p_corrected_mtime.py` | ADR-0023 corrected‑mtime tie‑break in p2p mode: divergent edits converge on the newer corrected mtime with loser preservation; sync completes when NTP is unreachable. |

| `test_bb_p2p_bootnode_client.py` | the client's `BootnodeClient` against the **real** `hsync-bootnode` (register → ack/STUN-echo, query relay, refresh, leave); skips when the sibling bootnode is not built. |
| `test_bb_p2p_bootnode_discovery.py` | two clients discover **and sync through** a loopback bootnode (`BOOTNODE 127.0.0.1 --port <p> --insecure`); also holds the opt-in real-NAT check. |

**These tests are disabled by default** because they are inherently
environment‑sensitive (multicast on loopback, self‑signed certificate
generation, election timing, multi‑process coordination).

To opt in:
```bash
HSYNC_BB_P2P=1 .venv/bin/python -m pytest --maxfail=3 tests/test_bb_p2p_*
```

Set `HSYNC_BB_P2P_NAT=1` to also run the opt-in real-NAT checks (they need a
live UPnP/IGD and are skipped otherwise); CI stays loopback-only.

Run the p2p group **isolated** from the remote/server black-box suite: do not
combine the two in the same `pytest` invocation with `-x`, or a single LAN
timeout will abort the stable remote suite. `--maxfail` keeps one flaky LAN
file from masking the others.

The LAN tests start two subprocesses with the `p2p` subcommand (and
`--force-master` / `--never-master`) via the `_p2p_start` helper in
`tests/p2p_helpers.py`. They use the `_HSYNC_COMMON_DIR`
constant to inject the `hsync-common` source directory into
`PYTHONPATH` so the subprocess can import shared models.

**Readiness contract** (how the harness knows a process is ready):

| Process | Readiness signal |
|---------|------------------|
| `hsync-server` subprocess | Poll `GET /health` until 200 (real readiness probe). |
| Client in continuous mode | `HSYNC_READY_FILE` PID file, written after the first completed sync cycle. |
| p2p master | `HSYNC_READY_FILE` PID file, written once its HTTP+WSS servers are up. |
| p2p slave | `HSYNC_READY_FILE` PID file, written after the initial sync. |

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
  and `HSYNC_P2P_PIN_FILE` (TOFU pins) under the test's `tmp_path` — nothing
  ever touches the developer's real `~/.local/state/hsync`. Two peers in one
  test are *distinct devices* by construction (same host + same bucket +
  same device ID would be ignored as "self" in discovery). Restarting a peer
  on the same directory reuses its identity; deleting the `*.pem` files
  rotates the certificate while keeping the device ID (used by the
  fingerprint-changed test).
- Subprocess environments are scrubbed of every pre-existing `HSYNC_*`
  variable before test overrides are applied (`_p2p_env`, `watch_sync`, and
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
  the shared account store is reused across tests — a shared
  the state DB caused startup `SQLITE_BUSY`/schema collisions when several
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
  `Pinned p2p master certificate`, etc.) with `_wait_for_text_in_stderr`,
  which also drains the final output of fast-exiting processes.
- Do **not** combine LAN and remote/server black-box tests in the same
  `pytest` invocation with `-x`: a LAN timeout will abort the stable
  remote suite. Run the two groups separately, or use `--maxfail`.

### Shared Black‑Box constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `BB_API_KEY` | `test-bb-api-key-0123456789abcdef` | API key used by the live server and the client. |
| `BB_CLIENT_CID` | `TestClientX1234567890` | Well‑formed 21‑character client ID for server uploads. |

## Security probes (black‑box pentest)

`tests/test_bb_security.py` drives the attack probes in `pentest/probes.py`
against an in‑process p2p master: authentication, path confinement, upload
integrity/DoS, tombstone gossip, WebSocket handshake and information
disclosure. The probes speak only the public HTTP/WS contract (no
`hsync_client` import), so the same code runs standalone against a live master
via `pentest/run_pentest.py`. Adversarial by nature, the suite is **opt‑in**:

```bash
# CI suite (in-process master)
cd hsync-client && HSYNC_BB_SECURITY=1 .venv/bin/python -m pytest -q tests/test_bb_pentest.py

# standalone against a running master
cd hsync-client && HSYNC_PENTEST_P2P_URL=https://127.0.0.1:8765 \
  HSYNC_PENTEST_P2P_KEY='<shared p2p key>' \
  HSYNC_PENTEST_P2P_BUCKET='<bucket>' \
  .venv/bin/python -m pentest.run_pentest
```

`HSYNC_PENTEST_HEAVY=1` also runs the heavy (~19 MB) tombstone‑oversize probe.
The full report and residual risks live in `pentest/SECURITY_FINDINGS.md`.

## Stress suites

`tests/test_bb_server_stress.py` and `tests/test_bb_p2p_stress.py` exercise the
server-mode and p2p paths under burst load (many files created in one
wave, then mutated, deleted and re-verified). They are **opt-in black-box
suites** gated by `HSYNC_BB_STRESS=1`, with bounded scale by default so
`make ci` stays fast:

```bash
# Bounded run (default knobs: 300 files / 50 dirs / 1 iteration)
cd hsync-client && HSYNC_BB_STRESS=1 .venv/bin/python -m pytest -q tests/test_bb_server_stress.py
cd hsync-client && HSYNC_BB_STRESS=1 .venv/bin/python -m pytest -q tests/test_bb_p2p_stress.py

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
  threads; under p2p burst load this produced `Unknown frame descriptor` /
  `Data corrupted` errors and, with zstandard ≥ 0.25, could crash the
  interpreter. Fixed by making decompressors per-thread (`threading.local()`).
  Regression: `hsync-common/tests/test_compression.py::TestThreadSafety`.
- *Directory permissions never converged on the LAN.* Directories created by
  a download cycle's `mkdir(parents=True)` inherit the process umask (e.g.
  `0o775` instead of `0o750`), and the one-shot `converge_directory_permissions`
  pass ran before the transfers and skipped not-yet-existing directories — so
  burst-created directories stayed divergent. Fixed by moving the converge
  pass after `_execute_parallel_transfers` and gating it for p2p mode.
  Regression: `hsync-client/tests/test_permissions_dirs.py`.
- *p2p download serving hashed every file 2–3 times.* Fixed with a bounded
  checksum cache on the p2p master (invalidated on any stat identity change).
  Regression: `hsync-client/tests/test_p2p_server_checksum_cache.py`.

These fixes took the LAN full-scale suite (6000 files / 400 dirs / ~1.5 GB)
from a 600 s convergence timeout with 163 mismatches to `1 passed in ~138 s`
with 0 mismatches (burst ≈ 67 file/s, mutation wave ≈ 147 file/s).

**Throughput follow-ups (fixed)** — attribution via
`scripts/benchmark_p2p_transfers.py` + a throwaway HTTPS micro-bench showed
the p2p master serves 200–600 req/s, so the burst ceiling was client-side:

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
  Regression: `tests/test_p2p_inventory.py::test_ack_timeout_stops_publisher_without_closing_channel`.
- *Synchronous log sinks throttled the master's watcher thread.* Each
  `[LOCAL CREATE]`-style line paid format + write + flush on the emitting
  thread (measured ~2.5× slower event drain in an in-process replica).
  Both sinks (`--log-file` and stdout) are now asynchronous (`enqueue=True`):
  identical lines, written by a dedicated loguru thread; readers that need
  the file immediately wait via `logger.complete()`.
  Regression: `tests/test_logger.py::TestSetupLogging::test_setup_with_logfile`.
- *The master's own downloads flooded its watcher.* Every file the p2p master
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

#### Server-mode stress baseline (2000 files / 300 dirs, cap 512 KiB)

`HSYNC_BB_STRESS=1 pytest tests/test_bb_server_stress.py` → **12 passed in
11:22, 0 mismatches** (the mismatch assertions live in the tests, so a green run
is the integrity verdict). Medians of 3 runs per phase:

| Phase | t_sync | files/s | MiB/s |
| --- | --- | --- | --- |
| initial burst (autotune concurrency) | 9.34 s | 214.1 | 14.5 |
| mutation wave | 3.09 s | 647.2 | 48.8 |
| sweep `--max-concurrent-transfers=1` | 26.64 s | 75.1 | 4.9 |
| sweep `=4` | 11.70 s | 171.0 | 11.6 |
| sweep `=8` | 9.49 s | 210.7 | 14.0 |

Two readings matter:

- **The concurrency sweep scales monotonically** (75 → 171 → 211 files/s), i.e.
  reconcile transfers *do* benefit from concurrency. This is the counter-evidence
  to the LAN inbound-apply convoy: the negative scaling recorded earlier is a
  property of that path, not of `MAX_CONCURRENT_TRANSFERS` in general.
- **`t_verify` (~32 s) dominates the harness wall clock**, because the
  REST-primary verdict re-downloads and re-hashes every file. The suite duration
  is therefore not a sync metric; only `t_sync` is.

Cross-mode reading (indicative, **not** an apples-to-apples comparison: the LAN
run covers 3× the files and drives one WSS event plus one HTTP download per
file, while server mode reconciles a listing with parallel uploads): server mode
converges 2000 files in 9.3 s (214 files/s), the p2p burst 6000 files at
120.7 files/s / t_sync 49.7 s (see the full-scale burst validation below). The
LAN path is markedly slower per file despite both peers being local, which is
where the remaining LAN investigation should start.

#### Reconcile cycle cost: first vs quiescent

Measured with a real p2p master (separate process) and `sync(p2p_mode=True)`
called three times on a 2000-file / 70 KiB tree:

| Cycle | t_sync | transfers |
| --- | --- | --- |
| 1st (populate) | 9.40 s | 2000 |
| 2nd (quiescent) | 0.43 s | 0 |
| 3rd (quiescent) | 0.23 s | 0 |

A no-op cycle is therefore **~0.25 s per 2000 files** (≈0.7 s at 6000 files):
the per-`RESYNC` (300 s) steady-state cost of the safety net is negligible, so
the mechanism earns its keep and its real risk is *silence*, not overhead (see
the watchdog entry below). The residual cost is the remote listing fetch plus a
`stat` sweep of the local tree; the SHA-256 cache makes it metadata-only, which
is why it does not scale with bytes.

### Stress-found correctness defects (fixed)

- *The periodic safety net could be disabled in silence.* `periodic_resync`
  skips a cycle when one is already running, and the skip was **DEBUG-only**.
  A cycle that never returns (a hung mount, a deadlock on `path_lock` /
  `sync_state._lock`) never executes its `finally`, so the coordination flag
  stays set and every later resync is skipped **forever** — with no signal that
  the only recovery mechanism for events missed by the watcher (`IN_Q_OVERFLOW`
  is not surfaced by watchdog, per `utils/resources.py`) had stopped working. The skip now
  escalates to **WARNING** once the running cycle outlives the RESYNC interval
  (a cycle older than its own period means the safety net has already missed a
  scheduled recovery), and the age of the active cycle is part of the message.
  New accessors `sync_cycle_age()` / `seconds_since_last_cycle()` expose the
  bookkeeping for logs and diagnosis.
  Regressions: `tests/test_sync_local.py::TestSyncCycleFlag::test_resync_warns_when_cycle_outlives_the_interval`,
  `...::test_resync_skip_stays_debug_while_the_cycle_is_recent`,
  `...::test_cycle_bookkeeping_tracks_a_real_cycle` (pins that `sync()` really
  rebinds the module state — without the `global` declaration Python binds
  locals and the watchdog would always read "idle"), `...::test_cycle_age_is_none_when_idle`.
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
  `tests/test_p2p_websocket_slave.py::TestSlaveFileEventHandler::test_stale_delete_dropped_when_path_recreated_inside_window`.

### Measured remaining limit (next target, not fixed)

The full-scale p2p burst drains at **~90 files/s** and no single hot function
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

- The negative scaling above is **specific to the LAN inbound-apply path** (the
  slave's event-driven downloads), not a general property of the engine. The
  server-mode concurrency sweep measures the **opposite** for reconcile
  transfers (client → server uploads), where more concurrency helps clearly:
  `--max-concurrent-transfers` 1 → 75 files/s, 4 → 171, 8 → 211 (medians of 3
  runs of a 2000-file burst, all 0 mismatches). Do not generalise the LAN
  finding to `MAX_CONCURRENT_TRANSFERS` as a whole; the two paths scale
  differently and only the inbound-apply one showed a convoy.
- Throughput must come from **less per-file Python work** (fewer statements,
  fewer locks touched, fewer encode/decode round trips per file), not from more
  threads on the same host — and the LAN apply path needs a profile of both
  peers during one real run before any knob is changed.

The gap between the replica (269 files/s) and the field (~90 files/s) is *not*
attributable to any component in isolation: in the field the master runs its
own watcher over the same 6000 files, serves the downloads and drives the WSS
sends while the slave applies them, and the two processes share the host's
cores. *(Update: that both-peer profile in one real run has since been done —
see "p2p burst: what the peer-offload block costs per file" below. With the
per-file CPU fixes applied the field runs ~113 files/s, and the largest
remaining field-vs-replica term is the peer-offload/inventory lane,
~4.3 ms/file, measured as a block.)*

### p2p burst: the CPU is real, and it is per-file Python

A later round measured the field path directly instead of the pool knob:

- **Thread CPU during the burst** (sampled from `/proc/<pid>/task/*/stat`): the
  slave burns **1.15-1.16 cores** and the master ~0.9, with the CPU spread over
  the 7 `asyncio_*` threads that `asyncio.to_thread` creates for the inbound
  fan-out — *not* the dedicated `hsync-transfer_*` pool used by the reconcile.
  Two executors serve the same work. Earlier "both peers idle" readings were
  mis-aligned samples taken outside the burst window; the burst is CPU-bound.
- **Per-file cost**: `usage` accounting gives **14.2-15.3 ms CPU/file** on the
  event path (81 files/s) versus ~9.4 ms on the reconcile path (145 files/s,
  observed concurrency peak 5 / time-weighted mean 2.9).
- **No waste**: exactly 2500 `[DOWNLOAD]` lines for 2500 unique paths, zero
  retries, zero errors, `mismatches=0`. The cost is genuine work.
- **Not the inventory DB**: `record_base + flush` measures 0.05 ms/file
  (0.02 ms batched) on a real store — the cProfile attribution that looked like
  40% was lock-wait accounting plus cProfile's per-call inflation on many small
  SQLite calls. Trust the `ru_utime/ru_stime` numbers, not the profiler shares.
- **Not the vclock/merge path**: adding the `version` object the master really
  advertises to the replica events *lowered* per-file cost (3.4 ms), so the
  merge path is not what makes the field expensive.
- **Not compression/logging/state**: at 200 KiB those add ~2.6 ms/file.
- **The replica remains cheaper per file than the field** (4.3 vs ~11 ms
  post-fix), and the field-vs-replica delta is now largely accounted for by
  the peer-offload/inventory lane (~4.3 ms/file as a block, see the
  peer-offload section below); what remains unattributed is the *internal*
  split inside that lane, which needs finer instrumentation.

### Read chunk size (transfer loop)

`adaptive_chunk_size` picked 8 KiB for every file under 1 MB, so a 200 KiB
transfer paid 26 Python-level iterations (frame read, throttle wrapper,
`hash.update`, write). A/B on the HTTPS apply path, identical configuration and
incompressible payloads, 3 runs per arm:

| small tier | ms CPU/file (200 KiB) | files/s |
| --- | --- | --- |
| 8 KiB | 5.5 | 232-269 |
| 256 KiB | 4.3 | 300-348 |

Above ~1 MiB the chunk size measured **neutral** (8 MiB files: 13.9 vs 14.9-18.0
ms/file), so the tiers were raised for the sub-1MB class and only enough above
it to keep `CHUNK_SIZE_LARGE * MAX_CONCURRENT_TRANSFERS` buffering small.

**Honest caveat**: the end-to-end LAN probe did **not** resolve the saving
(75.8 vs 81.3 files/s, i.e. within noise) because that path spends its CPU
budget on the field overhead above (now largely traced to the peer-offload
lane, see the peer-offload section below). The per-file reduction is real and
reproducible in isolation; the end-to-end throughput gain stays unproven and
should be re-tested after the lane is optimised.

**Measurement error worth remembering**: the first chunk A/B used a highly
compressible fixture (a repeated 18-byte pattern), so zstd shrank the HTTP body
to a few KB and the "transfer" degenerated into a buffer split — that arm
measured urllib3's splitting, not the network path, and overstated the gain
(6.1 → 2.8 ms/file, +43%). Re-measured with `os.urandom` payloads the effect is
smaller but real. Always make large-file/transfer fixtures incompressible.

### p2p burst: what the peer-offload block costs per file

A/B on the same probe (2500 files / 200 dirs), 3 runs per arm, alternating:

| arm | files/s (median [min..max]) | slave CPU/file (median [min..max]) |
| --- | --- | --- |
| peer offload on | 88.6 [82.3..101.8] | 12.95 ms [11.14..14.06] |
| `--no-peer-download` | 130.4 [114.2..159.4] | 8.62 ms [6.92..10.05] |

+47 % throughput and −33 % CPU/file with the block off; both arms saturate
~1.15 cores, so the burst is CPU-bound either way.

**Caveat that must not be lost:** `--no-peer-download` is **not one switch**.
`_start_slave_seeder` (`p2p_handler.py:68`) skips *both* the slave's own seeder
`P2PServer` *and* its `P2PInventoryPublisher` when it is off, and the resolver's
`enabled` turns False. The A/B therefore measures the whole peer-offload block
(source lookup + inventory publication + seeder + capability advertisement), and
the 4.3 ms/file must **not** be attributed to any single mechanism inside it.

Within the block, the per-file source lookup was measured against the production
HTTPS server: `http_post` + `_read_bounded_response` on a 200 costs ~1.0-1.1 ms
(POST alone 1.29 ms; reading a real 200 listing 1.04 ms). `candidates_for(refresh=True)`
issues **one POST per file** because it calls `resolve([file])`
(`p2p_sources.py:740`), while `resolve` is explicitly designed to batch up to
`P2P_SOURCES_MAX_FILES` identities — the realtime path defeats that batching by
feeding it one file at a time, and the realtime path always refreshes
(`refresh_peer_sources=source_resolver is not None` in `sync.update_file`).

Batching those lookups is the obvious next step — **and it was implemented,
measured, and reverted: it does not pay at the default concurrency.** A
micro-batching layer was built (the first caller collects for
`P2P_SOURCE_LOOKUP_WINDOW_S` = 3 ms so the transfer pool's concurrent workers
share one wantlist POST; the others read the resulting TTL candidate cache, so
the per-file outcome is unchanged) and A/B'd 3 runs per arm on the same probe:

| arm | files/s (median [min..max]) | slave CPU/file (median [min..max]) |
| --- | --- | --- |
| per-file lookup (reverted to this) | 88.6 [82.3..101.8] | 12.95 ms [11.14..14.06] |
| micro-batched lookup | 90.7 [83.9..102.1] | 12.13 ms [10.58..13.14] |

The ranges overlap and the medians differ by ~6 %, which the arithmetic
explains: the POST is ~1 ms of ~13 ms per file, and at the default 5-way
concurrency a 3 ms collection window rarely gathers more than ~1.5 callers while
its own download waits for the leader. A wider window would collect more but
stall the leader longer (a 20 ms window would gather nearly all five and lose
more than it saves). It was reverted rather than kept as unmeasured complexity
on the LAN transfer hot path.

**Conclusion for the next attempt:** the remaining ~3-4 ms/file of the
peer-offload block is **not** the source lookup. The deep-profile A/B
(`/tmp/deepprofAB_base` vs `deepprofAB_nopeer`, 2026-09-21) narrows it to the
slave-side inventory/event chain:

- `on_created` (watchdog handler, `p2p_websocket.py`) is called **the same
  number of times in both arms** (~82 per sampled window) but accumulates
  **~20× more time with the block on** (0.342 s vs 0.018 s cum): with the
  inventory lane live the handler feeds the `P2PInventoryStore`/publisher per
  event; with `--no-peer-download` the observer is a NOOP.
- `_publish_deltas`/`_send_with_ack` ran only **4 times per window** — the
  publisher is batched, so the per-frame WSS+ACK cost is amortised, not the
  dominant term the earlier hypothesis assumed.
- Source-resolution POSTs: **2 `resolve` calls per window** (~0.25 s cum, mostly
  GIL-wait).

Absolute per-function numbers are GIL-inflated (blocked threads show cum on
whatever was on the stack), so the block-level delta — **~4.3 ms/file, −33 %
CPU/file with the lane off** — stays the reliable figure; the split above is
directional. The engineering takeaway: the cost scales with *per-event work in
the inventory observer chain*, not with per-frame or per-lookup network calls.

**Per-event observer cost ruled out (2026-09-21).** The publisher's per-event
`upsert` is not the block: `P2PInventoryPublisher._normalize_entry` (path
normalisation + one `lstat`) measures **~9 µs** and
`utils.normalize_bucket_relpath` ~3 µs, i.e. ~55 ms for 6000 files — three
orders of magnitude below the ~4.3 ms/file (~10.8 s) block delta. A coalesced
wakeup (`_notify` once per burst instead of once per event) and a single-`lstat`
`_normalize_entry` were implemented and A/B'd 3 runs per arm at 2500 files:
medians 179.2 → 187.2 files/s (+4.5 %, inside the ±15 % host spread), so both
were **reverted** rather than kept as unmeasured complexity. The block is
therefore not removable per-event computation: it is the resolver's per-file
lookup POST plus the extra lane threads (seeder HTTPS server, publisher task)
contending for the GIL with the apply workers. The only per-file reduction
identified (source-lookup batching) was already measured and reverted; the
remaining lever is `--no-peer-download` for peers that do not need to serve
others, and any further gain needs a lane-level redesign (separate process or
burst-time resolver deferral) with its own freshness/robustness analysis.

**Intervention options, judged by sync reliability (2026-09-21).** Three ways to
recover the block were evaluated; the default stays unchanged:
- *Defer the resolver during a burst*: data-correct (the master is always a
  valid source, so a skipped lookup loses offload, never a transfer), but it
  shifts the burst's download load onto the master exactly when peers could
  have shared it — a fleet-robustness regression, not a win.
- *Move the lane to a separate process*: the only option that removes the GIL
  contention without changing sync semantics, but it adds a process boundary
  (lifecycle, crash, reconnect) and needs an ADR plus review.
- *Keep the default and expose the lever*: `--no-peer-download` is already the
  supported way to trade offload for burst speed; it stays the recommendation
  for single-peer or performance-first deployments.
Decision: **no default change now** (correctness and fleet robustness over pure
throughput); the separate-process lane is the tracked design task.

### p2p burst: per-file CPU attribution, and the two wastes it removed

The single-component benches could not explain the burst, so the **real peer** was
instrumented. `/proc/<pid>/task/*/stat` sampled over the burst window, per thread
name, is the attribution that finally holds (2500 files / 200 dirs):

| thread | CPU share | ms/file |
| --- | --- | --- |
| `asyncio_0..7` (the 8 `to_thread` apply workers) | 72 % | 10.6 |
| `python` (main thread / asyncio loop) | 10.7 % | 1.58 |
| `Thread-9` (inotify reader) | 7.9 % | 1.17 |
| `Thread-7` (watchdog emitter) | 4.2 % | 0.62 |
| `Thread-8`, `loguru-writer-1` | ~2.4 % / 0 % | 0.36 / 0 |

Total **~12-15 ms CPU/file with 83 % user / 17 % syscall**. The whole Python side
serialises onto ~1 core (the GIL), so throughput ≈ (1 core) / (CPU per file):
that is the ~90 files/s ceiling, and it is why *more threads make it worse*.

A deterministic `cProfile` inside the workers (one worker, so no cross-thread
interference) named the composition, per transferred file:

| item | per file | note |
| --- | --- | --- |
| `posix.lstat` / `posix.stat` | 54 / 23 calls | path confinement and the stat identity checks |
| `validate_remote_relpath` (+ `is_reserved_name`, `has_problematic_trailing`) | 18.8 (+60) | our own lexical validation |
| `os.path.realpath` (via `Path.resolve`) | 10.2 | **0.11 ms** of CPU — see below |
| `loguru._handler.emit` per accepted record | 1 | 82 µs each |
| stdlib `LogRecord.__init__` + `findCaller` | 14-26 | records that were dropped |
| `requests` env-proxy resolution | 5 | `getproxies_environment` + `proxy_bypass_environment`, 167 µs each |
| `_ssl._SSLSocket.read` | — | 5 % self time, not removable |

Two of those were **pure waste** and are fixed:

- **The pinned LAN session resolved the environment proxies per request.** The
  fingerprint is the LAN trust anchor, so an environment proxy or CA bundle is
  both pointless and harmful (it would route the pinned connection through a
  third party); `get_http_session` now sets `trust_env = False` for
  fingerprint-pinned sessions only. Measured **0.6-0.85 ms CPU/file** (5 calls
  per file, 3.8 % of the apply-path profile). Regression:
  `tests/test_utils.py::TestGetHttpSession::test_fingerprint_session_ignores_environment_proxies`
  (and `..._plain_session_still_consults_environment_proxies` for remote mode).
- **Every library DEBUG line built a `LogRecord` to be dropped.** `setup_logging`
  used loguru's `level=0` recipe, and a NOTSET root answers `True` to
  `isEnabledFor(DEBUG)`, so `urllib3`'s per-connection/per-request DEBUG messages
  paid record construction plus a stack walk in `findCaller`, only for the INFO
  sink to discard them: **14-26 records/file** (two independent windows), 6.5 µs
  each (**0.1-0.17 ms/file**) against 0.08 µs for the level check that now avoids
  them. Output is unchanged (they were dropped before and after); `--debug` still
  enables them. Regression:
  `tests/test_logger.py::TestSetupLogging::test_stdlib_root_level_follows_the_resolved_level`.

End-to-end on the burst probe, 3 runs per arm (median [min..max]):

| arm | files/s | slave CPU/file |
| --- | --- | --- |
| before (documented 3-run A/B) | 88.6 [82.3..101.8] | 12.95 ms [11.14..14.06] |
| after | 113.1 [101.3..149.5] | 10.92 ms [8.04..12.01] |

Every post-fix run stayed ≥ 100 files/s and ≤ 12.0 ms/file, which no pre-fix run
did; the isolated cost of what was removed is ~1.0 ms/file (~8 %), so part of the
move is this host's ±15 % run-to-run spread. Treat the direction as confirmed and
the magnitude as bounded by the isolated figures.

At the documented full scale (6000 files / 400 dirs, `HSYNC_BB_STRESS=1`) the
initial burst goes from the recorded **74.3 files/s / t_sync 80.7 s** to
**120.7 files/s [119.6..151.0] / t_sync 49.7 s [39.7..50.1]**, with the suite
green (3 iterations, 0 mismatches). Cross-run numbers at that scale carry a
host-state caveat: `t_gen` and `t_verify` — pure harness I/O, untouched by these
changes — moved 2-3× between the two runs, so the interleaved probe A/B above
remains the controlled comparison and this one corroborates its direction.

Two conclusions that must not be lost:

- **`os.path.realpath` is not the bottleneck**, although it is the hottest *leaf*
  in a statistical sample (44 % of the workers' samples). Its measured CPU is
  0.11 ms/file (10.2 calls × 11.1 µs). The samples are inflated by GIL
  contention: a thread waiting for the interpreter is observed inside whatever
  Python function it was descheduled in.
- **`resolve_within` is called 5.1 times per file** (`path_validation.py:581`
  re-resolves the constant `base_dir`, `:583` the target: exactly 2 realpath calls
  each, 25 456 for 2500 files, 100 % attributed). Caching the base was measured at
  ~0.08 ms/file and **not** taken: a 0.6 % saving does not justify a
  process-lifetime cache on a security path.

Measurement pitfalls found in this round (each produced a wrong number first):

1. A hand-rolled `http.server` stub reported **43 ms** to read a tiny 200 body.
   The same helper against the production `P2PServer`: **1.04 ms**. The stub's
   unbuffered HTTP/1.1 writes (Nagle / delayed-ACK) were the cost, not the code.
   Never trust a scratch stub for latency.
2. The same stub silently short-circuited (**0 POSTs received**) because the
   test CID `"benchcid"` fails `build_p2p_sources_request` validation; the
   lookup then looked free (0.019 ms). Assert the *observable effect* (request
   count), not just the elapsed time.
3. Sampling `/proc/<pid>/task/*/stat` outside the burst window showed both peers
   idle; aligned sampling showed 1.15 cores. Always confirm the sampling window
   covers the burst.
4. A compressible fixture hid the transfer cost behind zstd (see the chunk
   section above).
5. **A statistical stack sampler misattributes CPU under the GIL.** It reported
   `posixpath.realpath` as 44 % of the apply workers' samples; the same function
   measured in isolation is 11.1 µs, i.e. 0.11 ms/file (0.8 %). A thread waiting
   for the interpreter is sampled *inside* whatever Python function it was
   descheduled in. Use the sampler to find *candidates*, then confirm with a call
   count (`os.path.realpath_calls=25456` for 2500 files) or a per-thread
   `/proc` CPU split — never as the verdict.
6. **`cProfile` per thread on eight workers raises inside the code under test.**
   Each worker enabling its own `Profile` made the apply path fail with
   `ValueError: Another profiling tool is already active` (363 of 400 profiled
   calls). Those failures then dominated the profile through loguru's traceback
   formatting (`_better_exceptions`: source tokenization plus `inspect` of frame
   locals, ~1.8 ms per exception, 46 % of the profile) and looked exactly like a
   product defect. Profile with concurrency 1, and check the run logged **zero**
   exceptions before believing a profile.
7. **A short "the file count stopped moving" wait is not convergence.** Under
   instrumentation the burst stalls long enough for a stability heuristic to fire
   mid-transfer; the teardown then aborts every in-flight download and produces
   hundreds of error records that have nothing to do with steady state. Wait for
   the expected file count.
8. **A grep-shaped line count hides the level you did not ask for.** Counting log
   lines with `grep -oE 'INFO: |WARNING: '` while comparing against `wc -l` made
   a 381-line `ERROR:` storm look like a clean run. Count each level explicitly,
   or reconcile the total with the per-level sum.

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
