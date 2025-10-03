# FAQ — Server Mode

> For LAN P2P FAQ, see [FAQ — LAN P2P Mode](faq-lan.md).

## General

**Q: What is HSYNC Server Mode?**
Server mode is where a central HSYNC Server holds the canonical copy of your
files. All devices sync through it — the server is the authoritative source.

**Q: Is the server required for HSYNC to work?**
No. You can use LAN P2P Mode without a server. The server is only needed if
you want to sync devices across different networks (e.g., home ↔ office) or
if you prefer a central hub.

**Q: Can I use both Server Mode and LAN Mode at the same time?**
No. Each HSYNC instance operates in one mode at a time. Configure based on
your use case.

## Security & Safety

**Q: How does HSYNC keep my files safe?**
- All traffic uses HTTPS/WSS (TLS encrypted).
- The API key authenticates each client to the server.
- Files are never deleted immediately — they go through a tombstone retention
  period (default 30 days).
- SHA-256 verification ensures files are not corrupted in transit.

**Q: What happens if two clients modify the same file simultaneously?**
The server is a transport hub, not an authority. Concurrent edits are
resolved causally (ADR-0022): the deterministic SHA-256 tie-break picks the
winner, and the losing version is preserved as a `_conflicted_copy_`. See
[Conflict Resolution](conflict-resolution.md).

**Q: Can I recover accidentally deleted files?**
Yes. The server keeps tombstone records for deleted files (configurable
retention, default 30 days). Files can be restored during that window.

## Sync Behavior

**Q: How often does HSYNC sync?**
In watch mode, syncing is near-instant — changes are detected by the file
watcher and pushed immediately. You can also configure periodic full resyncs
with `HSYNC_RESYNC`.

**Q: What happens if the server goes offline?**
The client retries the WebSocket connection with exponential backoff. When
the server comes back, a full sync reconciles any changes made during the
downtime.

**Q: Can I exclude certain files or directories?**
Yes. Create a `.hsyncignore` file in your bucket directory using standard
gitignore syntax. You can also add extra patterns via `HSYNC_IGNORE_PATTERNS`.

## Files & Performance

**Q: Is there a file size limit?**
The server enforces a configurable maximum upload size (`HSYNC_MAX_UPLOAD_SIZE`,
default 10 GiB). Files larger than this are rejected.

**Q: Does HSYNC use a lot of bandwidth?**
Only changed files are transferred. For large files, delta transfer
(rsync-style block sync) sends only the changed blocks. Compression (zstd)
reduces transfer size further. You can also set bandwidth limits.

**Q: Can HSYNC handle many small files?**
Yes, but initial sync of thousands of small files may take time due to
per-file overhead. The event-driven architecture handles ongoing changes to
many files efficiently.

## Debian Package

**Q: How do I configure the systemd service for server mode?**
Set the environment variables in `/etc/hsync/hsync.env`:
```
HSYNC_SERVER="https://your-server:9753"
HSYNC_API_KEY="your-api-key"
HSYNC_BUCKET="mybucket"
HSYNC_PATH="/path/to/sync"
HSYNC_WATCH=true
```
Then restart with `systemctl restart hsync`.
