# HSYNC Client Architecture

This document describes the architecture of the HSYNC Client.

> **Note**: Diagrams are written in Mermaid. If they don't render, run `./generate-diagrams.sh` to generate PNG images in the `images/` directory.

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

![System Overview](images/architecture-1.png)

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

        subgraph "models/"
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
```

![Module Structure](images/architecture-2.png)

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

![Single Sync Mode](images/architecture-3.png)

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

![Watch Mode](images/architecture-4.png)

### Sync Direction Filtering

The `--sync-direction` option controls which operations are executed. Filtering is applied at two levels:

1. **Initial sync** — `get_sync_actions()` removes actions that don't match the direction before execution
2. **Watch mode** — Inline runtime checks skip events that don't match the direction

```
┌─────────────────────────────────────────────────────────┐
│                  Sync Direction Filter                    │
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

![Event Processing](images/architecture-5.png)

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

![File Change Detection](images/architecture-8.png)

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

![Configuration Flow](images/architecture-6.png)

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

![Parallel Transfers](images/architecture-7.png)

**Configuration:**
- `HSYNC_MAX_CONCURRENT_TRANSFERS`: Controls maximum parallel transfers (default: 5)
- Uses `asyncio.Semaphore` to limit concurrent operations
- Uses `ThreadPoolExecutor` for I/O-bound operations

## Performance Optimizations

### MD5 Cache with mtime Validation

To avoid redundant MD5 computations during a single sync operation, the client maintains an in-memory cache that maps file paths to `(md5sum, mtime)` tuples. On each `File.get()` call, the cached MD5 is returned only if the file's modification time hasn't changed — otherwise the entry is automatically invalidated and recomputed.

- Cache is cleared at the start of each sync cycle (`sync()`, `_run_preview()`)
- Individual entries are invalidated after downloads/uploads and before watchdog `File.get()` calls
- mtime validation provides automatic staleness detection even if manual invalidation is missed

### Adaptive Chunk Size for MD5 Computation

MD5 hashing uses different I/O chunk sizes based on file size for optimal throughput:

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

See [Configuration > Bandwidth Limiting](configuration.md#bandwidth-limiting) for usage details.

## Project Structure

```
hsync-client/
├── hsync_client/           # Main Python package (source code)
│   ├── __init__.py
│   ├── __main__.py         # Module entry point
│   ├── main.py             # Main entry point
│   ├── main_logic.py       # Core sync orchestration
│   ├── cli.py              # CLI argument parsing (Typer)
│   ├── config.py           # Configuration management (pydantic-settings)
│   ├── constants.py        # Application constants
│   ├── sync.py             # Sync engine with parallel transfers
│   ├── transfer.py         # File upload/download operations
│   ├── websocket_handler.py # WebSocket event handling
│   ├── event_queue.py      # Event queue with retry logic
│   ├── lan.py              # LAN mode discovery (mDNS/Zeroconf)
│   ├── lan_server.py       # LAN mode HTTP server (master node)
│   ├── logger.py           # Logging configuration (loguru)
│   ├── retry.py            # Retry logic (tenacity)
│   ├── ssl_utils.py        # SSL/TLS utilities
│   ├── exceptions.py       # Custom exceptions
│   ├── utils.py            # Utility functions
│   ├── validators.py       # Input validators
│   └── models/             # Data models
│       ├── __init__.py
│       ├── dir.py          # Directory model
│       ├── event.py        # Event model
│       ├── file.py         # File model
│       ├── symlink.py      # Symbolic link model
│       └── sync_action.py  # Sync action model
├── docs/                   # Documentation
│   ├── architecture.md     # Architecture overview
│   ├── configuration.md    # Configuration guide
│   ├── getting-started.md  # Getting started guide
│   ├── lan-mode.md         # LAN mode (P2P) documentation
│   ├── sync-modes.md       # Synchronization modes
│   └── usage.md            # Usage guide
├── tests/                  # Test files
├── dist/                   # Compiled binary (created by compile.sh)
│   └── hsync               # Standalone executable
├── dev.env                 # Development environment template
├── prod.env                # Production environment template
├── compile.sh              # Build script (PyInstaller)
├── pyproject.toml          # Poetry configuration
├── poetry.lock             # Locked dependencies
└── README.md
```
