# HSYNC LAN Mode (P2P Synchronization)

This document describes the LAN Mode feature for peer-to-peer file synchronization without a centralized server.

> **Note**: Diagrams are written in Mermaid. If they don't render, run `./generate-diagrams.sh` to generate PNG images in the `images/` directory.

## Overview

LAN Mode enables direct peer-to-peer file synchronization between devices on the same local network, without requiring a centralized HSYNC server.

```mermaid
flowchart TB
    subgraph "① Traditional Mode"
        C1A[Client A] -->|"sync"| S[Server]
        C1B[Client B] -->|"sync"| S
        C1C[Client C] -->|"sync"| S
    end

    subgraph "② LAN Mode (P2P)"
        C2A[Client A<br/>Master] <-->|"sync"| C2B[Client B]
        C2A <-->|"sync"| C2C[Client C]
    end

    style S fill:#fff3e0,stroke:#e65100
    style C2A fill:#e8f5e9,stroke:#2e7d32
    style C2B fill:#e3f2fd,stroke:#1565c0
    style C2C fill:#f3e5f5,stroke:#7b1fa2
```

![LAN Mode Overview](images/lan-mode-1.png)

## Key Features

| Feature | Description |
|---------|-------------|
| **Zero Configuration** | Automatic peer discovery via mDNS/Bonjour |
| **No Server Required** | Works without internet or central server |
| **Automatic Master Election** | Lowest CID becomes the master |
| **Optional Authentication** | Shared secret key for security |
| **Random Port Allocation** | Avoids port conflicts |

## How It Works

### Architecture

```mermaid
flowchart TB
    subgraph "LAN Network"
        subgraph "Master (lowest CID)"
            M_DISC[mDNS Service]
            M_HTTP[HTTP Server]
            M_FILES[(Local Files)]
        end

        subgraph "Client A"
            A_DISC[mDNS Discovery]
            A_SYNC[Sync Engine]
            A_FILES[(Local Files)]
        end

        subgraph "Client B"
            B_DISC[mDNS Discovery]
            B_SYNC[Sync Engine]
            B_FILES[(Local Files)]
        end
    end

    M_DISC -.->|"① Announce"| A_DISC
    M_DISC -.->|"① Announce"| B_DISC

    A_SYNC -->|"② HTTP API"| M_HTTP
    B_SYNC -->|"② HTTP API"| M_HTTP

    M_HTTP -->|"③ Read"| M_FILES
    M_FILES -->|"④ Write"| M_HTTP
    A_SYNC -->|"⑤ Write"| A_FILES
    B_SYNC -->|"⑤ Write"| B_FILES
```

![LAN Architecture](images/lan-mode-2.png)

### Discovery and Election Flow

```mermaid
sequenceDiagram
    participant A as Client A<br/>(CID: abc123)
    participant B as Client B<br/>(CID: xyz789)
    participant C as Client C<br/>(CID: def456)
    
    Note over A,C: Phase 1: Discovery
    A->>A: Register mDNS service
    B->>B: Register mDNS service
    C->>C: Register mDNS service
    
    A-->>B: mDNS announcement
    A-->>C: mDNS announcement
    B-->>A: mDNS announcement
    B-->>C: mDNS announcement
    C-->>A: mDNS announcement
    C-->>B: mDNS announcement
    
    Note over A,C: Phase 2: Election
    A->>A: Compare CIDs
    B->>B: Compare CIDs
    C->>C: Compare CIDs
    
    Note over A: abc123 < def456 < xyz789<br/>I am MASTER!
    Note over B: abc123 < def456 < xyz789<br/>A is MASTER
    Note over C: abc123 < def456 < xyz789<br/>A is MASTER
    
    Note over A,C: Phase 3: Sync
    A->>A: Start HTTP Server
    B->>A: Sync files via HTTP
    C->>A: Sync files via HTTP
```

![Discovery Flow](images/lan-mode-3.png)

### Synchronization Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant M as Master
    
    Note over C,M: List Remote Files
    C->>M: GET /api/v1/buckets/{bucket}/files
    M->>C: [{filepath: "doc.txt", mtime: ...}, ...]
    
    Note over C,M: Compare with Local
    C->>C: Determine sync actions
    
    Note over C,M: Download Newer Files
    C->>M: GET /api/v1/buckets/{bucket}/files/download?filename=doc.txt
    M->>C: [file content]
    
    Note over C,M: Upload Newer Files
    C->>M: POST /api/v1/buckets/{bucket}/files
    Note right of C: multipart/form-data<br/>with file content
    M->>C: 201 Created
```

![Sync Flow](images/lan-mode-4.png)

## Quick Start

### Basic Usage (No Authentication)

```bash
# On all devices - they will discover each other automatically
./dist/hsync -b photos -p ~/Pictures --lan-mode
```

### With Authentication

```bash
# On all devices - use the same key via environment variable
export HSYNC_LAN_KEY="mySecretKey123"
./dist/hsync -b photos -p ~/Pictures --lan-mode

# Or inline
HSYNC_LAN_KEY="mySecretKey123" ./dist/hsync -b photos -p ~/Pictures --lan-mode
```

### With Specific Port

```bash
# Use a specific port instead of random
./dist/hsync -b photos -p ~/Pictures --lan-mode --lan-port 9000
```

## CLI Options

| Option | Description | Default |
|--------|-------------|---------|
| `--lan-mode` | Enable LAN P2P mode | `false` |
| `--lan-port` | Port for HTTP server (0 = random) | `0` |
| `--lan-group` | Group name for peer filtering | `None` |
| `--no-lan-https` | Disable HTTPS (use plain HTTP) | HTTPS enabled by default |
| `--discovery-timeout` | Peer discovery timeout (seconds) | `10` |

> **Note**: For security, the LAN authentication key is only available via the `HSYNC_LAN_KEY` environment variable (not as a CLI option).

## Environment Variables

All LAN options can also be configured via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `HSYNC_LAN_PORT` | Port for HTTP server (0 = random) | `0` |
| `HSYNC_DISCOVERY_TIMEOUT` | Peer discovery timeout (seconds) | `10` |
| `HSYNC_HEARTBEAT_INTERVAL` | Heartbeat interval for peer health (seconds) | `5` |
| `HSYNC_PEER_TIMEOUT` | Timeout before marking peer as offline (seconds) | `15` |
| `HSYNC_LAN_KEY` | Shared secret for authentication | `None` |
| `HSYNC_LAN_GROUP` | Group name for peer filtering | `None` |
| `HSYNC_LAN_HTTPS` | Enable/disable HTTPS (`true`/`false`) | `true` |

### Configuration Priority

```
CLI argument > Environment variable > Default value
```

## Authentication

### No Authentication (Trust LAN)

By default, LAN mode operates without authentication. This is suitable for trusted home networks.

```bash
./dist/hsync -b photos -p ~/Pictures --lan-mode
```

### Shared Secret (Recommended)

For security, use a shared secret key that all peers must know:

```bash
# All peers must use the same key (via environment variable)
export HSYNC_LAN_KEY="mySecretKey123"
./dist/hsync -b photos -p ~/Pictures --lan-mode
```

The key is transmitted as a Bearer token in HTTP headers:

```
Authorization: Bearer mySecretKey123
```

## Group Filtering

Group filtering allows multiple hsync instances on the same LAN to be isolated. Peers with different groups will ignore each other.

### Filtering Logic (Option B - Backward Compatible)

| Local Group | Peer Group | Result |
|-------------|------------|--------|
| `None` | `None` | ✅ Compatible |
| `None` | `team-a` | ✅ Compatible |
| `team-a` | `None` | ✅ Compatible |
| `team-a` | `team-a` | ✅ Compatible |
| `team-a` | `team-b` | ❌ Ignored |

**Rule**: If **both** peers have a group defined, they must match. If **at least one** has no group, they are compatible (no filtering).

### Usage Examples

```bash
# Team A - only sees other Team A peers
./dist/hsync -b project -p ~/Projects --lan-mode --lan-group "team-a"

# Team B - only sees other Team B peers
./dist/hsync -b project -p ~/Projects --lan-mode --lan-group "team-b"

# No group - sees ALL peers (including team-a and team-b)
./dist/hsync -b project -p ~/Projects --lan-mode
```

### Use Cases

- **Separate teams**: Different teams on the same network sync independently
- **Dev vs Prod**: Isolate development and production environments
- **Multiple projects**: Same bucket name but different groups for different projects

## HTTPS Support

LAN mode supports encrypted communication using HTTPS with self-signed certificates.

### How It Works

1. **Certificate Generation**: When HTTPS is enabled, a self-signed certificate is generated per session
2. **Fingerprint Sharing**: The certificate fingerprint (SHA-256) is shared via mDNS TXT records
3. **Automatic Verification**: Clients can verify the master's certificate using the fingerprint

```
┌──────────────────────────────────────────────────────────────────┐
│                    HTTPS Certificate Flow                        │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│   Master                              Client                     │
│   ══════                              ══════                     │
│   ┌────────────────┐                                            │
│   │ Generate RSA   │                                            │
│   │ 2048-bit key   │                                            │
│   └───────┬────────┘                                            │
│           │                                                      │
│   ┌───────▼────────┐                                            │
│   │ Create X.509   │                                            │
│   │ certificate    │                                            │
│   └───────┬────────┘                                            │
│           │                                                      │
│   ┌───────▼────────┐        mDNS TXT record                     │
│   │ Calculate      │ ─────────────────────────>  ┌──────────────┐│
│   │ SHA-256        │     cert_fp=abc123...       │ Receive      ││
│   │ fingerprint    │                             │ fingerprint  ││
│   └───────┬────────┘                             └──────┬───────┘│
│           │                                             │        │
│   ┌───────▼────────┐        HTTPS request               │        │
│   │ Start HTTPS    │ <───────────────────────  ┌────────▼───────┐│
│   │ server         │                           │ Verify cert    ││
│   └────────────────┘                           │ fingerprint    ││
│                                                └────────────────┘│
└──────────────────────────────────────────────────────────────────┘
```

### Usage

HTTPS is **enabled by default** in LAN mode. No additional configuration is needed:

```bash
# HTTPS is enabled by default
./dist/hsync -b photos -p ~/Pictures --lan-mode

# Combine with authentication for maximum security
HSYNC_LAN_KEY="mySecret" ./dist/hsync -b photos -p ~/Pictures --lan-mode

# Disable HTTPS (use plain HTTP)
./dist/hsync -b photos -p ~/Pictures --lan-mode --no-lan-https

# Disable via environment variable
export HSYNC_LAN_HTTPS=false
./dist/hsync -b photos -p ~/Pictures --lan-mode
```

### Security Considerations

| Aspect | Description |
|--------|-------------|
| **Certificate Validity** | 1 day (session-based) |
| **Key Size** | RSA 2048-bit |
| **Fingerprint** | SHA-256 (64 hex characters) |
| **Trust Model** | Trust-on-first-use (TOFU) via mDNS |

### When to Use HTTPS

- **Shared networks**: Office or shared home networks where traffic sniffing is a concern
- **Sensitive data**: When syncing confidential files
- **Combined with `HSYNC_LAN_KEY`**: For both encryption and authentication

### Limitations

- **Self-signed certificates**: Browsers and strict clients may show warnings
- **No CA verification**: Trust is established via mDNS fingerprint sharing
- **Session-based**: New certificate generated on each restart

## Master Election

The master is elected using **lexicographic comparison** of Client IDs (CIDs):

```mermaid
flowchart LR
    subgraph "① CID Comparison"
        A["abc123"] -->|"②"| COMP{Compare}
        B["def456"] -->|"②"| COMP
        C["xyz789"] -->|"②"| COMP
    end

    COMP -->|"③"| RESULT["abc123 wins<br/>(lowest CID)"]

    style A fill:#e8f5e9,stroke:#2e7d32
    style RESULT fill:#e8f5e9,stroke:#2e7d32
```

![Master Election](images/lan-mode-5.png)

**Rules:**
- The peer with the **lowest CID** becomes the master
- CIDs are random alphanumeric strings generated at startup
- If the master goes offline, a new election occurs

## API Endpoints

The master exposes these HTTP endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/health` | GET | Health check |
| `/api/v1/buckets/{bucket}/files` | GET | List files with metadata |
| `/api/v1/buckets/{bucket}/files/download` | GET | Download a file |
| `/api/v1/buckets/{bucket}/files` | POST | Upload a file |

### List Files

```http
GET /api/v1/buckets/photos/files?sync_order=mtime HTTP/1.1
Authorization: Bearer mySecretKey123
```

**Response:**
```json
[
  {
    "filepath": "vacation/photo1.jpg",
    "name": "photo1.jpg",
    "size": 2048576,
    "mtime": 1706000000,
    "ctime": 1705900000,
    "md5sum": "d41d8cd98f00b204e9800998ecf8427e",
    "permissions": "-rw-r--r--"
  }
]
```

### Download File

```http
GET /api/v1/buckets/photos/files/download?filename=vacation/photo1.jpg HTTP/1.1
Authorization: Bearer mySecretKey123
```

### Upload File

```http
POST /api/v1/buckets/photos/files HTTP/1.1
Authorization: Bearer mySecretKey123
Content-Type: multipart/form-data; boundary=...

--boundary
Content-Disposition: form-data; name="cid"

client123
--boundary
Content-Disposition: form-data; name="path"

vacation
--boundary
Content-Disposition: form-data; name="file"; filename="photo2.jpg"
Content-Type: application/octet-stream

[binary content]
--boundary--
```

## Use Cases

### Home Network Sync

Sync photos between laptop and desktop without internet:

```bash
# On laptop
./dist/hsync -b photos -p ~/Pictures --lan-mode

# On desktop
./dist/hsync -b photos -p ~/Pictures --lan-mode
```

### Office File Sharing

Share project files between team members on the same network:

```bash
# All team members run:
export HSYNC_LAN_KEY="team-secret-2024"
./dist/hsync -b project -p ~/Projects/myproject --lan-mode
```

### Backup to NAS

Sync files to a NAS device on the local network:

```bash
# On workstation
HSYNC_LAN_KEY="backup-key" ./dist/hsync -b backup -p ~/Documents --lan-mode

# On NAS
HSYNC_LAN_KEY="backup-key" ./dist/hsync -b backup -p /volume1/backups --lan-mode
```

## Troubleshooting

### Peers Not Discovering Each Other

1. **Check firewall**: Ensure mDNS (UDP port 5353) is allowed
2. **Check network**: All devices must be on the same subnet
3. **Increase timeout**: Use `--discovery-timeout 15` for slower networks

### Authentication Failures

1. **Check key**: Ensure all peers use the exact same `HSYNC_LAN_KEY` environment variable
2. **Check logs**: Look for "401 Unauthorized" errors

### Port Conflicts

1. **Use random port**: Default `--lan-port 0` avoids conflicts
2. **Specify port**: Use `--lan-port 9000` if you need a fixed port

## Comparison: Server Mode vs LAN Mode

| Feature | Server Mode | LAN Mode |
|---------|-------------|----------|
| **Requires Server** | ✅ Yes | ❌ No |
| **Internet Required** | ✅ Yes | ❌ No |
| **Multi-Network Sync** | ✅ Yes | ❌ No (same LAN only) |
| **Automatic Discovery** | ❌ No | ✅ Yes (mDNS) |
| **Central Storage** | ✅ Yes | ❌ No (distributed) |
| **Authentication** | API Key | Shared Secret |
| **Watch Mode** | ✅ Yes | ❌ Not yet |

## Technical Details

### mDNS Service

LAN mode uses mDNS (Multicast DNS) for peer discovery:

- **Service Type**: `_hsync._tcp.local.`
- **TXT Records**: `cid`, `bucket`, `version`

### Dependencies

- **zeroconf**: Python library for mDNS/DNS-SD

### Thread Model

```mermaid
flowchart TB
    subgraph "① Main Thread"
        MAIN[Main Loop]
    end

    subgraph "② Background Threads"
        MDNS[mDNS Listener]
        HTTP[HTTP Server]
        HB[Heartbeat Timer]
    end

    MAIN -->|"① Start"| MDNS
    MAIN -->|"② Start"| HTTP
    MAIN -->|"③ Start"| HB

    MDNS -.->|"④ Peer Events"| MAIN
    HTTP -.->|"⑤ Requests"| MAIN
    HB -.->|"⑥ Peer Timeout"| MAIN
```

![Thread Model](images/lan-mode-6.png)

