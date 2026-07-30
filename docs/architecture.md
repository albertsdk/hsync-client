# HSYNC Client Architecture

This document describes the architecture of the HSYNC Client.

> The client builds on the shared models, constants, and utilities provided by
> `hsync-common`. See [HSYNC Common Architecture](https://github.com/albertobasso/hsync-common/blob/main/docs/architecture.md)
> for the foundation it depends on, and the
> [Architecture Decision Records](https://github.com/albertobasso/hsync-common/blob/main/docs/adr/README.md) for the
> *why* behind the ecosystem's stable decisions.

## System Overview

```mermaid
graph TB
    subgraph "HSYNC Client"
        subgraph "CLI Layer"
            CLI[Argument Parser]
            CFG[Configuration]
        end

        subgraph "Core Services"
            SYNC[Sync Engine]
            WD[Watchdog Observer]
            EQ[Event Queue]
            WS[WebSocket Client]
        end

        subgraph "API Layer"
            HTTP[HTTP Client]
            AUTH[Authentication]
        end

        subgraph "Models"
            FILE[File]
            DIR[Dir]
            EVENT[Event]
            SYMLINK[Symlink]
        end
    end

    subgraph "Local"
        FS[(File System)]
    end

    subgraph "Remote"
        SERVER[HSYNC Server]
    end

    CLI -->|"① Parse args"| CFG
    CFG -->|"② Configure"| SYNC

    SYNC -->|"③ HTTP requests"| HTTP
    SYNC -->|"④ WS messages"| WS

    WD -->|"⑤ File events"| EQ
    EQ -->|"⑥ Queued events"| WS

    WS -->|"⑦ Send events"| SERVER
    SERVER -.->|"⑧ Receive events"| WS
    HTTP -->|"⑨ Upload/Download"| SERVER

    FS -->|"⑩ Monitor"| WD
    SYNC -->|"⑪ Read/Write"| FS
```

## Component Description

| Component | Description |
|-----------|-------------|
| **CLI Layer** | Parses command-line arguments and configures the client |
| **Sync Engine** | Orchestrates file synchronization logic |
| **Watchdog Observer** | Monitors local file system for changes |
| **Event Queue** | Queues events with retry and exponential backoff |
| **WebSocket Client** | Maintains real-time connection with server |
| **HTTP Client** | Handles REST API requests (upload/download) |
| **Models** | Data structures for files, directories, events |

## Synchronization Modes

### Single Sync Mode

```mermaid
stateDiagram-v2
    [*] --> Initialize: ① Start
    Initialize --> FetchRemote: ② Get remote file list
    FetchRemote --> ScanLocal: ③ Scan local directory
    ScanLocal --> Compare: ④ Compare files
    Compare --> Execute: ⑤ Execute sync actions
    Execute --> [*]: ⑥ Complete
```

### Watch Mode (Continuous)

```mermaid
stateDiagram-v2
    [*] --> Initialize: ① Start
    Initialize --> InitialSync: ② Perform initial sync
    InitialSync --> Connected: ③ Connect WebSocket

    state Connected {
        [*] --> Watching
        Watching --> LocalEvent: ④a File changed locally
        Watching --> RemoteEvent: ④b Event from server

        LocalEvent --> Upload: ⑤a Upload file
        Upload --> Watching

        RemoteEvent --> Download: ⑤b Download file
        Download --> Watching
    }

    Connected --> Reconnecting: ⑥ Connection lost
    Reconnecting --> Connected: ⑦ Reconnected
    Reconnecting --> [*]: ⑧ Max retries exceeded
```

### Sync Direction Filtering

The `--sync-direction` option controls which operations are executed. Filtering is applied at two levels:

1. **Initial sync** — `get_sync_actions()` removes actions that don't match the direction before execution
2. **Watch mode** — Inline runtime checks skip events that don't match the direction

```
┌─────────────────────────────────────────────────────────┐
│                  Sync Direction Filter                  │
├─────────────────┬───────────────┬───────────────────────┤
│  Direction      │  Allowed      │  Blocked              │
├─────────────────┼───────────────┼───────────────────────┤
│  bidirectional  │  all actions  │  (none)               │
│  upload-only    │  upload       │  download, delete(*)  │
│  download-only  │  download,    │  upload               │
│                 │  delete       │                       │
└─────────────────┴───────────────┴───────────────────────┘
(*) Remote-triggered deletes are blocked in upload-only mode
```

The Observer (watchdog) continues running in all modes — events are filtered inline rather than stopping the Observer. This allows the sync direction to be changed dynamically at runtime via stdin command without restarting the process.

> **LAN Mode note**: In LAN mode, the `sync_direction` is also published via mDNS TXT records and used during master election — peers with **bidirectional** sync are preferred as master. Using `--force-master` with a non-bidirectional direction is an error. See [LAN Mode > Master Election](lan-mode.md#master-election) for details.

## Event Processing

```mermaid
flowchart TD
    subgraph "① Local Events (Watchdog)"
        L1[on_created]
        L2[on_closed / on_modified]
        L3[on_deleted]
        L4[on_moved]
    end

    subgraph "② Processing"
        STABLE[File Stability Check]
        UPLOAD[Upload to Server]
    end

    subgraph "③ Event Queue"
        Q[Queue with Retry]
    end

    subgraph "④ WebSocket"
        WS[Send to Server]
    end

    L1 -->|"①→③"| Q
    L2 -->|"①→②"| STABLE
    STABLE -->|"②"| UPLOAD
    L3 -->|"①→③"| Q
    L4 -->|"①→③"| Q
    Q -->|"③→④"| WS

    subgraph "⑤ Remote Events"
        R1[added]
        R2[modified]
        R3[deleted]
        R4[renamed]
        R5[symlink]
    end

    subgraph "⑥ Local Actions"
        A1[Download File]
        A2[Delete File]
        A3[Rename File]
        A4[Create Symlink]
    end

    R1 -->|"⑤→⑥"| A1
    R2 -->|"⑤→⑥"| A1
    R3 -->|"⑤→⑥"| A2
    R4 -->|"⑤→⑥"| A3
    R5 -->|"⑤→⑥"| A4
```

**Sync direction filtering in watch mode:**

- When `upload-only`: remote events (⑤) are **skipped** — only local events (①) are processed and uploaded
- When `download-only`: local events (①) are **skipped** — only remote events (⑤) are processed and downloaded
- When `bidirectional` (default): all events are processed normally

## File Change Detection

The client uses the **watchdog** library to monitor the local file system. The detection mechanism varies by platform:

```mermaid
flowchart TD
    subgraph "① File Modified"
        FM[File Write Detected]
    end

    FM -->|"②"| P{Platform?}

    subgraph "③a Linux (inotify)"
        P -->|"Linux"| OC[on_closed event]
        OC -->|"④a"| STABLE[Check file stability]
    end

    subgraph "③b macOS / Windows"
        P -->|"macOS/Windows"| OM[on_modified event]
        OM -->|"④b"| DEBOUNCE[Debounce 1 second]
        DEBOUNCE --> STABLE
    end

    STABLE -->|"⑤"| CHECK{Size & mtime stable?}
    CHECK -->|"No"| WAIT[Wait stability delay]
    WAIT --> CHECK
    CHECK -->|"⑥ Yes"| UPLOAD[Upload file]
```

**Platform-specific behavior:**

| Platform | Event Used | Reason |
|----------|------------|--------|
| **Linux** | `on_closed` | Triggered when file is closed after writing, avoiding multiple events during write |
| **macOS** | `on_modified` + debounce | FSEvents doesn't support `on_closed`, uses 1-second debounce |
| **Windows** | `on_modified` + debounce | ReadDirectoryChangesW doesn't support `on_closed`, uses 1-second debounce |

**File stability check:**

Before uploading, the client verifies the file has stopped being written by checking:
1. **File size** - must remain constant over the stability delay period
2. **Modification time (mtime)** - must remain constant over the stability delay period

The stability delay defaults to **5 seconds** (inspired by Syncthing's default of 10 seconds) and can be configured via:
- CLI: `--stability-delay SEC`
- Environment variable: `HSYNC_STABILITY_DELAY`

**When to increase the stability delay:**
- Large files on slow storage (network filesystems, HDDs)
- Applications that write in bursts with pauses between writes
- When experiencing premature uploads of incomplete files

This prevents uploading incomplete files during long write operations.

## Configuration Flow

```mermaid
flowchart LR
    subgraph "① Config Sources"
        ENV[Environment Variables]
        CLI[CLI Arguments]
    end

    ENV -->|"②"| CFG[Configuration]
    CLI -->|"②"| CFG
    CFG -->|"③"| VALIDATE[Validation]
    VALIDATE -->|"④"| SAFETY[Deletion Risk Check]
    SAFETY -->|"⑤"| RUN[Run Client]

    subgraph "⑤ Config Values"
        S[Server URL]
        B[Bucket Name]
        P[Local Path]
        K[API Key]
        O[Options]
    end

    CFG --> S
    CFG --> B
    CFG --> P
    CFG --> K
    CFG --> O
```

### Pre-Sync Safety Check (Deletion Risk)

Before starting synchronization, the client runs a **deletion risk check** (`_check_deletion_risk()` in `main_logic.py`). If the local directory is non-empty and the sync settings could cause file deletions, the user is warned and asked for confirmation:

| Condition | Risk Level | Behavior |
|-----------|------------|----------|
| `--sync-direction upload-only` | ✅ Safe | No warning |
| LAN mode + `--force-master` | ✅ Safe | No warning |
| Bidirectional server mode | ✅ Safe | No warning (normal behavior) |
| LAN mode without `--force-master` | ⚠️ Risky | Warning + confirmation prompt |
| `--sync-direction download-only` | ⚠️ Risky | Warning + confirmation prompt |
| Empty directory | ✅ Safe | No warning (nothing to delete) |

When `-y` / `--yes` is passed, the warning is still logged but the interactive prompt is skipped. The desktop client (`hsync-desktop`) shows its own confirmation dialog and always passes `--yes` to the CLI process.

## Parallel File Transfers

The sync engine uses a thread pool with semaphore-based concurrency control:

```mermaid
flowchart TB
    subgraph "① Sync Engine"
        SYNC[sync]
        ACTIONS[Separate Actions]
        PARALLEL[_execute_parallel_transfers]
    end

    subgraph "② Thread Pool"
        SEM[Semaphore<br/>max=5]
        T1[Thread 1]
        T2[Thread 2]
        T3[Thread 3]
        T4[Thread ...]
        T5[Thread N]
    end

    subgraph "③ Transfers"
        D1[Download 1]
        D2[Download 2]
        U1[Upload 1]
        U2[Upload 2]
    end

    subgraph "④ Server"
        API[REST API]
    end

    SYNC -->|"①"| ACTIONS
    ACTIONS -->|"② downloads + uploads"| PARALLEL
    PARALLEL -->|"③"| SEM
    SEM -->|"④"| T1 & T2 & T3 & T4 & T5
    T1 -->|"⑤"| D1
    T2 -->|"⑤"| D2
    T3 -->|"⑤"| U1
    T4 -->|"⑤"| U2
    D1 & D2 & U1 & U2 -->|"⑥"| API
```

**Configuration:**
- `HSYNC_MAX_CONCURRENT_TRANSFERS`: Controls maximum parallel transfers (default: 5)
- Uses `asyncio.Semaphore` to limit concurrent operations
- Uses `ThreadPoolExecutor` for I/O-bound operations


> For module structure, performance optimizations, error handling internals, and
> the full project layout, see the [Developer Guide](developer.md).
