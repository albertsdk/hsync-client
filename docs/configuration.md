# Configuration

This guide explains how to configure HSYNC for your environment.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HSYNC_SERVER` | Server URL | (required or use `-s`) |
| `HSYNC_API_KEY` | API key for authentication | (required) |
| `HSYNC_AUTOTUNE` | Enable auto-tuning of timeout, retry, and concurrency | `true` |
| `HSYNC_HTTP_TIMEOUT` | HTTP request timeout in seconds (1-300) | auto (`30` or `45` or `60`) |
| `HSYNC_RECONNECT_DELAY` | WS reconnection delay in seconds (1-60) | auto (`5`) |
| `HSYNC_MAX_CONCURRENT_TRANSFERS` | Max parallel file transfers (1-20) | auto (`5`-`20`) |
| `HSYNC_TRANSFER_RETRY_MIN_WAIT` | Per-file retry min wait in seconds | auto (`1`-`15`) |
| `HSYNC_TRANSFER_RETRY_MAX_WAIT` | Per-file retry max wait in seconds | auto (`10`-`30`) |
| `HSYNC_IGNORE_DELETE` | Do not propagate file deletions (backup mode) | `false` |
| `HSYNC_MAX_FILE_WORKERS` | Max parallel workers for local file scanning | auto (CPU-based) |
| `HSYNC_STABILITY_DELAY` | Seconds to wait before uploading modified files | `5` |
| `HSYNC_RESYNC` | Periodic full-resync interval (seconds) in continuous mode; `0` disables it | `300` |
| `HSYNC_IGNORE_PATTERNS` | Comma-separated extra gitignore-style patterns | `""` |
| `HSYNC_COMPRESSION_ENABLED` | Enable zstd compression for file transfers | `true` |
| `HSYNC_DELTA_ENABLED` | Enable rsync-style delta transfer for large files | `true` |
| `HSYNC_DELTA_MIN_FILE_SIZE` | Minimum file size (bytes) for delta transfer in server mode | `262144` (256 KB) |
| `HSYNC_DELTA_MIN_FILE_SIZE_LAN` | Minimum file size (bytes) for delta transfer in LAN mode | `1048576` (1 MB) |
| `HSYNC_TOMBSTONE_TTL_DAYS` | Days to retain deletion markers before garbage collection (`0` = forever) | `30` |

> `HSYNC_SERVER` and `HSYNC_API_KEY` are required in **Server Mode** only. In
> **LAN Mode** (`--lan-mode`) they are ignored; use the optional
> `HSYNC_LAN_KEY` instead (see [LAN Mode Options](#lan-mode-options)).
>
> `HSYNC_HTTP_TIMEOUT`, `HSYNC_RECONNECT_DELAY`, `HSYNC_MAX_CONCURRENT_TRANSFERS`,
> `HSYNC_TRANSFER_RETRY_MIN_WAIT`, and `HSYNC_TRANSFER_RETRY_MAX_WAIT` are
> **auto-detected** at startup based on measured network latency, bandwidth, and
> storage type. Set any of them explicitly to override auto-detection. Use
> `--no-autotune` or `HSYNC_AUTOTUNE=false` to disable auto-tuning entirely and
> use the built-in static defaults.

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

### Log rotation

When file logging is enabled with `-o/--output` (or `HSYNC_LOG_FILE`), the
client rotates logs automatically via Loguru. Configure rotation with:

| Variable | Default | Description |
|----------|---------|-------------|
| `HSYNC_LOG_ROTATION` | `10 MB` | Rotate when the log file reaches this size (examples: `100 MB`, `1 GB`, `1 day`) |
| `HSYNC_LOG_RETENTION` | `7 days` | Keep rotated logs for this duration (examples: `30 days`, `1 month`, `5` files) |
| `HSYNC_LOG_COMPRESSION` | `xz` | Compression for rotated logs (`gz`, `bz2`, `xz`, `tar.gz`, `zip`, …) |

## CLI Options

### Connection

These options apply to **Server Mode**. In **LAN Mode** (`--lan-mode`) no
server URL is required — peers are discovered automatically via mDNS, so
`-s/--server` and `HSYNC_SERVER` are ignored. See
[LAN Mode Options](#lan-mode-options) below.

| Option | Description | Default |
|--------|-------------|---------|
| `-s, --server` | Server URL (Server Mode only) | (required or `HSYNC_SERVER`) |
| `--timeout` | HTTP request timeout in seconds | auto (`30`/`45`/`60`) |
| `--ca-cert` | Path to CA certificate for self-signed server certs | — |
| `--insecure` | Skip SSL certificate verification | `false` |

### Sync

| Option | Description | Default |
|--------|-------------|---------|
| `-b, --bucket` | Bucket name | (required) |
| `-p, --path` | Local directory path | cwd |
| `-w, --watch` | Watch for changes and sync continuously. (Implicit in LAN mode) | `false` |
| `-i, --interval` | Force full sync every N seconds (requires `-w`); overrides `HSYNC_RESYNC` | — (default: `300` via env/constant) |
| `--retry-delay` | Retry connection after N seconds | auto (`5`–`15`) |
| `--stability-delay` | Seconds to wait before uploading modified files | — (default: `5` via env/constant) |
| `--max-wait-timeout` | Maximum time to wait for file stabilization | `120` |
| `--max-concurrent-transfers` | Maximum number of concurrent file transfers (1-20, overrides `HSYNC_MAX_CONCURRENT_TRANSFERS`) | — (default: `5` via env/constant) |
| `--no-autotune` | Disable auto-tuning of timeout, retry, and concurrency (use static defaults) | `false` |
| `--filter-path` | Sync only files within this subdirectory path | — |
| `-n, --dry-run` | Show what would be done without making changes | `false` |
| `--ignore-delete` | Do not propagate file deletions | `false` |
| `--sync-direction` | Sync direction: `bidirectional`, `upload-only`, `download-only` | `bidirectional` |
| `--max-bandwidth-upload` | Maximum upload bandwidth (e.g., `1MB`, `500KB`). `0` = unlimited | — |
| `--max-bandwidth-download` | Maximum download bandwidth (e.g., `1MB`, `500KB`). `0` = unlimited | — |
| `--no-compression` | Disable zstd compression for file transfers | `false` |
| `--no-delta` | Disable rsync-style delta transfer (block-level sync) for large files | `false` |
| `--delta-min-file-size` | Minimum file size in bytes for delta transfer in server mode. Default: 262144 (256 KB) | — |
| `--delta-min-file-size-lan` | Minimum file size in bytes for delta transfer in LAN mode. Default: 1048576 (1 MB) | — |
| `--min-free-disk-space` | Minimum free disk space in MB before sync. `0` = disabled | `500` |
| `--max-download-size` | Maximum size in MB of a single inbound file (download or received LAN upload), enforced as an absolute cap on top of the peer-declared size. `0` = disabled | `10240` |
| `--ignore-patterns` | Comma-separated extra gitignore-style patterns (layered on `.hsyncignore`) | — |
| `--mass-delete-guard` | Enable the mass-deletion safety guard (pause deletions over threshold) | `true` |
| `--no-mass-delete-guard` | Disable the mass-deletion safety guard | — |
| `--mass-delete-threshold` | Absolute number of deletions per cycle that trips the guard (`0` = disabled) | — (default: `50` via env/constant) |
| `--mass-delete-percent` | Percentage of local files whose deletion trips the guard (`0` = disabled) | — (default: `50` via env/constant) |

### Mass-Deletion Safety Guard

A transient failure or misconfiguration can make the master expose an empty (or
nearly-empty) bucket to connected peers. Without protection, a single
reconciliation cycle would delete every local file that is not in the master's
listing — effectively a **mass accidental wipe**.

The **mass-deletion safety guard** prevents this: before executing any deletion,
the client counts how many files would be removed in the current cycle. If the
count exceeds either the **absolute threshold** (`--mass-delete-threshold`) or
the **percentage of total local files** (`--mass-delete-percent`), the cycle is
**paused** — deletions are skipped, a `[GUARD]` warning is logged, and a
structured `sync_paused` event is emitted. The guard can be disabled entirely
with `--no-mass-delete-guard` or by setting `HSYNC_MASS_DELETE_GUARD=false`.

#### How it works

```
Sync cycle start
  └─ Compare local vs remote file lists
       └─ Count how many files would be DELETED
            │
            ├─ 0 deletions?  ──────────► proceed normally
            │
            ├─ Remote has 0 files?
            │  (empty bucket from the master) ──► BLOCK (always suspect)
            │
            ├─ deletions >= --mass-delete-threshold?  ──► BLOCK
            │
            ├─ deletions >= --mass-delete-percent% of local files? ──► BLOCK
            │
            └─ below both thresholds ──► proceed normally
```

An empty master (0 remote files while local data exists) is **always blocked**,
regardless of threshold settings — the only way to proceed in that scenario is
to explicitly disable the guard with `--no-mass-delete-guard`.

#### Configuration

| Setting | CLI flag | Env var | Default | Meaning |
|---------|----------|---------|---------|---------|
| Enable/disable | `--mass-delete-guard` / `--no-mass-delete-guard` | `HSYNC_MASS_DELETE_GUARD` | `true` | Master switch for the entire guard |
| Absolute threshold | `--mass-delete-threshold N` | `HSYNC_MASS_DELETE_THRESHOLD` | `50` | Max deletions/cycle (0 = disabled) |
| Percentage threshold | `--mass-delete-percent PCT` | `HSYNC_MASS_DELETE_PERCENT` | `50` | Max % of local files (0 = disabled) |

Priority: **CLI flag > environment variable > default**.

#### When to adjust

- **Decrease thresholds** on directories with many small, frequently-changing
  files (e.g. source trees with thousands of files) — a 50% deletion of 10,000
  files is 5,000 files, which may be legitimate after a reorganisation.
- **Increase thresholds** on backup targets where nearly all files are expected
  to be present on both sides — even 5% would be suspicious.
- **Disable entirely** (`--no-mass-delete-guard`) when you are confident the
  master state is authoritative and you want deletions to flow unimpeded.
- **Keep it enabled for one-way backups** (client → NAS/server): the guard is
  good protection against source-disk corruption, blocking a bulk local loss
  from propagating to the backup copy. For **pure backups**, though, the
  industry-standard approach is `--ignore-delete` instead — it disables
  remote deletion entirely (append-only semantics), so a file that
  disappears locally, by accident or malice, keeps its remote safety copy
  intact. See
  [Examples → Push-only backup](examples.md#13-push-only-backup-to-a-remote-server).
- **Always disable in CI/CD pipelines, automation scripts and ephemeral
  deployments** (`--no-mass-delete-guard` or `HSYNC_MASS_DELETE_GUARD=false`).
  In those environments it is perfectly normal to wipe entire directories —
  build folders, `node_modules`, cache directories — and recreate them from
  scratch on every run. With the guard active, the first deep-clean cycle
  would pause the sync and wait for a manual resume that never comes in an
  unattended run, failing the job. It is safe because the content is
  regenerated by the pipeline itself. See
  [Examples → CI/CD pipelines](examples.md#37-cicd-pipelines-automation-scripts-and-ephemeral-deployments).

#### Related safety features

The guard complements other deletion-safety measures:

- **`--ignore-delete`** — never propagate any deletions at all (backup mode).
- **`--sync-direction upload-only`** — never pull remote changes; local data
  can't be deleted by the master.
- **Conflict resolution** — the three-way merge preserves local edits as
  `.conflict-*` files before applying a conflicting remote deletion (see
  [Conflict Resolution](conflict-resolution.md)).

### Output

| Option | Description |
|--------|-------------|
| `-v, --verbose` | Enable verbose output |
| `-q, --quiet` | Suppress all output |
| `-o, --output` | Write output to file |
| `--no-color` | Disable colored output |
| `--help-symbols` | Show output symbols legend |

Colored output is also disabled automatically when stdout is not an interactive
terminal (containers, pipes, systemd units, cron) or when the standard
[`NO_COLOR`](https://no-color.org) environment variable is set to any non-empty
value. At startup the client logs one line describing the detected runtime
environment (container runtime, cgroup memory/CPU limits, TTY state).

### General

| Option | Description |
|--------|-------------|
| `-y, --yes` | Skip interactive confirmation prompts (still shows warnings) |
| `--env-file` | Load environment variables from a file |
| `--capabilities-json` | Print the binary's capabilities document as JSON and exit | — |
| `-V, --version` | Show version | — |

### Non-interactive runs (containers, systemd, cron)

When no interactive terminal is attached (e.g. inside a container started
without `-t`, or under a systemd unit), no operator can answer confirmation
prompts: if one would be required (e.g. the deletion-risk prompt), the client
fails fast with a clear error instead of blocking. Pass `-y/--yes` for
unattended runs. In LAN mode, write-capable sync without an interactive
terminal additionally requires `HSYNC_LAN_KEY` (see
[LAN Mode Options](#lan-mode-options)).

## LAN Mode Options

This is the single reference for every LAN (`--lan-mode`) option and `HSYNC_*`
environment variable. For the end-user guide see [LAN Mode](lan-mode.md); for the
protocol internals see [LAN Internals](internal/lan-internals.md).

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
| `HSYNC_LAN_HTTPS` | Use HTTPS for LAN peer communication (always on; self-signed cert generated per master) | `true` (always on, not configurable) |
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
hsync -s https://server.example.com:9753 -b mybucket
```

### Connecting to a Server with Self-Signed Certificate

When the server uses a self-signed certificate, you have two options:

**Option 1: Trust the server certificate (recommended)**

```bash
# Copy the server's certificate to the client machine
scp server:/path/to/certs/server.crt ./server.crt

# Connect using the CA certificate
hsync -s https://server:9753 -b mybucket --ca-cert server.crt
```

**Option 2: Disable SSL verification (not recommended for production)**

```bash
# Skip certificate verification (insecure!)
hsync -s https://server:9753 -b mybucket --insecure
```

> ⚠️ **Warning**: Using `--insecure` disables all SSL certificate verification, making the connection vulnerable to man-in-the-middle attacks. Only use this for testing.

### SSL Examples

**Watch mode with self-signed certificate:**
```bash
hsync -s https://server:9753 -b mybucket -w --ca-cert server.crt
```

**Dry-run with HTTPS:**
```bash
hsync -s https://server:9753 -b mybucket -n --ca-cert server.crt
```

## File Compression

HSYNC supports automatic file compression to optimize transfers using **Zstandard (zstd)**.
Compression is enabled by default and automatically skips already-compressed formats
(images, videos, archives, documents). Disable it when transferring over fast local
networks or when CPU is constrained:

| Option | Description |
|--------|-------------|
| `--no-compression` | Disable compression for this session |
| `HSYNC_COMPRESSION_ENABLED` | Environment variable to disable compression (default: `true`) |

> For details on compression thresholds and protocol-level negotiation, see the
> [Developer Guide → Compression Internals](internal/developer.md#compression-internals).

## Delta Transfer

HSYNC supports **rsync-style delta transfer** for large files with small changes.
Instead of transferring the entire file, only the changed blocks are sent,
significantly reducing bandwidth usage and transfer time.

### How it works

Delta transfer uses a rolling checksum algorithm (Adler32 + SHA-256) to identify
which blocks of a file have changed. The client computes block signatures of the
local base file, sends them to the server/master, which matches them against the
new version and returns delta instructions (copy/data operations). The client then
rebuilds the file from these instructions.

### When delta transfer is used

Delta transfer is attempted automatically when **all** of these conditions are met:

1. **File size threshold**: file is ≥ 256 KB (server mode) or ≥ 1 MB (LAN mode)
2. **Base hash exists**: a previously synced version is recorded in `.hsyncstate`
3. **Direction check**:
   - **Upload**: local file changed (`local_sha256 != base_hash`)
   - **Download**: local file is still the base and remote file changed
4. **Capability**: both peers support delta transfer

If any condition is not met, the file is transferred in full.

### Configuration

Delta transfer is **enabled by default**. Disable it with:

```bash
# Environment variable
export HSYNC_DELTA_ENABLED=false
```

Adjust the size thresholds:

```bash
# Lower threshold to 128 KB (server mode)
export HSYNC_DELTA_MIN_FILE_SIZE=131072

# Lower threshold to 512 KB (LAN mode)
export HSYNC_DELTA_MIN_FILE_SIZE_LAN=524288
```

### Fallback behavior

Delta transfer failures are **transparent**: if any step fails (network error,
hash mismatch, peer doesn't support delta, base changed mid-transfer), the client
automatically falls back to a full file transfer. The retry policy is unchanged —
delta failures do not consume retry attempts.

### Performance considerations

- **Bandwidth savings**: delta transfer can reduce transfer size by 90%+ for files
  with small changes (e.g., a 100 MB video file with a 1 MB edit)
- **CPU overhead**: rolling checksum computation adds CPU cost, but is typically
  negligible compared to network I/O
- **Memory usage**: delta operations require holding block signatures in memory
  (~16 bytes per block for a 4 KB block size)

> For the technical details of the delta transfer protocol, see
> [Sync Flow → Delta Transfer](sync-flow.md#delta-transfer-rsync-style-block-sync).

## Bandwidth Limiting

HSYNC supports upload and download bandwidth limiting using a **token bucket** algorithm for smooth rate control.

### Usage

```bash
# Limit upload to 1 MB/s
hsync -s https://server.example.com:9753 -b mybucket -w --max-bandwidth-upload 1MB

# Limit download to 500 KB/s
hsync -s https://server.example.com:9753 -b mybucket -w --max-bandwidth-download 500KB

# Limit both directions
hsync -s https://server.example.com:9753 -b mybucket -w --max-bandwidth-upload 2MB --max-bandwidth-download 1MB
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

> **Where to put the file — and the nested-file limitation.**
> The `.hsyncignore` file **must live at the root of the synced bucket
> directory** — i.e. the exact folder you pass as the local path / shared
> folder, not a subdirectory inside it. HSYNC reads **only** the ignore file at
> the bucket root; **nested `.hsyncignore` files in subdirectories are NOT
> supported** (unlike Git, which reads a `.gitignore` per directory). If the
> file is placed in a subfolder it is silently ignored and no rules are applied,
> so excluded content (e.g. `build/`) will still be synced. Patterns are matched
> at any depth, so a root-level rule such as `build/` already covers nested
> directories like `some/module/build/`. As an alternative to a file, pass
> patterns directly with `--ignore-patterns` (see below), which apply regardless
> of location.
>
> Note also that the ignore rules only prevent *future* transfers: files that
> were already synced to the server before a rule was added are **not** removed
> retroactively and must be deleted from the bucket manually.

### Default behaviour

Following the industry standard (Syncthing, rsync, Nextcloud, OneDrive), HSYNC
synchronises **all files by default**, including dotfiles (`.env`, `.git/`,
`.config/`, …). Only a built-in **ignore list** of operating-system cache files
and editor/office temporary files is excluded automatically:

| Pattern | Category | Example excluded files |
|---------|----------|------------------------|
| `.DS_Store` | macOS cache | macOS Finder metadata |
| `._*` | macOS cache | Apple Double resource fork files (e.g. `._myfile.pdf`) |
| `.Spotlight-V100` | macOS cache | Spotlight index directory |
| `.Trashes` | macOS cache | Trash folder on removable drives |
| `Thumbs.db` | Windows cache | Windows Explorer thumbnail cache |
| `Thumbs.db:encryptable` | Windows cache | Encrypted thumbnail cache (NTFS alternate data stream) |
| `ehthumbs.db` | Windows cache | Windows Media Center thumbnail cache |
| `ehthumbs_vista.db` | Windows cache | Windows Vista thumbnail cache |
| `desktop.ini` | Windows cache | Folder customisation metadata |
| `~$*` | Office lock | Microsoft Office lock/temp files (e.g. `~$document.docx`) |
| `.*.sw?` | Editor swap | Vim/Neovim swap files — dotfiles only (e.g. `.hello.txt.swp`, `.hello.txt.swx`). Does **not** exclude non-hidden files like `animation.swf` |
| `*~` | Editor backup | Backup files created by editors (e.g. `file.txt~`) |
| `4913` | Editor temp | Legacy Vim temporary file |
| `.~lock.*#` | Editor lock | LibreOffice/OpenOffice lock files (e.g. `.~lock.document.odt#`) |
| `*.tmp` | Generic temp | Temporary files (e.g. `output.tmp`) |
| `*.temp` | Generic temp | Temporary files (e.g. `draft.temp`) |
| `*.bak` | Generic backup | Backup files (e.g. `config.bak`) |
| `*-????-??-??-??????` | Date-stamped backup | Timestamped backup files (e.g. `archive-2024-01-15-120000`) |

These built-in patterns are **always active** and cannot be disabled. Users can
re-include any of them via negation rules in `.hsyncignore` (e.g. `!*.tmp`).

The sentinel file `.hsynckeep` is **always included** regardless of any rule.

Engine-owned artifacts (`.hsyncignore`, `.hsyncstate`, `.hsynctombstones`,
`*.part`) are never synced.

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

# Ignore editor swap files (already in defaults; explicit for clarity)
.*.sw?

# Re-include a specific log file
!important.log
```

### Excluding dotfiles

Since dotfiles are synchronised by default, to exclude specific dotfiles or
dotfile directories (e.g. `.git/`, `.env`) add them to `.hsyncignore`:

```
# Exclude .git directory
.git/

# Exclude .env files
.env
```

Or use `--ignore-patterns`:

```bash
hsync -s https://server.example.com:9753 -b mybucket --ignore-patterns ".git/,.env"
```

### Extra patterns via CLI

Pass extra patterns without creating a `.hsyncignore` file:

```bash
hsync -s https://server.example.com:9753 -b mybucket --ignore-patterns "*.log,/build/,/dist/"
```

### Hot-reload

The client watches `.hsyncignore` for changes while it is running.  Any
modification to the file is picked up automatically — no restart required.
