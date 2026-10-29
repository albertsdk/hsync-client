# Examples

Practical usage recipes for the `hsync` binary. For an overview of CLI options
and log labels see [Usage](usage.md) and [Configuration](configuration.md).

This document has been split by deployment mode:

- **[Examples — Server Mode](examples-server.md)** — central server examples (sections 1.1–1.8)
- **[Examples — p2p Mode](examples-p2p.md)** — peer-to-peer examples (sections 2.1–2.9)

## Common Operational Patterns

These patterns apply to **both** Server Mode and p2p Mode.

### 3.1 Dry run (preview changes)

```bash
# Preview changes without modifying any files
hsync server --server-url server.example.com -n -b mybucket -p ~/Sync
```

Shows what would change without modifying any files.

### 3.2 Verbose output

```bash
# Enable verbose output to see detailed sync progress
hsync server --server-url server.example.com -v -b mybucket -p ~/Sync
```

### 3.3 Log to file

```bash
# Write log output to a file instead of the console
hsync server --server-url server.example.com --log-file /var/log/hsync.log -b mybucket -p ~/Sync
```

### 3.4 File stability tuning

```bash
# Wait 2s for files to settle before syncing; give up after 60s
hsync server --server-url server.example.com -b mybucket -p ~/Sync \
  --stability-delay 2 --max-wait-timeout 60
```

### 3.5 CI/CD non-interactive sync

```bash
# Non-interactive sync for CI/CD: --allow-deletions accepts the deletion-risk confirmation
hsync server --server-url server.example.com --allow-deletions -b artifacts -p ./build
```

### 3.6 Ignore patterns

```bash
# Exclude temporary, log and build files from the sync
hsync server --server-url server.example.com -b mybucket -p ~/Sync \
  --ignore-pattern "*.tmp" \
  --ignore-pattern "*.log" \
  --ignore-pattern "/build/"
```

Or create a `.hsyncignore` file in the bucket root.

### 3.7 Delta transfer for large files

```bash
# Delta is enabled by default for files ≥ 256 KiB in server mode
hsync server --server-url server.example.com -b large -p ~/Large
```

### 3.8 Backup mode (never delete)

```bash
# Never propagate deletions in either direction
hsync server --server-url server.example.com --deletion-policy keep -b backup -p ~/Backup
```

For a one-way backup (push only, destination never loses data), combine it with
`--sync-direction upload-only` — see
[Examples — Server Mode 1.3](examples-server.md#13-push-only-backup-to-a-remote-server).

### 3.9 Resync interval

```bash
# Run a full resync every 10 minutes as a safety net for missed events
hsync server --server-url server.example.com -b mybucket -p ~/Sync -i 600
```

Full resync every 10 minutes as a safety net for missed events.
