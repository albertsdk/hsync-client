# HSYNC Client — Testing Guide

This guide covers the test suite for the HSYNC Client: structure, how to run
tests, how to write new tests, and cross-project testing with `hsync-common`.

> For development setup, see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Table of Contents

- [Running tests](#running-tests)
- [Test structure](#test-structure)
- [Fixtures](#fixtures)
- [Writing new tests](#writing-new-tests)
- [Mocking the server](#mocking-the-server)
- [LAN mode testing](#lan-mode-testing)
- [Cross-project testing](#cross-project-testing)

---

## Running tests

```bash
# Install dev dependencies
poetry install --with dev

# Run the full suite
poetry run pytest

# Run a specific file
poetry run pytest tests/test_sync.py

# Run with verbose output
poetry run pytest -v

# Run with coverage
poetry run pytest --cov=hsync_client --cov-report=term-missing

# Run only tests matching a pattern
poetry run pytest -k "conflict"

# Stop on first failure
poetry run pytest -x
```

## Test structure

```
tests/
├── __init__.py
├── conftest.py               # Shared fixtures and test configuration
├── test_sync.py              # Sync engine: file comparison, action generation
├── test_transfer.py          # Upload/download operations
├── test_websocket.py         # WebSocket handler and event processing
├── test_lan.py               # LAN mode: discovery, master election, pairing
├── test_config.py            # Configuration loading and CLI arg parsing
├── test_compression.py       # Zstandard compression/decompression
├── test_ignore.py            # .hsyncignore pattern matching
├── test_cli.py               # CLI argument parsing
├── test_main_logic.py        # Core sync orchestration
├── test_event_queue.py       # Event queuing and retry logic
└── test_conflict.py          # Three-way merge and conflict resolution
```

## Fixtures

`conftest.py` provides shared fixtures:

| Fixture | Purpose |
|---------|---------|
| `tmp_path` (pytest built-in) | Temporary directory for isolated test data |
| `sample_files` | Pre-built file tree with known content and hashes |
| `mock_server` | Mock HTTP server (responses library) for API tests |
| `mock_websocket` | Mock WebSocket connection for event tests |
| `test_config` | Minimal configuration for test isolation |

### Using fixtures

```python
def test_file_comparison(tmp_path, sample_files):
    """Compare local and remote file lists."""
    local = scan_directory(tmp_path)
    remote = create_remote_list(sample_files)
    actions = get_sync_actions(local, remote)
    assert len(actions) == expected_count
```

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
        "https://server:8765/api/v1/buckets/test/files",
        json={"files": [{"name": "hello.txt", "sha256sum": "abc123", "size": 13}]},
        status=200,
    )
    files = fetch_remote_files("https://server:8765", "test")
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
        {"cid": "peer1", "sync_direction": "bidirectional"},
        {"cid": "peer2", "sync_direction": "upload-only"},
    ]
    master = elect_master(peers)
    assert master["cid"] == "peer1"  # bidirectional preferred
```

## Cross-project testing

The client depends on `hsync-common` models. When changing models in
`hsync-common`, always run both test suites:

```bash
# From hsync-common
cd ../hsync-common && poetry run pytest

# From hsync-client
cd ../hsync-client && poetry run pytest
```

Model changes that break the client are caught by sync engine tests and
compression tests.

## See also

- [CONTRIBUTING.md](../CONTRIBUTING.md) — development setup and workflow.
- [Developer Guide](developer.md) — internal implementation details.
- [Sync Flow](sync-flow.md) — client-side sync flow.
