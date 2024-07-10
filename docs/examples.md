# HSYNC Client Examples

Real-world usage examples for the `hsync` binary, grouped by deployment
model. For an overview of the CLI options and log labels see
[Usage](usage.md); for a deeper dive into LAN mode see [LAN Mode](lan-mode.md).

## Conventions

All examples assume the binary has been built (`./compile.sh`) and the
`hsync` executable is on your `$PATH`. Replace `<platform>` with your
architecture if you run it from `dist/`, e.g. `dist/linux-amd64/hsync`.

Server-based examples require an `HSYNC_API_KEY` matching the one
configured on the server. The simplest way to set it is via a `.env`
file (see [`dev.env`](../dev.env) / [`prod.env`](../prod.env)).

```bash
export HSYNC_SERVER="https://hsync.example.com:8765"
export HSYNC_API_KEY="your-api-key"
```

LAN examples discover peers automatically via mDNS and require no server.

---

## Part 1 — Server-Based Examples

These examples use a central HSYNC server as a hub. All clients connect
to the same `bucket` to exchange files.

### 1.1 Sync a personal folder (one-time)

Run sync once and exit — useful for manual snapshots or scheduled jobs
(cron, systemd timer, Task Scheduler).

```bash
hsync -s "$HSYNC_SERVER" -b documents -p ~/Documents
```

### 1.2 Continuous workstation sync (watch mode)

Keep a working folder synchronised in real-time across all devices that
share the bucket. Local edits are pushed immediately; remote changes
arrive through the WebSocket.

```bash
hsync -s "$HSYNC_SERVER" -b workspace -p ~/Workspace -w
```

### 1.3 Push-only backup to a remote server

Push local files to the server and never download anything back — the
typical setup for a backup source (laptop → cloud bucket).

```bash
hsync -s "$HSYNC_SERVER" -b laptop-backup -p ~ \
      -w --sync-direction upload-only
```

Combine with `--ignore-delete` (or `HSYNC_IGNORE_DELETE=true`) to keep
files on the server even after they are removed locally — true backup
semantics:

```bash
HSYNC_IGNORE_DELETE=true hsync -s "$HSYNC_SERVER" -b laptop-backup \
      -p ~ -w --sync-direction upload-only
```

### 1.4 Read-only mirror from the server

Pull files from a shared bucket without ever uploading local edits.
Useful for shared assets, dotfiles distribution, public download mirrors.

```bash
hsync -s "$HSYNC_SERVER" -b shared-assets -p ~/Assets \
      -w --sync-direction download-only
```

### 1.5 Periodic full re-sync alongside watch mode

Watch mode reacts to live events, but a periodic full reconciliation can
catch missed events (e.g. after sleep / network interruptions). The
example below re-syncs every 5 minutes (`-i 300`) on top of the live
WebSocket stream:

```bash
hsync -s "$HSYNC_SERVER" -b project -p ~/Project -w -i 300
```

### 1.6 Multi-device sync via central server

Run the same command on every device — bidirectional sync, last writer
wins. The server is a blind relay; each client decides what to do with
the events it receives based on its own `--sync-direction`.

```bash
# Desktop, laptop, tablet — same command, different machines
hsync -s "$HSYNC_SERVER" -b family-photos -p ~/Pictures -w
```

### 1.7 Bandwidth-limited sync over a slow connection

Cap upload to 1 MB/s and download to 500 KB/s — handy on tethered
connections or to leave bandwidth for other applications.

```bash
hsync -s "$HSYNC_SERVER" -b media -p ~/Media -w \
      --max-bandwidth-upload 1MB \
      --max-bandwidth-download 500KB
```

### 1.8 Long-running unattended service

Add a longer stability delay (avoid uploading files still being
written), a longer retry delay (avoid hammering a flaky network) and
write logs to a rotating file (rotation governed by
`HSYNC_LOG_ROTATION` / `HSYNC_LOG_RETENTION`).

```bash
hsync -s "$HSYNC_SERVER" -b media -p /srv/media -w \
      --stability-delay 30 --retry-delay 30 \
      -o /var/log/hsync.log --yes
```

---

## Part 2 — LAN P2P Examples

These examples work without any central server: peers discover each
other via mDNS/Bonjour on the same local network. The client elected
as **master** serves the bucket; the others are slaves and connect to
it. See [LAN Mode](lan-mode.md) for the full election rules.

> **Tip**: in LAN mode `--watch` is implicit — every connected client
> keeps a WebSocket open with the master. Pass `--no-lan-persistent`
> to perform a one-shot sync and exit when no peer is found.

### 2.1 Sync a folder between two devices on the same Wi-Fi

The simplest LAN use case: two laptops at home, no server, no internet
required. Run the same command on both — one is elected master, the
other becomes a slave automatically.

```bash
# On every device that should share the folder
hsync -b shared -p ~/Shared --lan-mode
```

### 2.2 Sync your photo library across devices without internet

Useful at home, in a hotel room, on a plane. Use a shared key so only
your devices participate.

```bash
export HSYNC_LAN_KEY="my-secret-key"
hsync -b photos -p ~/Pictures --lan-mode
```

### 2.3 Backup laptop to a local NAS / always-on PC

Pin the always-on machine as master with `--force-master`, then run
upload-only on the laptop. The NAS keeps the canonical copy; the
laptop never downloads anything back.

```bash
# On the NAS / always-on PC (canonical store, must be master)
hsync -b laptop-backup -p /srv/backups/laptop --lan-mode --force-master

# On the laptop (push-only client)
hsync -b laptop-backup -p ~ --lan-mode --sync-direction upload-only
```

> **Note**: `--force-master` cannot be combined with
> `--sync-direction upload-only`/`download-only` on the same peer — the
> master must remain bidirectional. Apply the direction on the slaves.

### 2.4 One-shot LAN sync (no continuous listening)

Sync once when a peer is found and then exit. Handy from a script, a
`udev` rule on USB-tethered devices, or a manual "pull the latest"
command.

```bash
hsync -b photos -p ~/Pictures --lan-mode --no-lan-persistent
```

Equivalent via environment variable:

```bash
HSYNC_LAN_PERSISTENT=false hsync -b photos -p ~/Pictures --lan-mode
```

### 2.5 Read-only LAN slave (download-only)

Subscribe to changes from the master without ever pushing local edits
back — e.g. a digital signage display that mirrors a content folder.

```bash
hsync -b signage -p /opt/signage --lan-mode --sync-direction download-only
```

### 2.6 Multiple isolated groups on the same LAN

Use `--lan-group` (or `HSYNC_LAN_GROUP`) to partition peers on the same
network — only peers with the same group name see each other.

```bash
# Group A — design team
hsync -b assets -p ~/Assets --lan-mode --lan-group design

# Group B — developers, completely isolated from group A
hsync -b code -p ~/Code --lan-mode --lan-group dev
```

### 2.7 LAN with a fixed port (firewalled environments)

By default the embedded HTTP server binds to a random port. Pin it when
you need to open a specific port in a firewall.

```bash
hsync -b shared -p ~/Shared --lan-mode --lan-port 9000
```

### 2.8 LAN without HTTPS (trusted network only)

LAN mode uses self-signed HTTPS by default with a fingerprint exchanged
via mDNS. Disable HTTPS only on trusted networks for slightly lower
overhead:

```bash
hsync -b shared -p ~/Shared --lan-mode --no-lan-https
```

---

## Part 3 — Common Operational Patterns

The flags below apply to both deployment models.

### 3.1 Preview without writing (dry run)

See what would be transferred or deleted, change nothing:

```bash
hsync -s "$HSYNC_SERVER" -b mybucket -p ~/Data -n
```

Example output:

```
[↓] Would download: documents/report.pdf
[↑] Would upload: images/photo.jpg
[✕] Would delete: old/deprecated.txt
```

### 3.2 Verbose troubleshooting

```bash
hsync -s "$HSYNC_SERVER" -b mybucket -p ~/Data -v
```

### 3.3 Capture logs to a file (with rotation)

```bash
hsync -s "$HSYNC_SERVER" -b mybucket -p ~/Data -w -o /var/log/hsync.log
```

Rotation, retention and compression of the log file are controlled by
`HSYNC_LOG_ROTATION`, `HSYNC_LOG_RETENTION` and `HSYNC_LOG_COMPRESSION`
— see [`dev.env`](../dev.env).

### 3.4 Tune stability for slow storage or large files

The stability delay tells the client to wait until a file has stopped
changing for *N* seconds before uploading it. Increase it for large
files written over slow storage or by another tool.

```bash
hsync -s "$HSYNC_SERVER" -b mybucket -p ~/Data -w --stability-delay 30
```

### 3.5 Custom retry delay (flaky network)

```bash
hsync -s "$HSYNC_SERVER" -b mybucket -p ~/Data -w --retry-delay 30
```

### 3.6 Non-interactive / scripted execution

`--yes` (or `-y`) auto-confirms the deletion-risk warning so the client
runs without prompting — required for cron jobs, systemd services, CI:

```bash
hsync -s "$HSYNC_SERVER" -b mybucket -p ~/Data -w -y
```

### 3.7 Skip SSL verification (development only)

When the server uses a self-signed certificate not in the system trust
store:

```bash
hsync -s https://dev-server:8765 -b mybucket -p ~/Data --insecure
```

### 3.8 Compose multiple flags

A realistic always-on backup target combining several options:

```bash
hsync -s "$HSYNC_SERVER" -b backups -p /srv/backups -w \
      --sync-direction upload-only \
      --stability-delay 60 \
      --retry-delay 30 \
      --max-bandwidth-upload 2MB \
      -o /var/log/hsync-backup.log \
      --yes
```

---

## See Also

- [Usage](usage.md) — CLI options reference, log labels, error handling
- [Configuration](configuration.md) — environment variables and `.env` files
- [Sync Modes](sync-modes.md) — single / watch / LAN mode comparison
- [LAN Mode](lan-mode.md) — P2P architecture, master election, security
