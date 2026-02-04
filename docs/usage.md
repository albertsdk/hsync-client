# HSYNC Client Usage Guide

This document provides detailed usage instructions for the HSYNC Client.

> **Note**: Diagrams are written in Mermaid. If they don't render, run `./generate-diagrams.sh` to generate PNG images in the `images/` directory.

## Quick Start

```mermaid
flowchart LR
    A[Install] --> B[Configure]
    B --> C[Run]
    
    subgraph "1. Install"
        A1[poetry install]
    end
    
    subgraph "2. Configure"
        B1[Set HSYNC_API_KEY]
    end
    
    subgraph "3. Run"
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
      --order-by
      --stability-delay
    Network
      --retry-delay
      --insecure
    Output
      -v, --verbose
      -q, --quiet
      -o, --output
      --no-color
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

    U->>C: hsync -s http://server -b mybucket
    C->>S: Fetch remote file list
    C->>C: Compare with local files
    C->>S: Upload newer local files
    C->>S: Download newer remote files
    C->>U: Sync complete, exit
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

    U->>C: hsync -s http://server -b mybucket -w
    C->>S: Initial sync
    C->>S: Connect WebSocket

    loop Watch Mode
        alt Local file changed
            C->>S: Upload file
            S->>C: Broadcast to others
        else Remote event received
            S->>C: Event notification
            C->>S: Download file
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
    
    section Initial
    Initial Sync     :done, 00:00, 1m
    
    section Watch
    Real-time events :active, 00:01, 5m
    
    section Periodic
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
    E[Error Occurs] --> T{Error Type}
    
    T -->|Network| N[Retry with backoff]
    T -->|Auth| A[Check API key]
    T -->|Not Found| NF[Skip file]
    T -->|Permission| P[Log warning]
    
    N --> R{Retry count}
    R -->|< 5| W[Wait and retry]
    R -->|>= 5| F[Mark as failed]

    W --> E
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
