# HSYNC Client Usage Guide

This document provides detailed usage instructions for the HSYNC Client.

> **Note**: Diagrams are written in Mermaid. If they don't render, run `./generate-diagrams.sh` to generate PNG images in the `images/` directory.

## Quick Start

```mermaid
flowchart LR
    A[Install] -->|"①→②"| B[Configure]
    B -->|"②→③"| C[Run]

    subgraph "① Install"
        A1[poetry install]
    end

    subgraph "② Configure"
        B1[Set HSYNC_API_KEY]
    end

    subgraph "③ Run"
        C1[hsync -s URL -b bucket]
    end
```

![Quick Start](images/usage-1.png)

## Command Line Interface

### Basic Syntax

```bash
hsync -s <server_url> -b <bucket_name> [options]
```

### Options Overview

```mermaid
mindmap
  root((hsync))
    Required
      -s, --server
      -b, --bucket
    Paths
      -p, --path
    Sync Options
      -w, --watch
      -i, --interval
      --stability-delay
      --sync-direction
      --ignore-delete
    Network
      --retry-delay
      --insecure
    Output
      -v, --verbose
      -q, --quiet
      -o, --output
      --no-color
    Safety
      -y, --yes
    Other
      -n, --dry-run
      -V, --version
      --help-symbols
```

![CLI Options](images/usage-2.png)

## Usage Scenarios

### Scenario 1: One-Time Sync

Synchronize files once and exit:

```mermaid
sequenceDiagram
    participant U as User
    participant C as Client
    participant S as Server

    U->>C: ① hsync -s http://server -b mybucket
    C->>S: ② Fetch remote file list
    C->>C: ③ Compare with local files
    C->>S: ④ Upload newer local files
    S->>C: ⑤ Download newer remote files
    C->>U: ⑥ Sync complete, exit
```

![One-Time Sync](images/usage-3.png)

**Command:**
```bash
export HSYNC_API_KEY="your-api-key"
hsync -s http://localhost:8765 -b mybucket -p /path/to/local/dir
```

### Scenario 2: Continuous Sync (Watch Mode)

Keep files synchronized in real-time:

```mermaid
sequenceDiagram
    participant U as User
    participant C as Client
    participant S as Server

    U->>C: ① Start watch mode
    C->>S: ② Initial sync
    C->>S: ③ Connect WebSocket

    loop ④ Watch Mode
        alt ④a Local file changed
            C->>S: ⑤a Upload file
            S-->>C: Broadcast to others
        else ④b Remote event received
            S-->>C: ⑤b Event notification
            C->>S: ⑥ Request file
            S->>C: ⑦ Download file
        end
    end
```

![Continuous Sync](images/usage-4.png)

**Command:**
```bash
hsync -s http://localhost:8765 -b mybucket -w
```

### Scenario 3: Periodic Re-sync

Watch mode with periodic full sync:

```mermaid
gantt
    title Sync Timeline
    dateFormat HH:mm
    axisFormat %H:%M

    section ① Initial
    Initial Sync     :done, 00:00, 1m

    section ② Watch
    Real-time events :active, 00:01, 5m

    section ③ Periodic
    Full Re-sync     :crit, 00:05, 1m
    Real-time events :active, 00:06, 5m
    Full Re-sync     :crit, 00:10, 1m
```

![Periodic Re-sync](images/usage-5.png)

**Command:**
```bash
hsync -s http://localhost:8765 -b mybucket -w -i 300
```

### Scenario 4: Dry Run (Preview)

See what would happen without making changes:

```bash
hsync -s http://localhost:8765 -b mybucket -n
```

**Output example:**
```
[↓] Would download: documents/report.pdf
[↑] Would upload: images/photo.jpg
[✕] Would delete: old/deprecated.txt
```

### Scenario 5: Upload-Only Sync (Backup Source)

Push local files to the server without downloading remote changes — useful for backup sources:

```mermaid
sequenceDiagram
    participant U as User
    participant C as Client
    participant S as Server

    U->>C: ① hsync --sync-direction upload-only
    C->>S: ② Fetch remote file list
    C->>C: ③ Compare with local files
    C->>S: ④ Upload newer local files
    Note right of C: ⑤ Downloads are SKIPPED
    C->>U: ⑥ Sync complete
```

**Command:**
```bash
# Only upload local changes — remote-only files are ignored
hsync -s http://localhost:8765 -b backup -p /path/to/source -w --sync-direction upload-only
```

### Scenario 6: Download-Only Sync (Read-Only Mirror)

Pull remote files locally without uploading local changes — useful for read-only clients:

```mermaid
sequenceDiagram
    participant U as User
    participant C as Client
    participant S as Server

    U->>C: ① hsync --sync-direction download-only
    C->>S: ② Fetch remote file list
    C->>C: ③ Compare with local files
    Note left of C: ④ Uploads are SKIPPED
    S->>C: ⑤ Download newer remote files
    C->>U: ⑥ Sync complete
```

**Command:**
```bash
# Only download from server — local-only files are never uploaded
hsync -s http://localhost:8765 -b shared -p /path/to/mirror -w --sync-direction download-only
```

### Scenario 7: Multi-Client with Different Directions

Multiple clients can connect to the same bucket with different sync directions:

```mermaid
flowchart LR
    A["🖥️ Client A<br/><small>upload-only</small>"] -->|"Upload"| S["🗄️ Server<br/><small>Bucket: photos</small>"]
    S -->|"Download"| B["💻 Client B<br/><small>download-only</small>"]
    C["📱 Client C<br/><small>bidirectional</small>"] <-->|"Upload & Download"| S

    style A fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    style B fill:#e3f2fd,stroke:#1565c0,color:#0d47a1
    style C fill:#f3e5f5,stroke:#7b1fa2,color:#7b1fa2
    style S fill:#fff3e0,stroke:#e65100,color:#e65100
```

The server is a **blind relay** — it broadcasts all events to all connected clients. Each client independently decides which events to process based on its own `--sync-direction` setting.

## Log Symbols

| Symbol | Meaning | Description |
|--------|---------|-------------|
| `·` | Skip | File unchanged |
| `↓` | Download | Downloading from server |
| `↑` | Upload | Uploading to server |
| `→` | Move | Renaming/moving file |
| `✕` | Delete | Removing file |
| `+` | Create | Creating directory |
| `⚙` | Permissions | Setting file permissions |
| `L` | Symlink | Creating symbolic link |

## Error Handling

```mermaid
flowchart TD
    E[① Error Occurs] -->|"②"| T{Error Type}

    T -->|"③a Network"| N[Retry with backoff]
    T -->|"③b Auth"| A[Check API key]
    T -->|"③c Not Found"| NF[Skip file]
    T -->|"③d Permission"| P[Log warning]

    N -->|"④"| R{Retry count}
    R -->|"⑤a < 5"| W[Wait and retry]
    R -->|"⑤b >= 5"| F[Mark as failed]

    W -->|"⑥ Retry"| E
```

![Error Handling](images/usage-6.png)

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HSYNC_SERVER` | Default server URL | (use `-s`) |
| `HSYNC_API_KEY` | API key for authentication | (required) |
| `HSYNC_MAX_CONCURRENT_TRANSFERS` | Max parallel file transfers | `5` |
| `HSYNC_STABILITY_DELAY` | Seconds to wait before uploading modified files | `5` |

### Environment Files

Use the provided environment templates for easy setup:

```bash
# Development (HTTP, localhost)
source dev.env
hsync -b mybucket -w

# Production (HTTPS, remote server)
source prod.env
hsync -b mybucket -w
```

## Examples

### Sync with verbose output
```bash
hsync -s http://localhost:8765 -b mybucket -v
```

### Sync with file logging
```bash
hsync -s http://localhost:8765 -b mybucket -w -o /var/log/hsync.log
```

### Sync without SSL verification (development)
```bash
hsync -s https://dev-server:8765 -b mybucket --insecure
```

### Sync with custom retry delay
```bash
hsync -s http://localhost:8765 -b mybucket -w --retry-delay 10
```

### Sync with longer stability delay (for slow storage or large files)
```bash
hsync -s http://localhost:8765 -b mybucket -w --stability-delay 10
```

### Upload-only sync (backup source)
```bash
hsync -s http://localhost:8765 -b backup -w --sync-direction upload-only
```

### Download-only sync (read-only mirror)
```bash
hsync -s http://localhost:8765 -b shared -w --sync-direction download-only
```

### LAN mode with download-only slave
```bash
# Slave only receives files from master, never uploads local changes
hsync -b photos -p ~/Pictures --lan-mode --sync-direction download-only
```

### Skip deletion risk confirmation (scripting)
```bash
# Automatically confirm the deletion risk warning (still shows the warning)
hsync -b photos -p ~/Pictures --lan-mode -y

# Long form
hsync -b shared -w --sync-direction download-only --yes
```

> **Note**: In LAN mode, `--force-master` cannot be combined with `--sync-direction upload-only` or `download-only` (the client will exit with an error). During automatic election, bidirectional peers are preferred as master. See [LAN Mode > Master Election](docs/lan-mode.md#master-election) for the full election algorithm.
