# Client Synchronization Flow

This document describes how the HSYNC client handles file synchronization from
its own perspective. The client is the **active agent**: it watches the local
filesystem, decides what to sync, and drives the transfer.

> For the server-side flow (file storage, event broadcast, tombstones) see the
> [server Sync Flow](https://github.com/albertobasso/hsync-server/blob/main/docs/sync-flow.md).

## Overview

```mermaid
graph TB
    subgraph "HSYNC Client"
        WDA[Watchdog]
        ENGINE[Sync Engine]
        QUEUE[Event Queue]
    end

    subgraph "Server / LAN Master"
        API[REST API]
        WS[WebSocket]
    end

    subgraph "Local Disk"
        FS[File System]
    end

    FS -->|"① File change"| WDA
    WDA -->|"② Event"| ENGINE
    ENGINE -->|"③ Upload/Download"| API
    WS -.->|"④ Remote event"| ENGINE
    ENGINE -->|"⑤ Write"| FS
```

## Initial synchronization

When the client starts, it performs a full reconciliation to ensure consistency
with the remote side (server or LAN master):

```mermaid
sequenceDiagram
    participant C as Client
    participant R as Remote (Server / Master)

    C->>R: ① GET /api/v1/buckets/{bucket}/files
    R-->>C: ② List of remote files

    C->>C: ③ Scan local directory
    C->>C: ④ Compare local vs remote

    loop ⑤ For each difference
        alt ⑤a Remote newer
            C->>R: ⑥ Download file
            R-->>C: ⑦ File content
            C->>C: ⑧ Write to local disk
        else ⑤b Local newer
            C->>R: ⑥ Upload file
            R-->>C: ⑦ Confirmation
        else ⑤c Only remote
            C->>R: ⑥ Download file
            R-->>C: ⑦ File content
        else ⑤d Only local
            C->>R: ⑥ Upload file
            R-->>C: ⑦ Confirmation
        end
    end

    C->>C: ⑧ Sync complete
```

## Real-time synchronization (watch mode)

In watch mode, the client maintains a WebSocket connection for instant updates:

```mermaid
sequenceDiagram
    participant C1 as Client 1
    participant S as Server / Master
    participant C2 as Client 2

    Note over C1,C2: Both clients connected via WebSocket

    C1->>C1: ① User creates file.txt
    C1->>S: ② POST /api/v1/buckets/{bucket}/files
    S->>S: ③ Store file
    S-->>C2: ④ WebSocket: {"name": "added", "src": {...}}
    C2->>S: ⑤ GET /api/v1/buckets/{bucket}/files/download
    S-->>C2: ⑥ File content
    C2->>C2: ⑦ Write file.txt locally
```

## Event handling

When the client receives a remote event via WebSocket, it processes it
according to the event type:

```mermaid
flowchart TD
    E[① Event received] -->|"②"| T{Event type?}

    T -->|"③a added"| A[Download file]
    T -->|"③b modified"| M[Download file]
    T -->|"③c renamed"| R[Rename local file]
    T -->|"③d deleted"| D[Delete local file]
    T -->|"③e symlink"| L[Create symlink]

    A -->|"④"| V{Verify SHA-256}
    M -->|"④"| V
    V -->|"⑤a Match"| S[Success]
    V -->|"⑤b Mismatch"| RE[Retry download]
```

## Conflict resolution

The client uses "last write wins" based on modification time (mtime):

```mermaid
flowchart TD
    C[① Compare files] -->|"②"| T{mtime comparison}

    T -->|"③a remote > local"| D[Download from remote]
    T -->|"③b local > remote"| U[Upload to remote]
    T -->|"③c equal"| H{④ SHA-256 match?}

    H -->|"⑤a Yes"| S[Skip - files identical]
    H -->|"⑤b No"| U2[Upload local version]
```

## Event queue and retry

The client queues outbound events (local file changes detected by the watchdog)
and sends them to the server via WebSocket with retry logic:

```mermaid
stateDiagram-v2
    [*] --> Queued: ① Add event
    Queued --> Processing: ② Dequeue
    Processing --> Success: ③a Event sent
    Processing --> Retry: ③b Network error
    Retry --> Queued: ④ Backoff wait
    Retry --> Failed: ⑤ Max retries
    Success --> [*]
    Failed --> [*]

    note right of Retry
        Backoff: 1s, 2s, 4s, 8s...
        Max: 60s
        Max retries: 5
    end note
```

### Queue interaction with the sync engine

The event queue and the sync engine operate independently but coordinate
through shared state:

- **Event queue** handles real-time propagation: local watchdog events are
  queued immediately and sent to the server as soon as the WebSocket is
  available. Events are not batched — each file change produces a separate
  event.
- **Sync engine** handles full reconciliation: on startup, on reconnection,
  and periodically via `HSYNC_RESYNC`. It compares the full local and remote
  file lists and generates `SyncAction`s.
- **Coordination**: During a full sync, the event queue is drained before
  the sync engine computes its actions. This ensures that events generated
  during the sync window are not lost and are processed before the next
  reconciliation cycle.
- **Debouncing**: The watchdog debounces rapid successive writes to the same
  file (e.g., an editor saving multiple times). On Linux, `on_closed` provides
  natural debouncing; on macOS/Windows, a 1-second debounce window is applied.

> For the three-way merge algorithm used during conflict resolution, see
> [Conflict Resolution](conflict-resolution.md).

## Connection recovery

When the WebSocket connection is lost, the client reconnects and resyncs:

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server / Master

    C->>S: ① WebSocket connected
    Note over C,S: ② Connection lost

    C->>C: ③ Wait reconnect_delay seconds
    C->>S: ④ Reconnect WebSocket
    S-->>C: ⑤ Connection accepted
    C->>S: ⑥ Authenticate
    S-->>C: ⑦ Auth OK

    C->>S: ⑧ GET /api/v1/buckets/{bucket}/files
    S-->>C: ⑨ Current file list
    C->>C: ⑩ Resync to catch missed events
```

## File stability guard

Before uploading a file, the client waits until the file has stopped changing.
This prevents uploading a half-written file:

- **Fast path (Linux `inotify` `IN_CLOSE_WRITE`)**: the kernel signals that the
  writer closed the file descriptor. The client performs a short re-check
  (0.3 s) and uploads immediately — sub-second propagation.
- **Slow path (polling / non-Linux)**: the client polls size and mtime until
  they settle for `STABILITY_DELAY` seconds (default 5 s).

If a file never stabilises within `MAX_WAIT_TIMEOUT` (default 120 s), the
upload is rescheduled rather than uploading a torn file.

## Delta Transfer (rsync-style block sync)

For large files with small changes, HSYNC can transfer **only the changed blocks**
instead of the whole file. This rsync-style delta transfer significantly reduces
bandwidth usage and transfer time.

### When delta transfer triggers

Delta transfer is attempted automatically when **all** of these conditions are met:

1. **File size threshold**: file is ≥ 256 KB (server mode) or ≥ 1 MB (LAN mode)
2. **Base hash exists**: a previously synced version of the file is recorded in
   `.hsyncstate` (three-way merge state)
3. **Direction check**:
   - **Upload**: local file changed (`local_sha256 != base_hash`)
   - **Download**: local file is still the base (`local_sha256 == base_hash`) and
     remote file changed (`remote_sha256 != base_hash`)
4. **Capability**: both local and remote peers support delta transfer

If any condition is not met, the file is transferred in full — you never have to
choose manually.

### Upload delta flow

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server / Master

    Note over C,S: ① Client detects local file changed
    C->>S: GET /delta/signatures?path=file.bin
    S-->>C: ② Block signatures of server's base
    C->>C: ③ Rolling checksum match (Adler32 + SHA-256)
    C->>C: ④ Compute delta ops (copy/data)
    C->>S: POST /delta/apply (metadata + literal payload)
    S->>S: ⑤ Rebuild file from ops
    S->>S: ⑥ Verify SHA-256
    S-->>C: ⑦ Success (200) or Conflict (409)
    Note over C: On failure → fall back to full upload
```

### Download delta flow

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server / Master

    Note over C,S: ① Client detects remote file changed
    C->>C: ② Compute block signatures of local base
    C->>S: POST /delta/signatures (path + signatures)
    S->>S: ③ Match against server's new file
    S-->>C: ④ Delta instructions + data_url (or delta:false)
    C->>S: GET /delta/data/{token}
    S-->>C: ⑤ Literal payload
    C->>C: ⑥ Rebuild file from ops
    C->>C: ⑦ Verify SHA-256
    Note over C: On failure → fall back to full download
```

### Fallback behavior

Delta transfer failures are **transparent**: if any step fails (network error,
hash mismatch, peer doesn't support delta, base changed mid-transfer), the client
automatically falls back to a full file transfer. The retry policy is unchanged —
delta failures do not consume retry attempts.

### Configuration

Delta transfer is **enabled by default**. Disable it with:

```bash
# Environment variable
export HSYNC_DELTA_ENABLED=false

# Or in code
config.delta_enabled = False
```

> For details on delta transfer thresholds and protocol-level negotiation, see
> the [Configuration](configuration.md#delta-transfer) section.

## See also

- [Usage](usage.md) — CLI options reference and log labels.
- [Configuration](configuration.md) — environment variables and CLI options.
- [Sync Modes](sync-modes.md) — single, watch, and LAN mode comparison.
- [Server Sync Flow](https://github.com/albertobasso/hsync-server/blob/main/docs/sync-flow.md) — the server's perspective.
- [Server Delta Transfer Guide](https://github.com/albertobasso/hsync-server/blob/main/docs/delta-transfer.md) — server-side delta transfer endpoints and configuration.
