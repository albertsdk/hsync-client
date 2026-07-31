# HSYNC Server Mode (Centralized Synchronization)

Server Mode connects every device to a central **HSYNC server**, which acts as
the canonical store and relays events to all connected clients in real time.

> **Quick links**
> - Full option & environment-variable reference → [Configuration](configuration.md#connection)
> - Ready-to-use recipes → [Examples](examples.md#part-1--server-based-examples)
> - Server vs LAN comparison → [Sync Modes](sync-modes.md#operating-modes-server-vs-lan)
> - Client-side sync flow (diagrams, delta transfer, event handling) → [Sync Flow](sync-flow.md)

## Overview

In Server Mode every client connects to the same central HSYNC server over HTTPS
(REST + WebSocket). The server stores the files, keeps a canonical listing, and
broadcasts every change to all connected clients so they stay in sync
automatically.

```mermaid
flowchart TB
    subgraph "Server Mode"
        S[HSYNC Server<br/>REST + WebSocket]
        C1[Client A<br/>Desktop] <-->|"sync"| S
        C2[Client B<br/>Laptop] <-->|"sync"| S
        C3[Client C<br/>Phone] <-->|"sync"| S
    end

    style S fill:#fff3e0,stroke:#e65100
    style C1 fill:#e8f5e9,stroke:#2e7d32
    style C2 fill:#e3f2fd,stroke:#1565c0
    style C3 fill:#f3e5f5,stroke:#7b1fa2
```

## Key Features

| Feature | Description |
|---------|-------------|
| **Central Hub** | One server, many clients; the server is the source of truth |
| **Real-Time Sync** | WebSocket-based instant event propagation in watch mode (`-w`) |
| **Delta Transfer** | Rsync-style block sync for large files with small changes |
| **Compression** | Automatic zstd compression (skips already-compressed formats) |
| **Bandwidth Limiting** | Token-bucket throttling for upload and download |
| **Bidirectional Sync** | Any client can create/modify/delete files; changes propagate to all |
| **Directional Sync** | Upload-only (backup) or download-only (mirror) via `--sync-direction` |
| **Periodic Resync** | Configurable interval for full reconciliation as a safety net |
| **Mass Deletion Guard** | Prevents accidental wipe when the server exposes an empty bucket |

## How It Works

In plain terms, server mode works in four steps:

1. **Connect** — the client establishes an HTTPS connection to the server and
   authenticates with `HSYNC_API_KEY` (Bearer token).
2. **Reconcile** — the client fetches the remote file list (REST), scans the
   local directory, and uploads/downloads every difference.
3. **Watch** — if `-w` (watch mode) is set, the client opens a WebSocket and
   receives remote events (add/modify/delete/rename) in real time. A periodic
   full resync (`-i`) runs as a safety net.
4. **Stay in sync** — as long as the client is running, every local change is
   pushed to the server and every remote change is pulled locally.

```mermaid
sequenceDiagram
    participant C1 as Client A
    participant S as Server
    participant C2 as Client B

    Note over C1,C2: Both connected via WebSocket (watch mode)

    C1->>C1: ① Edit file.txt
    C1->>S: ② POST file.txt
    S->>S: ③ Store + compute SHA-256
    S-->>C2: ④ WebSocket: "added" file.txt
    C2->>S: ⑤ GET file.txt
    S-->>C2: ⑥ File content
    C2->>C2: ⑦ Write file.txt locally
```

> The full initial-sync sequence diagram, the event-handling flow chart, the
> conflict-resolution tree and the delta-transfer internals are in
> [Sync Flow](sync-flow.md).

## Quick Start

### Basic Usage

```bash
# One-shot sync
hsync -s https://server.example.com:9753 -b mybucket -p /path/to/dir

# Continuous real-time sync (watch mode)
hsync -s https://server.example.com:9753 -b mybucket -p /path/to/dir -w
```

The API key is **required** in server mode. Set it via environment variable:

```bash
export HSYNC_API_KEY="your-api-key"
```

### With Custom CA Certificate (self-signed server)

```bash
# Copy the server's certificate to the client machine, then:
hsync -s https://server:9753 -b mybucket -p /path/to/dir --ca-cert server.crt
```

> For local development only: `--insecure` skips all certificate verification.
> Do **not** use in production. See [SSL/TLS](configuration.md#ssltls-httpswss)
> for the full security guidance.

### Directional Sync

```bash
# Upload-only: push local files to server, never download (backup)
hsync -s https://server:9753 -b backup -p ~ --sync-direction upload-only

# Download-only: mirror remote content locally (shared assets)
hsync -s https://server:9753 -b shared -p ~/Assets --sync-direction download-only
```

## Watch Mode & Auto-Sync

Watch mode (`-w`) keeps a persistent WebSocket connection open. The server
pushes every remote change (add, modify, delete, rename) to all connected
clients instantly — no polling.

A **periodic full resync** runs every `-i` seconds (default 300, set `0` to
disable) as a safety net: it catches any file-system events the watcher may
have missed (e.g. the inotify watch limit on Linux, sleep/wake cycles).

```bash
# Watch mode with a 5-minute resync interval
hsync -s https://server:9753 -b mybucket -p /path/to/dir -w -i 300

# Watch mode with a longer stability delay (safe for slow storage)
hsync -s https://server:9753 -b media -p /srv/media -w --stability-delay 30
```

For scheduled background sync (cron, systemd timer, Task Scheduler) use
one-shot mode (`-y` to skip interactive prompts):

```bash
# Cron entry: sync every hour
0 * * * * HSYNC_API_KEY="key" hsync -s https://server:9753 -b mybucket -p /path -y
```

## Delta Transfer, Compression & Bandwidth

HSYNC supports **delta transfer** (rsync-style block sync), **zstd
compression**, and **bandwidth limiting** — all enabled by default and
configured via the options detailed in [Configuration](configuration.md).

### Delta transfer (big files, small changes)

For files ≥ 256 KB (server mode) with a previously synced base, only the
changed blocks travel — bandwidth savings of 90%+ are common. The client
automatically falls back to a full transfer on any failure.

```bash
# Delta is enabled by default; disable it:
hsync -s https://server:9753 -b media -p ~/Media -w --no-delta
```

> Full conditions, configuration and the fallback model are documented in
> [Configuration → Delta Transfer](configuration.md#delta-transfer).

### Compression (automatic, skips already-compressed files)

```bash
# Compression is enabled by default; disable it for fast local networks:
hsync -s https://server:9753 -b media -p ~/Media -w --no-compression
```

### Bandwidth limiting (token bucket)

```bash
# Cap upload to 1 MB/s
hsync -s https://server:9753 -b mybucket -w --max-bandwidth-upload 1MB
# Cap download to 500 KB/s
hsync -s https://server:9753 -b mybucket -w --max-bandwidth-download 500KB
```

## File Filtering & Ignore Rules

HSYNC includes a **built-in ignore list** of OS cache and editor temporary
files that is always active (`.DS_Store`, `Thumbs.db`, `*.swp`, `*.tmp`,
`*.bak` …). Dotfiles (`.env`, `.git/`, `.config/`) are **synchronised
normally** — add them to `.hsyncignore` if you need to exclude them.

Place a `.hsyncignore` file at the **root of the bucket directory** with
gitignore-style patterns:

```
# Exclude build artefacts
/dist/
/build/

# Exclude .git directory
.git/
```

Or pass patterns directly on the command line without creating a file:

```bash
hsync -s https://server:9753 -b mybucket --ignore-patterns ".git/,*.log,/dist/"
```

> Full syntax, examples and the complete default-pattern list are in
> [Configuration → Ignore Files](configuration.md#ignore-files-hsyncignore).

## Security

Server Mode communicates exclusively over **HTTPS/WSS** — there is no
plain-HTTP transport. The API key (`HSYNC_API_KEY`) is sent as a Bearer token
over the encrypted channel and is **required**.

| Security aspect | How it works |
|---|---|
| **API Key** | Set `HSYNC_API_KEY` (env var only — no CLI flag). Missing key → client aborts before connecting |
| **HTTPS/WSS** | Always on; the client refuses to send the API key over plain HTTP. Works through reverse proxies that terminate TLS |
| **Custom CA** | Trust a self-signed server certificate with `--ca-cert server.crt` |
| **Server cert fingerprint** | The desktop GUI pins the server's SHA-256 fingerprint on first connect (TOFU), like LAN mode does for peer certificates |
| **Skip verification** | `--insecure` disables all certificate checks — **development only** |

> Complete SSL/TLS documentation, including certificate pinning,
> reverse-proxy setup, and the `--ca-cert` / `--insecure` options, is in
> [Configuration → SSL/TLS](configuration.md#ssltls-httpswss).

## Mass Deletion Guard

A transient server failure or misconfiguration can make the server expose an
empty (or nearly-empty) bucket. Without protection, a single sync cycle would
delete every local file not in the server's listing — effectively a **mass
accidental wipe**.

The **mass-deletion safety guard** pauses deletions when the count exceeds
either the absolute threshold (`--mass-delete-threshold`, default 50) or the
percentage of local files (`--mass-delete-percent`, default 50%). An empty
remote bucket is **always blocked**, regardless of thresholds.

```bash
# Disable the guard (when confident the server is authoritative)
hsync -s https://server:9753 -b mybucket --no-mass-delete-guard

# Tighter guard: pause deletions at 10 files or 10% of the tree
hsync -s https://server:9753 -b mybucket --mass-delete-threshold 10 --mass-delete-percent 10
```

> Full decision flowchart and the relationship with `--ignore-delete` and
> `--sync-direction` are in [Configuration → Mass-Deletion Safety
> Guard](configuration.md#mass-deletion-safety-guard).

## Options & Environment Variables

All server-mode command-line options and `HSYNC_*` environment variables are
documented in the single reference:
[Configuration → CLI Options](configuration.md#cli-options).

Key server-mode specific options:

| Option | Env var | Description | Default |
|--------|---------|-------------|---------|
| `-s, --server` | `HSYNC_SERVER` | Server URL | (required) |
| `--ca-cert` | — | Path to CA certificate (self-signed servers) | — |
| `--insecure` | — | Skip SSL certificate verification | `false` |
| `--timeout` | `HSYNC_HTTP_TIMEOUT` | HTTP request timeout (seconds) | `30` |

> **Tip**: use a `.env` file to avoid typing credentials on every command.
> See [Environment Files](configuration.md#environment-files).

## Comparison: Server Mode vs LAN Mode

See [Sync Modes → Operating Modes](sync-modes.md#operating-modes-server-vs-lan)
for the full comparison table.

## Troubleshooting

### 401 / 403 Authentication Errors

- **Check the API key**: ensure `HSYNC_API_KEY` is set and matches the server's
  configured key exactly.
- **Check the URL scheme**: the client refuses to send the API key over
  `http://` — always use `https://`.
- **Reverse proxy**: if a reverse proxy terminates TLS, point the client at the
  proxy's `https://` URL.

### SSL / Certificate Errors

- **Self-signed certificate**: copy the server's certificate to the client
  machine and pass `--ca-cert server.crt`.
- **Custom CA**: point `--ca-cert` at your CA bundle.
- **Development only**: `--insecure` skips all verification. Do not use in
  production.

### Server Unreachable / Timeout

- **Check connectivity**: verify you can reach the server's port from the
  client machine (e.g. `curl -k https://server:9753/health`).
- **Firewall**: ensure the server port (default 9753) is open.
- **Increase timeout**: use `--timeout 60` for high-latency connections.

### Files Not Syncing

- **`.hsyncignore` placement**: the file must be at the root of the bucket
  directory, not in a subdirectory. See
  [Ignore Files](configuration.md#ignore-files-hsyncignore).
- **Dotfiles**: they sync by default. If they aren't appearing, check that
  `.hsyncignore` or `--ignore-patterns` is not excluding them. If they
  shouldn't sync, add `.git/` or `.env` to `.hsyncignore`.
- **Dry run first**: use `-n` to preview what would happen without making
  changes.
- **Verbose output**: use `-v` for detailed logs.

## Use Cases & Recipes

Practical server-mode recipes — personal sync, backup, mirror, bandwidth
limits, cron/systemd scheduling, mass-deletion guard — are in
[Examples → Server-Based Examples](examples.md#part-1--server-based-examples).

## Technical Internals

For the deep client-side flow — the initial-sync and watch-mode **sequence
diagrams**, the event-handling flow chart, the conflict-resolution tree, the
delta-transfer protocol and the retry/timeout model — see [Sync
Flow](sync-flow.md). The server-side flow (file storage, event broadcast,
tombstones) is documented in the
[server Sync Flow](https://github.com/albertobasso/hsync-server/blob/main/docs/sync-flow.md).
