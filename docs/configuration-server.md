# Configuration — Server Mode

Server-mode configuration for connecting to a central HSYNC Server.

> For shared options (sync, compression, delta, logging), see [Configuration](configuration.md).
> For LAN P2P options, see [Configuration — LAN P2P Mode](configuration-lan.md).

## Server Connection Options

| Option | Description | Default |
|---|---|---|
| `-s, --server` | Server URL (required) | `HSYNC_SERVER` env var |
| `-b, --bucket` | Bucket name | (required) |
| `--api-key` | API key for authentication | `HSYNC_API_KEY` env var |

## SSL/TLS for Server Connections

| Option | Description | Default |
|---|---|---|
| `--ca-cert` | Path to CA certificate for self-signed server certs | — |
| `--insecure` | Skip SSL certificate verification | `false` |

## Environment Variables — Server Mode

| Variable | Description | Default |
|---|---|---|
| `HSYNC_SERVER` | Server URL | (required or use `-s`) |
| `HSYNC_API_KEY` | API key for authentication | (required) |

## Configuration via `.env` file

Create a `.env` file in your working directory:

```bash
# Server Mode
HSYNC_SERVER="https://hsync.example.com:9753"
HSYNC_API_KEY="your-api-key"
HSYNC_BUCKET="documents"
HSYNC_PATH="$HOME/Documents"
HSYNC_WATCH=true
HSYNC_SYNC_DIRECTION="bidirectional"
```

Then simply run `hsync` — the client reads `.env` automatically.

## See also

- [Configuration](configuration.md) — shared options (sync, compression, delta, ignore, logging)
- [Server Mode Guide](server-mode.md) — detailed walkthrough
- [Authentication — Client-Side](authentication.md) — auth flow diagrams
