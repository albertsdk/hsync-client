# Configuration

Shared configuration options that apply to both Server Mode and LAN P2P Mode.

> **See also:**
> - [Configuration — Server Mode](configuration-server.md) — server connection, SSL/TLS, API key
> - [Configuration — LAN P2P Mode](configuration-lan.md) — LAN options, master election, discovery

## Sync Options

| Option | Description | Default |
|---|---|---|
| `-b, --bucket` | Bucket name | (required) |
| `-p, --path` | Local directory path | cwd |
| `-w, --watch` | Watch for changes and sync continuously (implicit in LAN mode) | `false` |
| `-i, --interval` | Force full sync every N seconds (requires `-w`) | — |
| `--sync-direction` | Sync direction: `bidirectional`, `upload-only`, `download-only` | `bidirectional` |
| `--ignore-delete` | Do not propagate file deletions (backup mode) | `false` |
| `--dry-run` | Dry run: show what would change without applying | `false` |

## Mass-Deletion Guard

| Option | Description | Default |
|---|---|---|
| `--mass-deletion-threshold` | Max files allowed to be deleted in one sync (0 = unlimited) | — |
| `--mass-deletion-window` | Time window (seconds) for counting deletions | `3600` |

## Output & Logging

| Option | Description | Default |
|---|---|---|
| `-o, --output` | Log file path (enables file logging) | — (stdout only) |
| `-q, --quiet` | Suppress non-error output | `false` |
| `-v, --verbose` | Verbose output | `false` |
| `--log-level` | Log level: `debug`, `info`, `warning`, `error` | `info` |

## General Options

| Option | Description | Default |
|---|---|---|
| `--yes` | Non-interactive mode (skip confirmations) | `false` |
| `--version` | Show version and exit | — |
| `--help` | Show help and exit | — |

## Compression

| Variable | Description | Default |
|---|---|---|
| `HSYNC_COMPRESSION_ENABLED` | Enable zstd compression for file transfers | `true` |

## Delta Transfer (rsync-style block sync)

| Variable | Description | Default |
|---|---|---|
| `HSYNC_DELTA_ENABLED` | Enable delta transfer for large files | `true` |

The minimum file size for delta transfer differs by mode (256 KB server, 1 MB LAN).
See [Configuration — Server Mode](configuration-server.md) and
[Configuration — LAN P2P Mode](configuration-lan.md) for mode-specific defaults.

## Bandwidth Limiting

| Variable | Description | Default |
|---|---|---|
| `HSYNC_UPLOAD_LIMIT` | Upload bandwidth limit (bytes/second, 0 = unlimited) | `0` |
| `HSYNC_DOWNLOAD_LIMIT` | Download bandwidth limit (bytes/second, 0 = unlimited) | `0` |

## Ignore Files & Patterns

| Variable | Description | Default |
|---|---|---|
| `HSYNC_IGNORE_PATTERNS` | Comma-separated extra gitignore-style patterns | `""` |

Create a `.hsyncignore` file in your bucket directory using standard gitignore syntax.

Ignored files stop propagating from the next sync onward, but existing copies
on other devices are **never deleted** by an ignore rule — excluding protects,
it does not remove (the same contract as rsync ``--exclude`` without
``--delete-excluded`` and Syncthing ``.stignore``). Remove the pattern to start
syncing the file again.

## Periodic Resync

| Variable | Description | Default |
|---|---|---|
| `HSYNC_RESYNC` | Periodic full-resync interval (seconds); `0` disables | `300` |

## Log Rotation

When file logging is enabled with `-o/--output`:

| Variable | Default | Description |
|---|---|---|
| `HSYNC_LOG_ROTATION` | `10 MB` | Rotate when log file reaches this size |
| `HSYNC_LOG_RETENTION` | `7 days` | Keep rotated logs for this duration |
| `HSYNC_LOG_COMPRESSION` | `xz` | Compression for rotated logs |

## Auto-Tuning

| Variable | Description | Default |
|---|---|---|
| `HSYNC_AUTOTUNE` | Enable auto-tuning of timeout, retry, and concurrency | `true` |
| `HSYNC_HTTP_TIMEOUT` | HTTP request timeout in seconds (1-300) | auto |
| `HSYNC_RECONNECT_DELAY` | WS reconnection delay in seconds (1-60) | auto (`5`) |
| `HSYNC_MAX_CONCURRENT_TRANSFERS` | Max parallel file transfers (1-20) | auto (`5`-`20`) |
| `HSYNC_TRANSFER_RETRY_MIN_WAIT` | Per-file retry min wait in seconds | auto |
| `HSYNC_TRANSFER_RETRY_MAX_WAIT` | Per-file retry max wait in seconds | auto |
| `HSYNC_MAX_FILE_WORKERS` | Max parallel workers for local file scanning | auto (CPU-based) |

Use `--no-autotune` or `HSYNC_AUTOTUNE=false` to disable auto-tuning and use
built-in static defaults.

## Clock synchronisation (NTP)

In LAN mode there is no server at startup, so the client measures its clock
offset against public SNTP servers. The offset powers the chronological
conflict tie-break (`corrected_mtime`, see
[Conflict Resolution](conflict-resolution.md)): the most recent
offset-corrected edit keeps the original path. When every probe fails the
client records offset 0 (raw local time) and synchronisation proceeds
normally.

| Variable | Description | Default |
|---|---|---|
| `HSYNC_NTP_HOSTS` | Comma-separated SNTP servers, tried in order; entries may carry a port override (`host:port`) | `pool.ntp.org,time.cloudflare.com,time.google.com` |

## State directory

Causal versions and deletion markers live in a single SQLite database
**outside** the synced folder (`syncstate.db`). The folder itself only holds
a small `.hsyncfolder` identity marker.

| Variable | Description | Default |
|---|---|---|
| `HSYNC_STATE_DIR` | Directory that holds `syncstate.db` | `$XDG_STATE_HOME/hsync` or `~/.local/state/hsync` |

## Tuning for containers and VMs

| Variable | Description | Default |
|---|---|---|
| `HSYNC_AUTOTUNE_STORAGE_TYPE` | Override detected storage type: `hdd`, `ssd`, `nvme`, `unknown` | auto |
| `HSYNC_AUTOTUNE_NETWORK_TYPE` | Override detected network: `lan`, `wan`, `unknown` | auto |

## Environment Files

The client provides environment file templates:

```bash
# Development (HTTP, localhost)
source dev.env

# Production (HTTPS, remote server)
source prod.env
```

## Periodic resync and the inotify watch limit (Linux)

In continuous mode (`-w`) the client runs a periodic full sync every
`HSYNC_RESYNC` seconds (default `300`, set `0` to disable). This is a safety
net: it recovers any file-system events the watcher may have missed.

On Linux the recursive watcher uses one inotify watch per sub-directory. Large
trees can exceed `fs.inotify.max_user_watches`, after which the kernel drops
events silently. The client warns at startup when the watched tree approaches
this limit; raise it with:

```bash
sudo sysctl fs.inotify.max_user_watches=$(( $(cat /proc/sys/fs/inotify/max_user_watches) * 4 ))
