# Sync Flow — Server Mode

This document describes how the HSYNC client handles file synchronization when
connected to a central server.

> For p2p sync flow, see [Sync Flow — p2p Mode](sync-flow-p2p.md).

## 1. Initial Connection

1. **Config Load**: The client reads environment variables (`HSYNC_SERVER_URL`,
   `HSYNC_API_KEY`, `HSYNC_BUCKET`, `HSYNC_PATH`).
2. **Server Connection**: The client connects to the server over HTTPS/WSS.
3. **WebSocket Auth**: The client sends an auth frame with its API key, app
   version, protocol version, and capabilities. The server validates and replies.
4. **Initial Sync**: The client performs a full bidirectional sync — comparing
   local files with the server's file list and transferring differences.

## 2. Real-Time Continuous Mode

Once the initial sync completes, the client enters continuous mode (the default):

1. **File Watcher (watchdog)**: Monitors the local bucket for create, modify,
   delete, rename events.
2. **WebSocket Listener**: Receives remote change events from the server.
3. **Event Loop**: Both sources feed into a unified event queue.

### Event Flow (Server Mode)

```text
Local FS change (watchdog)
  │
  ├── file created/modified → upload to server (HTTP POST)
  ├── file deleted → notify server (HTTP DELETE)
  └── file renamed → notify server ("renamed" WS event; server moves its copy)

Remote WS event (server)
  │
  ├── "added" → download file (HTTP GET)
  ├── "modified" → download file (HTTP GET)
  ├── "deleted" → delete local file/dir
  └── "renamed" → rename local file/dir
```

### Renames never re-upload the file

A move is propagated as a move, never as "delete the old path and send the file
again", in both directions:

- **Rename while connected**: the client announces it on the WebSocket and the
  server renames its own copy. No bytes cross the wire.
- **Rename while the client was not running** (offline, or a one-shot run): the
  next sync cycle finds the old path still on the server (with a deletion
  marker recorded here) and the new path locally. Both sides advertise the same
  SHA-256 and size, so the client asks the server to move its copy
  (`PATCH …/files`, see the server's API reference) instead of deleting and
  re-uploading. The request carries the expected checksum, so a file that
  changed in the meantime is uploaded normally rather than moved; a destination
  that appeared concurrently is a conflict and takes the same fallback.
- **Receiving a rename**: a delete plus a download of identical content is
  applied as a local move, so a peer that was offline during the rename also
  converges without downloading the file.

The preview reports these as renames, and the cycle counters show them under
`renamed` — a rename moves no bytes.

## 3. Conflict Handling

The server is a **transport hub**, not an authority. No node decides conflicts
by role: every peer resolves them causally with the same algorithm.

1. A three-way merge compares the local hash/clock, the remote hash/clock and
   the recorded base state.
2. On a true concurrent divergence, the later **`corrected_mtime`** keeps the
   original path; SHA-256 is the fallback when that field is missing or tied.
   The losing content is preserved as a `_conflicted_copy_`.
3. The conflict is logged so the user can review the preserved copy.

See [Conflict Resolution](conflict-resolution.md) for the full algorithm.

## 4. Connection Recovery

- If the WebSocket connection drops, the client waits a **full-jitter
  exponential backoff** based on `--retry-delay` (default 5 seconds, capped at
  60) and tries again, forever — the daemon never exits on a lost connection.
  Per-file transfers use exponential backoff separately.
- On reconnect, a full sync is triggered to reconcile any missed events.
- Changes made while the connection was down (files created, modified, or
  deleted, plus new directories) are all reconciled by that full sync: new and
  modified files are uploaded, and files deleted while offline are removed from
  the server instead of being downloaded back (deletions are recorded as causal
  tombstones so they cannot be resurrected).
- If the client process is killed and restarted, the same full sync on startup
  reconciles any changes made while it was dead. A file uploaded just before the
  crash and then deleted while the process was dead is still removed from the
  server on restart — the agreement on the uploaded content is persisted to disk
  on every real-time transfer, so a crash cannot lose it.
