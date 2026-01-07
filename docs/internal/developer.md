# HSYNC Client — Developer Guide

This page covers internal implementation details useful for contributors and
developers working on the HSYNC Client codebase.

> For the high-level system design, see [Architecture](architecture.md).
> For the sync protocol, see [Sync Flow](../sync-flow.md).

## Table of Contents

- [Module Structure](#module-structure)
- [Performance Optimizations](#performance-optimizations)
- [Error Handling & Retries](#error-handling--retries)
- [Compression Internals](#compression-internals)
- [WebSocket Reconnection](#websocket-reconnection)
- [Stdin Command Protocol](#stdin-command-protocol)
- [LAN Peer Pairing Flow](#lan-peer-pairing-flow)

---

## Module Structure

```mermaid
graph LR
    subgraph "hsync_client"
        main[main.py]
        cli[cli.py]
        config[config.py]
        constants[constants.py]
        sync[sync.py]
        transfer[transfer.py]
        websocket[websocket_handler.py]
        logger[logger.py]
        event_queue[event_queue.py]
        compression[compression.py]
        stdin[stdin_commands.py]

        subgraph "LAN/"
            lan[lan.py]
            lan_server[lan_server.py]
            multipart[multipart.py]
            lan_websocket[lan_websocket.py]
            lan_handler[lan_handler.py]
        end

        subgraph "models (from hsync-common)"
            file[file.py]
            dir[dir.py]
            event[event.py]
            symlink[symlink.py]
            sync_action[sync_action.py]
        end
    end

    main --> cli
    main --> config
    main --> sync
    main --> websocket
    sync --> transfer
    sync --> constants
    websocket --> transfer
    cli --> logger
    main --> lan
    lan --> lan_server
    lan --> lan_websocket
    lan_websocket --> lan_handler
    transfer --> compression
```

## Performance Optimizations

### SHA-256 Cache with mtime Validation

To avoid redundant SHA-256 computations during a single sync operation, the client maintains an in-memory cache that maps file paths to `(sha256sum, mtime)` tuples. On each `File.get()` call, the cached SHA-256 is returned only if the file's modification time hasn't changed — otherwise the entry is automatically invalidated and recomputed.

- Cache is cleared at the start of each sync cycle (`sync()`, `_run_preview()`)
- Individual entries are invalidated after downloads/uploads and before watchdog `File.get()` calls
- mtime validation provides automatic staleness detection even if manual invalidation is missed

### Adaptive Chunk Size for SHA-256 Computation

SHA-256 hashing uses different I/O chunk sizes based on file size for optimal throughput:

| File Size | Chunk Size |
|-----------|------------|
| < 1 MB | 8 KB |
| < 100 MB | 64 KB |
| ≥ 100 MB | 256 KB |

### Configurable Parallelization for File Scanning

Local file scanning uses `ThreadPoolExecutor` with an adaptive worker count:

| File Count | Workers |
|------------|---------|
| < 10 | min(2, files) |
| < 100 | min(4, files, CPU cores) |
| ≥ 100 | min(8, files, CPU cores) |

Override with `HSYNC_MAX_FILE_WORKERS` environment variable.

### Bandwidth Limiting

Upload and download bandwidth can be limited via `--max-bandwidth-upload` and `--max-bandwidth-download` CLI options. The implementation uses a **token bucket** algorithm for smooth rate control:

- **Uploads**: `_ThrottledFileReader` wraps file handles and injects delays after each `read()` call
- **Downloads**: Throttle delay is applied after each downloaded chunk in the streaming loop
- Throttle state is reset between sync sessions via `reset_bandwidth_throttle()`

For usage details, see [Configuration > Bandwidth Limiting](../configuration.md#bandwidth-limiting).

## Error Handling & Retries

Errors are classified by type and handled independently so a single failing file
never aborts the whole sync:

```mermaid
flowchart TD
    E[① Error Occurs] -->|"②"| T{Error Type}

    T -->|"③a Network"| N[Retry with backoff]
    T -->|"③b Auth"| A[Check API key]
    T -->|"③c Not Found"| NF[Skip file]
    T -->|"③d Permission"| P[Log warning]

    N -->|"④"| R{Retry count}
    R -->|"⑤a < 3"| W[Wait and retry]
    R -->|"⑤b >= 3"| F[Mark as failed]

    W -->|"⑥ Retry"| E
```

- **Network errors** are retried with backoff up to a bounded number of attempts,
  then the item is marked failed and the sync continues with the next file.
- **Authentication errors** abort early with a clear message (see
  [Configuration > API Key & Encryption Policy](../configuration.md#api-key--encryption-policy)).
- **Not-found** items are skipped; **permission** issues are logged as warnings.

## Compression Internals

HSYNC uses **Zstandard (zstd)** for transfer compression. Compression is enabled
by default and can be disabled with `--no-compression` or
`HSYNC_COMPRESSION_ENABLED=false`.

### Thresholds

Different thresholds balance CPU cost vs. bandwidth savings for each direction:

| Context | Threshold | Rationale |
|---------|-----------|-----------|
| **Download** (Server→Client) | 10 KB | Server CPU is acceptable, clear benefit above this size |
| **Upload** (Client→Server) | 32 KB | Client CPU is more precious, typical production threshold |
| **LAN mode** | 100 KB | Local network is fast (1Gbps+), only compress large files |

### Skipped Files

Compression is automatically skipped for:
- Files below the threshold (depends on context)
- Pre-compressed formats: `.jpg`, `.png`, `.mp4`, `.zip`, `.gz`, `.pdf`, `.docx`, etc.

### Protocol

- Server informs client about compression support during WebSocket handshake
- In LAN mode, peers advertise compression capability via mDNS

For usage and configuration, see [Configuration > Compression](../configuration.md#compression).

## WebSocket Reconnection

When the WebSocket connection drops in watch mode, `remote_connect` loops
forever: sleep a **fixed** `RECONNECT_DELAY`, refresh autotune RTT, then
run a full `sync()` and re-enter `remote_events`. There is no attempt
counter, no exponential backoff, and no fatal exit after N failures.

```
Connected ──(disconnect)──► sleep RECONNECT_DELAY
                         ──► full sync() ──► Connected
```

`RECONNECT_DELAY` defaults to 5 seconds (range 1–60). Autotune may change
the value from measured RTT; each wait still uses that single interval.
Exponential backoff belongs to per-file transfer retries (`retry.py`) and
the outbound `EventQueue`, not this loop.

### Reconnect vs. periodic resync

Every reconnect already runs a full `sync()` before listening again.
`HSYNC_RESYNC` (default 300 seconds) is a separate safety net that
re-reconciles while the connection stays up, in case events were missed.

## Stdin Command Protocol

In watch mode, the client accepts runtime commands via stdin. Each command is a
single JSON line:

```json
{"command": "SET_SYNC_DIRECTION", "direction": "upload-only"}
```

### Supported commands

| Command | Payload | Effect |
|---------|---------|--------|
| `SET_SYNC_DIRECTION` | `{"direction": "bidirectional"}` / `{"direction": "upload-only"}` / `{"direction": "download-only"}` | Change sync direction at runtime |
| `SYNC` | (none) | Trigger an immediate full resync |
| `PREVIEW` | (none) | Run a dry-run preview of pending sync actions |
| `SET_BANDWIDTH` | `{"upload": 1048576}` / `{"download": 500000}` | Update upload/download bandwidth limits at runtime |

Commands are processed asynchronously by `stdin_commands.py`. The direction
change is applied inline without restarting the watchdog observer — events
already in the queue are filtered by the new direction immediately.

> The desktop client (`hsync-desktop`) uses this protocol via the subprocess
> stdin pipe to implement GUI controls like the sync direction selector.

## LAN Peer Pairing Flow

When two LAN peers discover each other for the first time, they establish trust
through a **Trust On First Use (TOFU)** model:

```mermaid
sequenceDiagram
    participant A as Peer A (slave)
    participant M as Peer M (master)

    A->>M: ① HTTPS GET /health
    M-->>A: ② 200 OK + TLS certificate

    Note over A: ③ Record fingerprint of M's cert
    Note over A:    in HSYNC_LAN_PIN_FILE (default ~/.local/state/hsync/lan_known_peers.json)

    A->>M: ④ Subsequent connections: verify fingerprint
    alt Fingerprint matches
        M-->>A: ⑤ Trusted, proceed
    else Fingerprint changed
        M-->>A: ⑥ Connection refused (MitM warning)
    end
```

### Fingerprint pinning

The certificate's SHA-256 fingerprint is stored in a local JSON file
(`HSYNC_LAN_PIN_FILE`, default `~/.local/state/hsync/lan_known_peers.json`).
On every subsequent connection, the presented certificate's fingerprint is
checked against the pinned value. If it doesn't match, the connection is
refused — this prevents man-in-the-middle attacks on the LAN.

### Strict pairing (`--require-pairing`)

When strict pairing is enabled, a first-seen master certificate is **not**
automatically trusted. Instead, the fingerprint must be confirmed out-of-band
(e.g., compared manually or via a QR code). Until confirmed, all connections to
that peer are refused. This is the most secure mode and is recommended when
multiple untrusted devices share the same network.

### see also

- [Conflict Resolution](../conflict-resolution.md) — three-way merge algorithm and conflict handling.
- [Architecture](architecture.md) — high-level system overview and data flows.
- [Sync Flow](../sync-flow.md) — client-side sync flow.
- [LAN Internals](lan-internals.md) — deep dive into the LAN protocol.
- [Contributing](../../CONTRIBUTING.md) — development setup and guidelines.
- [HSYNC Common docs](https://github.com/albertobasso/hsync-common/blob/main/docs/README.md) — shared models and protocol.
