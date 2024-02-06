# Configuration

This guide explains how to configure HSYNC for your environment.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HSYNC_SERVER` | Server URL | (required or use `-s`) |
| `HSYNC_API_KEY` | API key for authentication | (required) |
| `HSYNC_MAX_CONCURRENT_TRANSFERS` | Max parallel file transfers | `5` |
| `HSYNC_IGNORE_DELETE` | Do not propagate file deletions (backup mode) | `false` |

## Environment Files

The client provides environment file templates:

```bash
# Development (HTTP, localhost)
source dev.env

# Production (HTTPS, remote server)
source prod.env
```

## CLI Options

| Option | Description |
|--------|-------------|
| `-s, --server` | Server URL |
| `-b, --bucket` | Bucket name (required) |
| `-p, --path` | Local directory path (default: cwd) |
| `-w, --watch` | Watch for changes and sync continuously |
| `-i, --interval` | Force full sync every N seconds (requires `-w`) |
| `--retry-delay` | Retry connection after N seconds (default: 5) |
| `-n, --dry-run` | Show what would be done without making changes |
| `--ignore-delete` | Do not propagate file deletions |
| `--sync-direction` | Sync direction: `bidirectional`, `upload-only`, `download-only` |
| `-v, --verbose` | Enable verbose output |
| `-q, --quiet` | Suppress all output |
| `-o, --output` | Write output to file |
| `--ca-cert` | Path to CA certificate for self-signed server certs |
| `--insecure` | Skip SSL certificate verification |
| `--no-color` | Disable colored output |
| `-y, --yes` | Skip interactive confirmation prompts |
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

## SSL/TLS Options

### Connecting with Valid Certificate

```bash
./dist/hsync -s https://server.example.com:8765 -b mybucket
```

### Connecting with Self-Signed Certificate

```bash
# Using CA certificate
./dist/hsync -s https://server:8765 -b mybucket --ca-cert server.crt

# Skip verification (not recommended)
./dist/hsync -s https://server:8765 -b mybucket --insecure
```

## Compression Options

| Option | Description |
|--------|-------------|
| `--no-compression` | Disable compression for this session |
| `HSYNC_COMPRESSION_ENABLED` | Environment variable to disable compression |

Compression is enabled by default. See [Usage](usage.md) for details.
