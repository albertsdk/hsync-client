# Examples — Server Mode

> Back to [Examples](examples.md)

Real-world usage examples for the `hsync` binary in **server-based** deployments.

## Conventions

All examples assume the binary has been built (`./compile.sh`) and the
`hsync` executable is on your `$PATH`. Server-based examples require an
`HSYNC_API_KEY` holding an account token (`hs_<id>_<secret>`) issued by the
server.

### Configuration via environment

The client does not load `./.env` by itself. Export the token, then pass the
server host, bucket and path on the CLI:

```bash
export HSYNC_API_KEY="hs_<id>_<secret>"
hsync server server.example.com -b documents -p "$HOME/Documents"
```

The port defaults to 9753 (the hsync-server default); a reverse proxy on the
standard HTTPS port is selected explicitly:

```bash
hsync server server.example.com --port 443 -b documents -p "$HOME/Documents"
```

`--env-file` loads recognised `HSYNC_*` keys only (`HSYNC_SERVER`,
`HSYNC_API_KEY`, …). `HSYNC_BUCKET` and `HSYNC_PATH` are not settings keys.

> **Buckets must exist before the first sync.** Create them on the server
> (once) with `hsync-server admin bucket create --bucket <name>` — see the
> server [Getting Started](https://github.com/albertobasso/hsync-server/blob/main/docs/getting-started.md)
> guide. The examples below assume the referenced bucket already exists.

## 1.1 Sync a personal folder (one-time)

```bash
hsync server server.example.com -b personal -p ~/Documents
```

## 1.2 Continuous workstation sync

```bash
hsync server server.example.com -b work -p ~/Work
```

## 1.3 Push-only backup to a remote server

```bash
hsync server server.example.com -b backup -p ~/Backup --sync-direction upload-only
```

## 1.4 Read-only mirror from the server

```bash
hsync server server.example.com -b shared -p ~/Shared --sync-direction download-only
```

## 1.5 Periodic full re-sync alongside continuous mode

```bash
hsync server server.example.com -b docs -p ~/Docs --interval 3600
```

## 1.6 Multi-device sync via central server

On each device, configure the same bucket with its own local path:

```bash
# Laptop
hsync server server.example.com -b team -p ~/Team

# Desktop
hsync server server.example.com -b team -p ~/Team
```

Changes made on one device propagate through the server to all others.

## 1.7 Bandwidth-limited sync over a slow connection

```bash
hsync server server.example.com -b docs -p ~/Docs --limit-upload 1MB
```

## 1.8 Long-running unattended service

```bash
hsync server server.example.com -b docs -p ~/Docs -y
```

The `-y` flag skips confirmation prompts. Combine with a systemd service for
production deployments.

## See also

- [Server Mode Guide](server-mode.md) — detailed walkthrough
- [Configuration — Server Mode](configuration-server.md) — all server-mode options
