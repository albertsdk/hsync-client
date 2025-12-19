# Examples — Server Mode

> Back to [Examples](examples.md)

Real-world usage examples for the `hsync` binary in **server-based** deployments.

## Conventions

All examples assume the binary has been built (`./compile.sh`) and the
`hsync` executable is on your `$PATH`. Server-based examples require an
`HSYNC_API_KEY` holding an account token (`hs_<id>_<secret>`) issued by the
server.

### Configuration via `.env` file

```bash
# .env
HSYNC_SERVER="https://hsync.example.com:9753"
HSYNC_API_KEY="your-api-key"
HSYNC_BUCKET="documents"
HSYNC_PATH="$HOME/Documents"
HSYNC_WATCH=true
HSYNC_SYNC_DIRECTION="bidirectional"
```

Then simply run `hsync` — the client reads `.env` automatically.

## 1.1 Sync a personal folder (one-time)

```bash
hsync -s "$HSYNC_SERVER" -b personal -p ~/Documents
```

## 1.2 Continuous workstation sync (watch mode)

```bash
hsync -s "$HSYNC_SERVER" -b work -p ~/Work -w
```

## 1.3 Push-only backup to a remote server

```bash
hsync -s "$HSYNC_SERVER" -b backup -p ~/Backup --sync-direction upload-only -w
```

## 1.4 Read-only mirror from the server

```bash
hsync -s "$HSYNC_SERVER" -b shared -p ~/Shared --sync-direction download-only -w
```

## 1.5 Periodic full re-sync alongside watch mode

```bash
hsync -s "$HSYNC_SERVER" -b docs -p ~/Docs -w --interval 3600
```

## 1.6 Multi-device sync via central server

On each device, configure the same bucket with its own local path:

```bash
# Laptop
hsync -s "$HSYNC_SERVER" -b team -p ~/Team -w

# Desktop
hsync -s "$HSYNC_SERVER" -b team -p ~/Team -w
```

Changes made on one device propagate through the server to all others.

## 1.7 Bandwidth-limited sync over a slow connection

```bash
HSYNC_UPLOAD_LIMIT=1048576 hsync -s "$HSYNC_SERVER" -b docs -p ~/Docs -w
```

## 1.8 Long-running unattended service

```bash
hsync -s "$HSYNC_SERVER" -b docs -p ~/Docs -w -y
```

The `-y` flag skips confirmation prompts. Combine with a systemd service for
production deployments.

## See also

- [Server Mode Guide](server-mode.md) — detailed walkthrough
- [Configuration — Server Mode](configuration-server.md) — all server-mode options
