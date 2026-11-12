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
export HSYNC_API_KEY="hs_ACCOUNT_ID_ACCOUNT_SECRET"
hsync server --server-url server.example.com -b documents -p "$HOME/Documents"
```

The port defaults to 9753 (the hsync-server default); a reverse proxy on the
standard HTTPS port is selected explicitly:

```bash
hsync server --server-url server.example.com:443 -b documents -p "$HOME/Documents"
```

`--env-file` loads recognised `HSYNC_*` keys only (`HSYNC_SERVER_URL`,
`HSYNC_API_KEY`, …). `HSYNC_BUCKET` and `HSYNC_PATH` are not settings keys.

> **Buckets must exist before the first sync.** Create them on the server
> (once) with `sudo hsync-server admin bucket create --bucket BUCKET` on the
> server host — see the
> server [Getting Started](https://github.com/albertsdk/hsync-server/blob/main/docs/getting-started.md)
> guide. The examples below assume the referenced bucket already exists.
>
> The client never creates a bucket implicitly: a sync starts by listing the
> remote files, and a missing bucket answers `404` before any upload could run.
> The desktop app offers to create it from **Settings → Share** (and from the
> setup wizard's connection test) when the server reports it missing.

## 1.1 Sync a personal folder (one-time)

```bash
hsync server --server-url server.example.com -b personal -p ~/Documents
```

## 1.2 Continuous workstation sync

```bash
hsync server --server-url server.example.com -b work -p ~/Work
```

## 1.3 Push-only backup to a remote server

```bash
# One-way push that never deletes on the destination
hsync server --server-url server.example.com -b backup -p ~/Backup \
  --sync-direction upload-only --deletion-policy keep
```

`--sync-direction upload-only` sends local changes without pulling remote ones;
`--deletion-policy keep` stops deletions in either direction, so a
file removed on the source stays on the server. Drop `--deletion-policy keep` for a
strict mirror that follows deletions.

## 1.4 Read-only mirror from the server

```bash
hsync server --server-url server.example.com -b shared -p ~/Shared --sync-direction download-only
```

## 1.5 Periodic full re-sync alongside continuous mode

```bash
hsync server --server-url server.example.com -b docs -p ~/Docs -i 3600
```

## 1.6 Multi-device sync via central server

On each device, configure the same bucket with its own local path:

```bash
# Laptop
hsync server --server-url server.example.com -b team -p ~/Team

# Desktop
hsync server --server-url server.example.com -b team -p ~/Team
```

Both runs need the **same bucket name** and an account that is the server
`admin` or a member of that bucket; the same account on both devices works, and
one account per device keeps revocation and key rotation per device (see the
server [Roles and Permissions → Sharing one bucket between
devices](https://github.com/albertsdk/hsync-server/blob/main/docs/roles-and-permissions.md#sharing-one-bucket-between-devices)).

Changes made on one device propagate through the server to all others, so a
shared bucket doubles as a team **drop box**: anyone with write access can drop
a file and every member receives it.

## 1.7 Bandwidth-limited sync over a slow connection

```bash
hsync server --server-url server.example.com -b docs -p ~/Docs --limit-upload 1MB
```

## 1.8 Long-running unattended service

```bash
hsync server --server-url server.example.com -b docs -p ~/Docs --allow-deletions
```

The `--allow-deletions` flag accepts the deletion-risk confirmation. Combine with
a systemd service for production deployments.

## See also

- [Server Mode Guide](server-mode.md) — detailed walkthrough
- [Configuration — Server Mode](configuration-server.md) — all server-mode options
