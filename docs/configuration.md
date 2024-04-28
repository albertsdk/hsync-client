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

| Option | Description | Default |
|--------|-------------|---------|
| `-s, --server` | Server URL | (required or `HSYNC_SERVER`) |
| `--timeout` | HTTP request timeout in seconds | `30` |
| `--ca-cert` | Path to CA certificate for self-signed server certs | — |
| `--insecure` | Skip SSL certificate verification | `false` |

### Sync

| Option | Description | Default |
|--------|-------------|---------|
| `-b, --bucket` | Bucket name | (required) |
| `-p, --path` | Local directory path | cwd |
| `-w, --watch` | Watch for changes and sync continuously. (Implicit in LAN mode) | `false` |
| `-i, --interval` | Force full sync every N seconds (requires `-w`) | — |
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
| `--min-free-disk-space` | Minimum free disk space in MB before sync. `0` = disabled | `100` |

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

| Option | Description |
|--------|-------------|
| `--lan-mode` | Enable LAN P2P mode (no server required) |
| `--lan-port` | Port for LAN HTTP server (0 = random) |
| `HSYNC_LAN_KEY` | Shared secret for LAN authentication (env var only) |
| `--lan-group` | Group name for peer filtering |
| `--no-lan-https` | Disable HTTPS (enabled by default) |
| `--discovery-timeout` | Peer discovery timeout in seconds |

## SSL/TLS (HTTPS/WSS)

The client supports encrypted communication via HTTPS and WSS (WebSocket Secure).

### Connecting to a Server with Valid SSL Certificate

```bash
# Standard HTTPS connection (certificate verified against system CA store)
./dist/hsync -s https://server.example.com:8765 -b mybucket
```

### Connecting to a Server with Self-Signed Certificate

When the server uses a self-signed certificate, you have two options:

**Option 1: Trust the server certificate (recommended)**

```bash
# Copy the server's certificate to the client machine
scp server:/path/to/certs/server.crt ./server.crt

# Connect using the CA certificate
./dist/hsync -s https://server:8765 -b mybucket --ca-cert server.crt
```

**Option 2: Disable SSL verification (not recommended for production)**

```bash
# Skip certificate verification (insecure!)
./dist/hsync -s https://server:8765 -b mybucket --insecure
```

> ⚠️ **Warning**: Using `--insecure` disables all SSL certificate verification, making the connection vulnerable to man-in-the-middle attacks. Only use this for testing.

### SSL Examples

**Watch mode with self-signed certificate:**
```bash
./dist/hsync -s https://server:8765 -b mybucket -w --ca-cert server.crt
```

**Dry-run with HTTPS:**
```bash
./dist/hsync -s https://server:8765 -b mybucket -n --ca-cert server.crt
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
./dist/hsync -s http://server:8765 -b mybucket -w --max-bandwidth-upload 1MB

# Limit download to 500 KB/s
./dist/hsync -s http://server:8765 -b mybucket -w --max-bandwidth-download 500KB

# Limit both directions
./dist/hsync -s http://server:8765 -b mybucket -w --max-bandwidth-upload 2MB --max-bandwidth-download 1MB
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
