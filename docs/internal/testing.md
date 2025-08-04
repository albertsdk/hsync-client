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

## Test structure

```
tests/
├── __init__.py
├── conftest.py                         # Shared fixtures and test configuration
│                                          (autouse fixtures, Black‑Box harness,
│                                           live_server, ClientGlobalsGuard, …)
├── test_bb_remote_sync.py              # Black‑Box: client‑server sync
├── test_bb_continuous_sync.py          # Black‑Box: continuous (watch) sync
├── test_bb_safety_guards.py            # Black‑Box: mass‑delete guard, disk‑space
├── test_bb_cli_surface.py              # Black‑Box: CLI help / error messages
├── test_bb_lan_mode.py                 # Black‑Box: LAN P2P sync (opt‑in)
├── test_sync_local.py                  # Local file comparison and action generation
├── test_sync_remote.py                 # Remote file comparison
├── test_sync_update.py                 # File update detection
├── test_sync_workers.py                # Parallel transfer workers
├── test_sync_permanent_failures.py     # Permanent-failure skip-set
├── test_transfer_download.py           # Download operations
├── test_transfer_upload.py             # Upload operations
├── test_transfer_status.py             # Transfer status tracking
├── test_websocket_events.py            # WebSocket event processing
├── test_events.py                      # Event models and processing
├── test_event_queue.py                 # Event queuing and retry logic
├── test_event_dispatch_delete.py       # Inbound delete event dispatch
├── test_event_dispatch_rename.py       # Inbound rename event dispatch
├── test_event_dispatch_symlink.py      # Inbound symlink event dispatch
├── test_lan_peer.py                    # LAN peer model and logic
├── test_lan_peer_manager.py            # LAN peer manager
├── test_lan_handler_advanced.py        # Advanced LAN handler scenarios
├── test_lan_sync.py                    # LAN sync operations
├── test_lan_server_endpoints.py        # LAN server endpoints
├── test_lan_server_https.py            # LAN server HTTPS setup
├── test_lan_server_security.py         # LAN server security
├── test_lan_server_thread_cap.py       # LAN server thread cap
├── test_lan_utils.py                   # LAN utility functions
├── test_lan_websocket_handle.py        # LAN WebSocket handle
├── test_lan_websocket_handler.py       # LAN WebSocket handler
├── test_lan_websocket_server.py        # LAN WebSocket server
├── test_lan_websocket_slave.py         # LAN WebSocket slave client
├── test_config.py                      # Configuration loading and CLI arg parsing
├── test_compression.py                 # Zstandard compression/decompression
├── test_ignore.py                      # .hsyncignore pattern matching
├── test_cli.py                         # CLI argument parsing
├── test_validators.py                  # Config validators
├── test_exceptions.py                  # Exception hierarchy
├── test_main_logic_deletion_risk.py    # Core sync orchestration: deletion risk
├── test_main_logic_lan.py              # Core sync orchestration: LAN mode
├── test_main_logic_preview.py          # Core sync orchestration: preview mode
├── test_main_logic_sync.py             # Core sync orchestration: sync flow
├── test_event_dispatch_base.py         # Base event dispatch logic
├── test_file_event_handler.py          # File event handler
├── test_logger.py                      # Logging configuration
├── test_edge_cases.py                  # Edge cases and corner scenarios
├── test_stdin_commands.py              # Stdin protocol commands
├── test_coverage_gaps.py               # Coverage gap fillers
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
├── test_delta_transfer.py              # Delta transfer (rsync-style)
└── test_conflict.py                    # Three-way merge and conflict resolution
```

## Fixtures

`conftest.py` provides shared fixtures in three layers:

### Global autouse fixtures

| Fixture | Scope | Purpose |
|---------|-------|---------|
| `reset_sync_state_stores` | function | Clears the in‑memory `sync_state._stores` before each test so a previous test's `LOCAL_BUCKET` does not leak into the current one. |
| `deterministic_cli_output` | function | Forces plain (uncoloured) CLI output so assertions on `result.output` are stable across CI and local runs. |

### Black‑Box harness fixtures (server‑mode)

Used by `test_bb_remote_sync.py`, `test_bb_continuous_sync.py`,
`test_bb_safety_guards.py` and `test_bb_cli_surface.py`.

| Fixture | Scope | Purpose |
|---------|-------|---------|
| `live_server` | session | Starts the **real `hsync-server`** in‑process via uvicorn on loopback HTTPS with a self‑signed certificate. Yields `(base_url, api_key)`. Disables rate‑limiting and docs. |
| `bb_bucket` | function | Creates a uniquely‑named bucket on the live server for one test. |
| `local_dir` | function | Creates a temp directory used as the client's local sync path. |
| `run_sync_once` | function | Returns a callable that runs a **one‑shot** client sync via `CliRunner` (Typer) with `--yes --insecure --min-free-disk-space 0`. Wraps the invocation in `_ClientGlobalsGuard`. |
| `run_cli` | function | Like `run_sync_once` but adds no safety flags of its own — used by tests that must control `--yes` / `--sync-direction` / `--min-free-disk-space` precisely. |
| `watch_sync` | function | Returns a callable that starts the client in **watch mode** as a subprocess. The caller receives the `Popen` handle and must terminate it. |
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
        local_hash="abc123",   # local changed
        remote_hash="def456",  # remote changed
        base_hash="xyz000",    # base is different from both
    )
    assert result == SyncDecision.conflict

def test_resolve_three_way_upload():
    # Only local changed
    result = resolve_three_way(
        local_hash="abc123",   # local changed
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

| File | What it covers |
|------|---------------|
| `test_bb_remote_sync.py` | Full sync cycle: upload, download, deletion, rename, directory sync, Unicode filenames, feature flags (compression, delta, autotune), dry‑run, ignore patterns. |
| `test_bb_continuous_sync.py` | Watch‑mode (`--watch`): file creation, modification, and deletion are propagated while the client runs continuously. |
| `test_bb_safety_guards.py` | Mass‑deletion guard, mass‑deletion percent guard, insufficient disk‑space abort, download‑only interactive prompts. |
| `test_bb_cli_surface.py` | CLI help output, error messages, and surface‑level argument validation. |

These tests use the `live_server` session fixture (real `hsync-server`
via uvicorn on loopback HTTPS). They require **no external services**
and run in every CI / local environment.

### LAN‑mode Black‑Box tests (`test_bb_lan_mode.py`)

Tests the LAN peer‑to‑peer sync use case: two real client instances that
discover each other via mDNS/zeroconf, elect a master, and sync over
HTTPS/WSS with TOFU certificate pinning.

**These tests are disabled by default** because they are inherently
environment‑sensitive (multicast on loopback, self‑signed certificate
generation, election timing, multi‑process coordination).

To opt in:
```bash
HSYNC_BB_LAN=1 .venv/bin/python -m pytest tests/test_bb_lan_mode.py
```

The LAN tests start two subprocesses with `--lan-mode` (and
`--force-master` / `--never-master`). They use the `_HSYNC_COMMON_DIR`
constant to inject the `hsync-common` source directory into
`PYTHONPATH` so the subprocess can import shared models.

**Known limitations**:
- The tests are flaky by design (multi‑process + mDNS + election
  handshake). A single timeout or port conflict can cause a cascade of
  failures.
- stdout is piped to `subprocess.DEVNULL` to avoid deadlock; stderr is
  polled for log markers (`MASTER`, `Pinned LAN master certificate`,
  etc.).

### Shared Black‑Box constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `BB_API_KEY` | `test-bb-api-key-0123456789abcdef` | API key used by the live server and the client. |
| `BB_CLIENT_CID` | `TestClientX1234567890` | Well‑formed 21‑character client ID for server uploads. |

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
