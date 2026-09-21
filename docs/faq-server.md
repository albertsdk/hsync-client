# FAQ — Server Mode

> For p2p FAQ, see [FAQ — p2p Mode](faq-p2p.md).

## General

**Q: What is HSYNC Server Mode?**
Server mode is where a central HSYNC Server relays files between your devices
and keeps a copy of the synced content. The server is a **transport hub**, not
an authority: conflicts are resolved causally by every client with the same
algorithm, and no node's copy wins by role.

**Q: Is the server required for HSYNC to work?**
No. You can use p2p Mode without a server. The server is only needed if
you want to sync devices across different networks (e.g., home ↔ office) or
if you prefer a central hub.

**Q: Can I use both Server Mode and p2p Mode at the same time?**
No. Each HSYNC instance operates in one mode at a time. Configure based on
your use case.

## Security & Safety

**Q: How does HSYNC keep my files safe?**
- All traffic uses HTTPS/WSS (TLS encrypted).
- The API key authenticates each client to the server.
- Deletions take effect immediately on disk. A causal tombstone marker is
  recorded so offline peers do not resurrect the file (retention of the
  marker, default 90 days). Tombstones are not a recycle bin.
- SHA-256 verification ensures files are not corrupted in transit.

**Q: What happens if two clients modify the same file simultaneously?**
The server is a transport hub, not an authority. Concurrent edits are
resolved causally (ADR-0022): the later `corrected_mtime` keeps the original
path (ADR-0023); SHA-256 is the fallback when that field is missing or tied.
The losing version is preserved as a `_conflicted_copy_`. See
[Conflict Resolution](conflict-resolution.md).

**Q: Can I recover accidentally deleted files?**
No. Tombstones are deletion *markers* (hashed path, content hash, vector
clock). They stop a stale copy from coming back; they do not store file
contents and there is no restore command or API. Keep verified backups if you
need undelete. Concurrent-edit losers are a different mechanism
(`_conflicted_copy_` files) and only apply to that conflict, not to ordinary
deletes. See [Conflict Resolution](conflict-resolution.md).

## Sync Behavior

**Q: How often does HSYNC sync?**
In continuous mode (the default), syncing is near-instant — changes are detected by the file
watcher and pushed immediately. You can also configure periodic full resyncs
with `HSYNC_RESYNC`.

**Q: What happens if the server goes offline?**
The client retries the WebSocket connection forever with a **full-jitter
exponential backoff**: each consecutive failure doubles the upper bound of
the wait window (base `--retry-delay` / `HSYNC_RECONNECT_DELAY`, default
5 seconds), capped at 60 seconds, with a uniformly random delay inside that
window. Autotune may adjust the base from network RTT. The daemon never
exits on a lost connection; per-file transfer retries use a separate
exponential backoff. When the connection succeeds again, a full sync
reconciles changes made during the downtime — including files deleted while
offline, which are removed from the server instead of being downloaded back.

**Q: What happens if the client process is killed?**
On restart, a full sync reconciles any changes made while the process was
dead. Files created or modified while offline are uploaded, and files deleted
while offline are removed from the server. A file uploaded just before the
crash and then deleted while the process was dead is still removed on restart:
the agreed content is persisted to disk on every real-time transfer, so a
crash cannot lose it and resurrect the file.

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
many small files efficiently.

**Q: Why does the log say the file watcher degraded to polling (`[WATCHER FALLBACK]`, inotify limits)?**
The machine's per-user kernel inotify limits are exhausted — a condition of
the host, not of HSYNC (editors, browsers and desktop services each hold
watcher instances). Real-time sync continues on the polling fallback (higher
CPU, ~1 s event latency) and inotify is restored automatically once the
limits free up (`[WATCHER RESTORE]`). A `[WATCHER ERROR]` line instead means
even the polling fallback could not start. See
[The file watcher degrades to polling (inotify limits)](troubleshooting.md) for the
diagnosis commands and the remedies (raise the limits or free some up).

**Q: What are the `.hsynckeep` files that keep appearing in my synced folder?**
HSYNC creates a small, empty (0-byte) file named `.hsynckeep` in every folder
that is otherwise empty. It is a marker — like Git's `.gitkeep` — that tells
sync "this folder exists, keep it".

**Why are they created?** HSYNC synchronizes files, so a folder with nothing
in it has nothing to transfer and would be lost on your other devices. The
marker makes the folder visible to sync, so empty folders are preserved
everywhere.

**When are they created?** At the start of every sync, HSYNC scans your folder
and adds a `.hsynckeep` to any directory with no files of its own. A directory
that only contains other directories, or only hidden files, also counts as
empty. A marker is also added when the last file in a directory is deleted.

**Are they synced to other devices?** No. `.hsynckeep` files are local,
per-device markers: they are never uploaded or downloaded, and each device
creates and keeps its own.

**Can I delete them?** You can, but HSYNC will recreate them at the next sync.
They are harmless: zero bytes, hidden, and excluded from both sync and Git.

## Debian Package

**Q: How do I configure the systemd service for server mode?**
The packaged unit starts **p2p** mode (`hsync p2p`, continuous by default)
and reads `/etc/hsync/hsync.conf`. `HSYNC_BUCKET` / `HSYNC_PATH` in that file
are expanded by systemd into `--bucket` / `--path`; they are not client
settings keys.

To run server mode, override `ExecStart` (for example with `systemctl edit
hsync`) so it calls `hsync` with `-s` / `-b` / `-p` and set
`HSYNC_API_KEY` in `hsync.conf`. See [Debian package](debian-package.md).
