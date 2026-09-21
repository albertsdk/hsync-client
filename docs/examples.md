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
hsync server --dry-run -b mybucket -p ~/Sync
```

Shows what would change without modifying any files.

### 3.2 Verbose output

```bash
hsync server -v -b mybucket -p ~/Sync
```

### 3.3 Log to file

```bash
hsync server --log-file /var/log/hsync.log -b mybucket -p ~/Sync
```

### 3.4 File stability tuning

```bash
HSYNC_STABILITY_DELAY=2 HSYNC_MAX_WAIT_TIMEOUT=60 hsync server -b mybucket -p ~/Sync
```

### 3.5 CI/CD non-interactive sync

```bash
hsync server -y -b artifacts -p ./build
```

### 3.6 Ignore patterns

```bash
HSYNC_IGNORE_PATTERNS="*.tmp,*.log,/build/" hsync server -b mybucket -p ~/Sync
```

Or create a `.hsyncignore` file in the bucket root.

### 3.7 Delta transfer for large files

```bash
HSYNC_DELTA_ENABLED=true HSYNC_DELTA_MIN_FILE_SIZE=262144 hsync server -b large -p ~/Large
```

### 3.8 Backup mode (never delete)

```bash
hsync server --ignore-delete -b backup -p ~/Backup
```

### 3.9 Resync interval

```bash
HSYNC_RESYNC=600 hsync server -b mybucket -p ~/Sync
```

Full resync every 10 minutes as a safety net for missed events.
