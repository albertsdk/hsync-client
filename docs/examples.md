# Examples

Practical usage recipes for the `hsync` binary. For an overview of CLI options
and log labels see [Usage](usage.md) and [Configuration](configuration.md).

This document has been split by deployment mode:

- **[Examples — Server Mode](examples-server.md)** — central server examples (sections 1.1–1.8)
- **[Examples — p2p Mode](examples-p2p.md)** — peer-to-peer examples (sections 2.1–2.8)

## Common Operational Patterns

These patterns apply to **both** Server Mode and p2p Mode.

### 3.1 Dry run (preview changes)

```bash
# Preview changes without modifying any files
hsync server --dry-run -b mybucket -p ~/Sync
```

Shows what would change without modifying any files.

### 3.2 Verbose output

```bash
# Enable verbose output to see detailed sync progress
hsync server -v -b mybucket -p ~/Sync
```

### 3.3 Log to file

```bash
# Write log output to a file instead of the console
hsync server --log-file /var/log/hsync.log -b mybucket -p ~/Sync
```

### 3.4 File stability tuning

```bash
# Wait 2s for files to settle before syncing; give up after 60s
hsync server -b mybucket -p ~/Sync --stability-delay 2 \
  --max-wait-timeout 60
```

### 3.5 CI/CD non-interactive sync

```bash
# Non-interactive sync for CI/CD: -y skips confirmation prompts
hsync server -y -b artifacts -p ./build
```

### 3.6 Ignore patterns

```bash
# Exclude temporary, log and build files from the sync
HSYNC_IGNORE_PATTERNS="*.tmp,*.log,/build/" hsync server -b mybucket -p ~/Sync
```

Or create a `.hsyncignore` file in the bucket root.

### 3.7 Delta transfer for large files

```bash
# Transfer only changed blocks for files larger than 256 KiB
hsync server -b large -p ~/Large \
  --delta-min-file-size 262144
```

### 3.8 Backup mode (never delete)

```bash
# Backup mode: sync uploads but never apply remote deletes locally
hsync server --ignore-delete -b backup -p ~/Backup
```

### 3.9 Resync interval

```bash
# Run a full resync every 10 minutes as a safety net for missed events
hsync server -b mybucket -p ~/Sync --interval 600
```

Full resync every 10 minutes as a safety net for missed events.
