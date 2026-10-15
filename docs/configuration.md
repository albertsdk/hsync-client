# Configuration

Shared configuration options that apply to both Server Mode and p2p Mode.

> **See also:**
> - [Configuration — Server Mode](configuration-server.md) — server connection, SSL/TLS, API key
> - [Configuration — p2p Mode](configuration-p2p.md) — p2p options, master election, discovery

## Sync Options

| Option | Description | Default |
|---|---|---|
| `-b, --bucket` | Bucket name | (required) |
| `-p, --path` | Local directory path | cwd |
| `--once` | Run a single sync cycle and exit. Without it the client runs continuously and retries an unreachable server indefinitely (the default). In p2p mode it runs one slave sync cycle and exits (the master role stays continuous) | `false` |
| `-i, --interval` | Force full sync every N seconds (continuous mode only) | — |
| `--sync-direction` | Sync direction: `bidirectional`, `upload-only`, `download-only` | `bidirectional` |
| `--ignore-delete` | Do not propagate file deletions (backup mode) | `false` |
| `--dry-run` | Dry run: show what would change without applying | `false` |

## Mass-Deletion Guard

There is no time window. The guard trips on a **single reconciliation
cycle** (absolute count or percentage of local files).

| Option | Description | Default |
|---|---|---|
| `--mass-delete-guard` / `--no-mass-delete-guard` | Enable or disable the guard | enabled |
| `--mass-delete-threshold` | Absolute deletions per cycle that trip the guard (`0` = disabled) | `50` |
| `--mass-delete-percent` | Percentage of local files whose deletion trips the guard (`0` = disabled) | `50` |

Environment equivalents: `HSYNC_MASS_DELETE_GUARD`,
`HSYNC_MASS_DELETE_THRESHOLD`, `HSYNC_MASS_DELETE_PERCENT`.

## File Stability & Safety Limits

| Option | Description | Default |
|---|---|---|
| `--stability-delay` | Seconds a file must stay unchanged before upload. Already-idle files (mtime older than this) skip the wait; after a writer close a 0.3 s re-check is used instead | `5` |
| `--max-wait-timeout` | Maximum seconds to wait for a file to stabilize | `120` |
| `--subdir` | Sync only files within this subdirectory path | — (sync everything) |
| `--min-free-disk-space` | Minimum free disk space in MB before sync pauses (`0` = disabled) | `500` |
| `--max-inbound-size` | Maximum size in MB of a single inbound file (download or received p2p upload), enforced as an absolute cap on top of the peer-declared size (`0` = disabled) | `10240` |

Environment equivalent: `HSYNC_STABILITY_DELAY`. `--subdir`,
`--max-wait-timeout`, `--min-free-disk-space` and `--max-inbound-size` are
CLI-only (no environment variables).

## Output & Logging

| Option | Description | Default |
|---|---|---|
| `-o, --log-file` | Log file path (enables file logging) | — (stdout only) |
| `-q, --quiet` | Suppress non-error output | `false` |
| `-v, --verbose` | Verbose output | `false` |
| `--no-color` | Disable colored output | `false` |
| `--help-symbols` | Show output symbols legend and exit | — |

There is no `--log-level` flag. Verbosity is `-v` / `-q` only.

In verbose mode the startup log includes a `Local configuration` JSON dump of
the effective settings plus a `PATHS` section (env file, device identity dir,
state dir, the `HSYNC_STATE_DB` override if set, instance-lock dir). Secrets
(`API_KEY`, `P2P_KEY`, `Authorization`) are masked.

## General Options

| Option | Description | Default |
|---|---|---|
| `--allow-deletions` | Accept the deletion risk: a download-only sync may delete local-only files to mirror the source | `false` |
| `--env-file` | Load recognised `HSYNC_*` keys from a file (does not load `./.env` by itself) | — |
| `--capabilities-json` | Print the binary's capabilities document as JSON and exit | — |
| `-V, --version` | Show version and exit | — |
| `--help` | Show help and exit | — |

Environment equivalent for `--allow-deletions`: `HSYNC_ALLOW_DELETIONS`.

## Compression

| Variable | Description | Default |
|---|---|---|
| `HSYNC_COMPRESSION_ENABLED` | Enable zstd compression for file transfers | `true` |

## Delta Transfer (rsync-style block sync)

| Variable | Description | Default |
|---|---|---|
| `HSYNC_DELTA_ENABLED` | Enable delta transfer for large files | `true` |

| Option | Description | Default |
|---|---|---|
| `--delta-min-file-size` | Min file size in bytes for delta transfer in Server Mode | `262144` (256 KB) |
| `--delta-min-file-size-p2p` | Min file size in bytes for delta transfer in p2p Mode | `1048576` (1 MB) |

The environment equivalents are documented per mode:
`HSYNC_DELTA_MIN_FILE_SIZE` in [Configuration — Server Mode](configuration-server.md)
and `HSYNC_DELTA_MIN_FILE_SIZE_P2P` in [Configuration — p2p Mode](configuration-p2p.md).

## Bandwidth Limiting

Bandwidth is set on the CLI only (no `HSYNC_UPLOAD_LIMIT` /
`HSYNC_DOWNLOAD_LIMIT` environment variables).

| Option | Description | Default |
|---|---|---|
| `--limit-upload` | Upload cap (e.g. `1MB`, `500KB`); `0` = unlimited | unlimited |
| `--limit-download` | Download cap (e.g. `1MB`, `500KB`); `0` = unlimited | unlimited |

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

When file logging is enabled with `-o/--log-file`:

| Variable | Default | Description |
|---|---|---|
| `HSYNC_LOG_ROTATION` | `10 MB` | Rotate when log file reaches this size |
| `HSYNC_LOG_RETENTION` | `7 days` | Keep rotated logs for this duration |
| `HSYNC_LOG_COMPRESSION` | `xz` | Compression for rotated logs |

## Auto-Tuning

| Variable | Description | Default |
|---|---|---|
| `HSYNC_AUTOTUNE` | Enable auto-tuning of timeout, retry, and concurrency | `true` |
| `HSYNC_HTTP_TIMEOUT` | HTTP request timeout in seconds (1-300); CLI `--http-timeout` | auto |
| `HSYNC_RECONNECT_DELAY` | Base of the full-jitter exponential reconnect backoff in seconds (1-60); CLI `--retry-delay` | auto (`5`) |
| `HSYNC_MAX_CONCURRENT_TRANSFERS` | Max parallel file transfers (1-20) | auto (`5`-`20`) |
| `HSYNC_TRANSFER_RETRY_MIN_WAIT` | Per-file retry min wait in seconds | auto |
| `HSYNC_TRANSFER_RETRY_MAX_WAIT` | Per-file retry max wait in seconds | auto |
| `HSYNC_CONNECTIVITY_RETRY_ATTEMPTS` | Startup connectivity check retry attempts for **one-shot (`--once`) runs** (1–20); continuous mode retries forever instead | `7` |
| `HSYNC_CONNECTIVITY_RETRY_MIN_WAIT` | Connectivity check retry min wait in seconds | `1` |
| `HSYNC_CONNECTIVITY_RETRY_MAX_WAIT` | Connectivity check retry max wait in seconds | `30` |
| `HSYNC_MAX_FILE_WORKERS` | Max parallel workers for local file scanning | auto (CPU-based) |

Use `--no-autotune` or `HSYNC_AUTOTUNE=false` to disable auto-tuning and use
built-in static defaults.

## Clock synchronisation (NTP)

In p2p mode there is no server at startup, so the client measures its clock
offset against public SNTP servers. The offset powers the chronological
conflict tie-break (`corrected_mtime`, see
[Conflict Resolution](conflict-resolution.md)): the most recent
offset-corrected edit keeps the original path. The probe runs in the
background, so discovery never waits on an external time source. Each sync
cycle freezes whatever offset is already known: a probe that finishes during
the cycle applies only to the next one. When every probe fails the client
records offset 0 (raw local time) and synchronisation proceeds normally.

| Variable | Description | Default |
|---|---|---|
| `HSYNC_NTP_HOSTS` | Comma-separated SNTP servers, tried in order; entries may carry a port override (`host:port`) | `pool.ntp.org,time.cloudflare.com,time.google.com` |

## State directory

Causal versions and deletion markers live in one SQLite database **per synced
folder**, outside the synced directory, under the device dir
(`<device_dir>/syncstate/<uuid>.db`, named by the `.hsyncfolder` UUID), plus a
small per-device index. The folder itself only holds a small `.hsyncfolder`
identity marker.

| Variable | Description | Default |
|---|---|---|
| `HSYNC_DEVICE_DIR` | Directory holding the device identity **and** the per-folder sync-state DBs | `$XDG_STATE_HOME/hsync/device` or `~/.local/state/hsync/device` |
| `HSYNC_STATE_DIR` | Installation state directory (server-side per-bucket state; also the default parent of the device dir) | `$XDG_STATE_HOME/hsync` or `~/.local/state/hsync` |
| `HSYNC_STATE_DB` | Explicit single sync-state DB for this process (overrides the per-folder layout) | unset |

## Tuning for containers and VMs

Storage and network class are **probed at runtime**. There are no
`HSYNC_AUTOTUNE_STORAGE_TYPE` or `HSYNC_AUTOTUNE_NETWORK_TYPE` overrides.
Disable autotune with `--no-autotune` / `HSYNC_AUTOTUNE=false` and set
`--http-timeout`, `--retry-delay`, and `--max-concurrent-transfers` yourself.

## Environment files

The client does **not** load `./.env` automatically. Use `--env-file PATH`,
or `export` / `source` variables in the shell before launching `hsync`.

`--env-file` only applies keys recognised by the settings model (the
`HSYNC_*` variables listed in this page and the server/p2p guides). It does
**not** set CLI-only options such as `-b/--bucket`, `-p/--path`, `--once`,
`--sync-direction`, or `--prefer-master`.

Repo templates `dev.env.example` / `prod.env.example` are meant to be copied to
`dev.env` / `prod.env` (which are git-ignored) and then sourced or passed to
`--env-file`; the templates are not read on their own.

```bash
# Load recognised HSYNC_* keys, then pass CLI-only flags
cp prod.env.example prod.env
hsync server --env-file ./prod.env server.example.com -b documents -p ~/Documents
```

## Periodic resync and the inotify watch limit (Linux)

In continuous mode (the default) the client runs a periodic full sync every
`HSYNC_RESYNC` seconds (default `300`, set `0` to disable). This is a safety
net: it recovers any file-system events the watcher may have missed.

On Linux the recursive watcher uses one inotify watch per sub-directory. Large
trees can exceed `fs.inotify.max_user_watches`: the watcher then degrades to
polling (logged as `[WATCHER FALLBACK]`) and is restored to inotify
automatically once the limit frees up. The client warns at startup when the
watched tree approaches this limit; raise it with:

```bash
current=$(cat /proc/sys/fs/inotify/max_user_watches)
sudo sysctl fs.inotify.max_user_watches=$((current * 4))
```
