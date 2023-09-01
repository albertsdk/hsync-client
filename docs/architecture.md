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

    CLI --> CFG
    CFG --> SYNC
    
    SYNC --> HTTP
    SYNC --> WS
    
    WD -->|File Events| EQ
    EQ -->|Queued Events| WS
    
    WS <-->|Events| SERVER
    HTTP <-->|REST API| SERVER
    
    SYNC <--> FS
    WD --> FS
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
    [*] --> Initialize
    Initialize --> FetchRemote: Get remote file list
    FetchRemote --> ScanLocal: Scan local directory
    ScanLocal --> Compare: Compare files
    Compare --> Execute: Execute sync actions
    Execute --> [*]: Complete
```

![Single Sync Mode](images/architecture-3.png)

### Watch Mode (Continuous)

```mermaid
stateDiagram-v2
    [*] --> Initialize
    Initialize --> InitialSync: Perform initial sync
    InitialSync --> Connected: Connect WebSocket
    
    state Connected {
        [*] --> Watching
        Watching --> LocalEvent: File changed locally
        Watching --> RemoteEvent: Event from server
        
        LocalEvent --> Upload: Upload file
        Upload --> Watching
        
        RemoteEvent --> Download: Download file
        Download --> Watching
    }
    
    Connected --> Reconnecting: Connection lost
    Reconnecting --> Connected: Reconnected
    Reconnecting --> [*]: Max retries exceeded
```

![Watch Mode](images/architecture-4.png)

## Event Processing

```mermaid
flowchart TD
    subgraph "Local Events (Watchdog)"
        L1[File Created]
        L2[File Modified]
        L3[File Deleted]
        L4[File Moved]
    end
    
    subgraph "Event Queue"
        Q[Queue with Retry]
    end
    
    subgraph "WebSocket"
        WS[Send to Server]
    end
    
    L1 --> Q
    L2 --> Q
    L3 --> Q
    L4 --> Q
    Q --> WS
    
    subgraph "Remote Events"
        R1[added]
        R2[modified]
        R3[deleted]
        R4[renamed]
        R5[symlink]
    end
    
    subgraph "Local Actions"
        A1[Download File]
        A2[Delete File]
        A3[Rename File]
        A4[Create Symlink]
    end
    
    R1 --> A1
    R2 --> A1
    R3 --> A2
    R4 --> A3
    R5 --> A4
```

![Event Processing](images/architecture-5.png)

## Configuration Flow

```mermaid
flowchart LR
    ENV[Environment Variables] --> CFG[Configuration]
    CLI[CLI Arguments] --> CFG
    CFG --> VALIDATE[Validation]
    VALIDATE --> RUN[Run Client]

    subgraph "Config Sources"
        ENV
        CLI
    end

    subgraph "Config Values"
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

## Parallel File Transfers

The sync engine uses a thread pool with semaphore-based concurrency control:

```mermaid
flowchart TB
    subgraph "Sync Engine"
        SYNC[sync]
        ACTIONS[Separate Actions]
        PARALLEL[_execute_parallel_transfers]
    end

    subgraph "Thread Pool"
        SEM[Semaphore<br/>max=5]
        T1[Thread 1]
        T2[Thread 2]
        T3[Thread 3]
        T4[Thread ...]
        T5[Thread N]
    end

    subgraph "Transfers"
        D1[Download 1]
        D2[Download 2]
        U1[Upload 1]
        U2[Upload 2]
    end

    subgraph "Server"
        API[REST API]
    end

    SYNC --> ACTIONS
    ACTIONS -->|downloads + uploads| PARALLEL
    PARALLEL --> SEM
    SEM --> T1 & T2 & T3 & T4 & T5
    T1 --> D1
    T2 --> D2
    T3 --> U1
    T4 --> U2
    D1 & D2 & U1 & U2 --> API
```

![Parallel Transfers](images/architecture-7.png)

**Configuration:**
- `HSYNC_MAX_CONCURRENT_TRANSFERS`: Controls maximum parallel transfers (default: 5)
- Uses `asyncio.Semaphore` to limit concurrent operations
- Uses `ThreadPoolExecutor` for I/O-bound operations
