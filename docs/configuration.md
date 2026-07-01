# Configuration

This guide explains how to configure HSYNC for your environment.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HSYNC_SERVER` | Server URL | (required or use `-s`) |
| `HSYNC_API_KEY` | API key for authentication | (required) |
| `HSYNC_MAX_CONCURRENT_TRANSFERS` | Max parallel file transfers | `5` |
| `HSYNC_IGNORE_DELETE` | Do not propagate file deletions (backup mode) | `false` |
| `HSYNC_MAX_FILE_WORKERS` | Max parallel workers for local file scanning | auto (CPU-based) |
| `HSYNC_STABILITY_DELAY` | Seconds to wait before uploading modified files | `5` |
| `HSYNC_RESYNC` | Periodic full-resync interval (seconds) in continuous mode; `0` disables it | `300` |
| `HSYNC_SYNC_HIDDEN` | Include hidden/system files in sync (disables built-in filters) | `false` |
| `HSYNC_IGNORE_PATTERNS` | Comma-separated extra gitignore-style patterns | `""` |

> `HSYNC_SERVER` and `HSYNC_API_KEY` are required in **Server Mode** only. In
> **LAN Mode** (`--lan-mode`) they are ignored; use the optional
> `HSYNC_LAN_KEY` instead (see [LAN Mode Options](#lan-mode-options)).

### Periodic resync and the inotify watch limit (Linux)

In continuous mode (`-w`) the client runs a periodic full sync every
`HSYNC_RESYNC` seconds (default `300`, set `0` to disable). This is a safety
net: it recovers any file-system events the watcher may have missed.

On Linux the recursive watcher uses one inotify watch per sub-directory. Large
trees can exceed `fs.inotify.max_user_watches`, after which the kernel drops
events silently. The client warns at startup when the watched tree approaches
this limit; raise it (then re-run) with, e.g.:

```bash
sudo sysctl fs.inotify.max_user_watches=$(( $(cat /proc/sys/fs/inotify/max_user_watches) * 4 ))
```

## Environment Files

The client provides environment file templates:

```bash
# Development (HTTP, localhost)
source dev.env

# Production (HTTPS, remote server)
source prod.env
```

## CLI Options

### Connection

These options apply to **Server Mode**. In **LAN Mode** (`--lan-mode`) no
server URL is required — peers are discovered automatically via mDNS, so
`-s/--server` and `HSYNC_SERVER` are ignored. See
[LAN Mode Options](#lan-mode-options) below.

| Option | Description | Default |
|--------|-------------|---------|
| `-s, --server` | Server URL (Server Mode only) | (required or `HSYNC_SERVER`) |
| `--timeout` | HTTP request timeout in seconds | `30` |
| `--ca-cert` | Path to CA certificate for self-signed server certs | — |
| `--insecure` | Skip SSL certificate verification | `false` |

### Sync

| Option | Description | Default |
|--------|-------------|---------|
| `-b, --bucket` | Bucket name | (required) |
| `-p, --path` | Local directory path | cwd |
| `-w, --watch` | Watch for changes and sync continuously. (Implicit in LAN mode) | `false` |
| `-i, --interval` | Force full sync every N seconds (requires `-w`); overrides `HSYNC_RESYNC` | `300` |
| `--retry-delay` | Retry connection after N seconds | `5` |
| `--stability-delay` | Seconds to wait before uploading modified files | `5` |
| `--max-wait-timeout` | Maximum time to wait for file stabilization | `120` |
| `--filter-path` | Sync only files within this subdirectory path | — |
| `-n, --dry-run` | Show what would be done without making changes | `false` |
| `--ignore-delete` | Do not propagate file deletions | `false` |
| `--sync-direction` | Sync direction: `bidirectional`, `upload-only`, `download-only` | `bidirectional` |
| `--max-bandwidth-upload` | Maximum upload bandwidth (e.g., `1MB`, `500KB`). `0` = unlimited | — |
| `--max-bandwidth-download` | Maximum download bandwidth (e.g., `1MB`, `500KB`). `0` = unlimited | — |
| `--no-compression` | Disable zstd compression for file transfers | `false` |
| `--min-free-disk-space` | Minimum free disk space in MB before sync. `0` = disabled | `500` |
| `--sync-hidden` | Include hidden/system files in sync (disables built-in filters) | `false` |
| `--ignore-patterns` | Comma-separated extra gitignore-style patterns (layered on `.hsyncignore`) | — |

### Output

| Option | Description |
|--------|-------------|
| `-v, --verbose` | Enable verbose output |
| `-q, --quiet` | Suppress all output |
| `-o, --output` | Write output to file |
| `--no-color` | Disable colored output |
| `--help-symbols` | Show output symbols legend |

### General

| Option | Description |
|--------|-------------|
| `-y, --yes` | Skip interactive confirmation prompts (still shows warnings) |
| `--env-file` | Load environment variables from a file |
| `-V, --version` | Show version |

## LAN Mode Options

This is the single reference for every LAN (`--lan-mode`) option and `HSYNC_*`
environment variable. For the end-user guide see [LAN Mode](lan-mode.md); for the
protocol internals see [LAN Internals](lan-internals.md).

### CLI Options

| Option | Description | Default |
|--------|-------------|---------|
| `--lan-mode` | Enable LAN P2P mode (no server required) | `false` |
| `--lan-port PORT` | Port used when acting as LAN master (`0` = random) | `0` |
| `--discovery-timeout SEC` | Initial peer discovery timeout (`0` = wait indefinitely) | `10` |
| `--lan-group GROUP` | Group name for peer filtering | none |
| `--no-lan-persistent` | Exit after a single sync attempt instead of waiting for peers | persistent on |
| `--force-master` | Always be the LAN master | `false` |
| `--never-master` | Never become master; stay a client/slave | `false` |
| `--require-pairing` | Strict pairing: refuse a first-seen master certificate until confirmed out of band (disables TOFU auto-pinning) | `false` |

> `--sync-direction` and `-y, --yes` also affect LAN mode; they are listed under
> [Sync](#sync) and [General](#general) above.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HSYNC_LAN_KEY` | Shared secret for LAN authentication (env var only — no CLI flag) | none |
| `HSYNC_LAN_PORT` | Master HTTPS server port (`0` = random) | `0` |
| `HSYNC_LAN_GROUP` | Group name for peer filtering | none |
| `HSYNC_DISCOVERY_TIMEOUT` | Initial peer discovery timeout (`0` = wait indefinitely) | `10` |
| `HSYNC_HEARTBEAT_INTERVAL` | Heartbeat interval for peer health (seconds) | `5` |
| `HSYNC_PEER_TIMEOUT` | Time before a silent peer is marked offline (seconds) | `15` |
| `HSYNC_LAN_PERSISTENT` | Stay active indefinitely waiting for peers | `true` |
| `HSYNC_NEVER_MASTER` | Never become master (`true`/`false`) | `false` |
| `HSYNC_LAN_REQUIRE_PAIRING` | Strict pairing — disable TOFU auto-pinning (`true`/`false`) | `false` |
| `HSYNC_LAN_CERT_DIR` | Directory holding the persistent LAN master keypair | `$XDG_STATE_HOME/hsync/lan_cert` or `~/.local/state/hsync/lan_cert` |
| `HSYNC_LAN_PIN_FILE` | File storing the pinned fingerprints of known LAN peers (TOFU) | `$XDG_STATE_HOME/hsync/lan_known_peers.json` or `~/.local/state/hsync/lan_known_peers.json` |

### Incompatibilities & Priority

- `--force-master` cannot be combined with `--sync-direction upload-only` or
  `download-only` — the master must stay bidirectional; the client refuses to start.
- `--force-master` and `--never-master` are mutually exclusive; the client
  refuses to start if both are set.
- If **every** discovered peer is `--never-master`, no master is elected and a
  parsable `[NO_MASTER]` warning is logged after a 60-second grace period.

```
CLI argument > Environment variable > Default value
```

## SSL/TLS (HTTPS/WSS)

The client supports encrypted communication via HTTPS and WSS (WebSocket Secure).

### API Key & Encryption Policy

In **Server Mode** an API key (`HSYNC_API_KEY`) is **required**: the server
rejects every authenticated request that lacks a valid `Authorization: Bearer`
token.

- **Missing key** — the client aborts immediately with a clear error
  (`No API key configured`) instead of failing later mid-sync with
  `Invalid API key`.
- **HTTPS required** — the key is only ever transmitted over an encrypted
  channel. The check keys on the **URL scheme** (`https://`), so a reverse
  proxy that terminates TLS in front of a plain-HTTP backend is fully
  supported: simply point the client at the proxy's `https://` URL.
- **LAN Mode** is exempt — it relies on the optional shared secret
  `HSYNC_LAN_KEY` over its mandatory HTTPS transport instead.

### Connecting to a Server with Valid SSL Certificate

```bash
# Standard HTTPS connection (certificate verified against system CA store)
hsync -s https://server.example.com:8765 -b mybucket
```

### Connecting to a Server with Self-Signed Certificate

When the server uses a self-signed certificate, you have two options:

**Option 1: Trust the server certificate (recommended)**

```bash
# Copy the server's certificate to the client machine
scp server:/path/to/certs/server.crt ./server.crt

# Connect using the CA certificate
hsync -s https://server:8765 -b mybucket --ca-cert server.crt
```

**Option 2: Disable SSL verification (not recommended for production)**

```bash
# Skip certificate verification (insecure!)
hsync -s https://server:8765 -b mybucket --insecure
```

> ⚠️ **Warning**: Using `--insecure` disables all SSL certificate verification, making the connection vulnerable to man-in-the-middle attacks. Only use this for testing.

### SSL Examples

**Watch mode with self-signed certificate:**
```bash
hsync -s https://server:8765 -b mybucket -w --ca-cert server.crt
```

**Dry-run with HTTPS:**
```bash
hsync -s https://server:8765 -b mybucket -n --ca-cert server.crt
```

## File Compression

HSYNC supports automatic file compression to optimize transfers using **Zstandard (zstd)**.

### How It Works

- Files are compressed before transfer and decompressed on receipt
- Compression is enabled by default and can be disabled via `--no-compression`
- Server informs client about compression support during WebSocket handshake
- In LAN mode, peers advertise compression capability via mDNS

### Compression Thresholds

Different thresholds are used based on network characteristics:

| Context | Threshold | Rationale |
|---------|-----------|-----------|
| **Download** (Server→Client) | 10 KB | Server CPU is acceptable, clear benefit above this size |
| **Upload** (Client→Server) | 32 KB | Client CPU is more precious, typical production threshold |
| **LAN mode** | 100 KB | Local network is fast (1Gbps+), only compress large files |

### Skipped Files

Compression is automatically skipped for:
- Files below the threshold (depends on context)
- Pre-compressed formats: `.jpg`, `.png`, `.mp4`, `.zip`, `.gz`, `.pdf`, `.docx`, etc.

### Configuration

| Option | Description |
|--------|-------------|
| `--no-compression` | Disable compression for this session |
| `HSYNC_COMPRESSION_ENABLED` | Environment variable to disable compression (default: `true`) |

## Bandwidth Limiting

HSYNC supports upload and download bandwidth limiting using a **token bucket** algorithm for smooth rate control.

### Usage

```bash
# Limit upload to 1 MB/s
hsync -s https://server.example.com:8765 -b mybucket -w --max-bandwidth-upload 1MB

# Limit download to 500 KB/s
hsync -s https://server.example.com:8765 -b mybucket -w --max-bandwidth-download 500KB

# Limit both directions
hsync -s https://server.example.com:8765 -b mybucket -w --max-bandwidth-upload 2MB --max-bandwidth-download 1MB
```

### Supported Formats

Bandwidth values accept human-readable suffixes:

| Format | Example | Bytes/s |
|--------|---------|---------|
| `KB` or `KB/s` | `500KB` | 512,000 |
| `MB` or `MB/s` | `1MB` | 1,048,576 |
| `GB` or `GB/s` | `1GB` | 1,073,741,824 |
| Integer | `1048576` | 1,048,576 |
| `0` | `0` | Unlimited |

### Limits

- Minimum: 1 KB/s (1,024 bytes/s)
- Maximum: 100 MB/s (104,857,600 bytes/s)

## Ignore Files (`.hsyncignore`)

HSYNC supports a per-bucket ignore file with the same syntax as `.gitignore`.
Place a `.hsyncignore` file in the **root of the local bucket directory** to
control which files and directories are excluded from synchronization.

### Default behaviour

By default (without `.hsyncignore` and without `--sync-hidden`), HSYNC
automatically excludes:

- Files whose name starts with `.` (e.g. `.DS_Store`, `.git/`)
- Temporary / editor swap files (`.swp`, `~`, `.bak`, `.tmp`, etc.)
- Lock files (`.~lock.*#`)
- Numbered temporary files (`4913`)

The sentinel file `.hsynckeep` is **always included** regardless of any rule.

### `.hsyncignore` syntax

The file follows [gitignore pattern syntax](https://git-scm.com/docs/gitignore):

| Pattern | Effect |
|---------|--------|
| `*.log` | Ignore all `.log` files anywhere in the bucket |
| `/build/` | Ignore the `build/` directory only at the root |
| `!important.log` | Re-include `important.log` even if `*.log` would ignore it |
| `**/__pycache__/` | Ignore all `__pycache__` directories recursively |

### Examples

```
# Ignore build artefacts
/dist/
/build/

# Ignore Python cache
**/__pycache__/
*.pyc

# Ignore editor files (already in defaults; explicit for clarity)
*.swp

# Re-include a specific log file
!important.log
```

### Including hidden files

Use `--sync-hidden` (or `HSYNC_SYNC_HIDDEN=true`) to disable the built-in
hidden-file patterns.  Only patterns in `.hsyncignore` and `--ignore-patterns`
will then be applied:

```bash
# Sync hidden files too, but still exclude build artefacts via .hsyncignore
hsync -s https://server.example.com:8765 -b mybucket --sync-hidden
```

### Extra patterns via CLI

Pass extra patterns without creating a `.hsyncignore` file:

```bash
hsync -s https://server.example.com:8765 -b mybucket --ignore-patterns "*.log,/build/,/dist/"
```

### Hot-reload

The client watches `.hsyncignore` for changes while it is running.  Any
modification to the file is picked up automatically — no restart required.
