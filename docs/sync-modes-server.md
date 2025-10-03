# Sync Modes — Server Mode

HSYNC supports two operating topologies. This page covers **Server Mode**.

> For LAN P2P mode, see [Sync Modes — LAN P2P Mode](sync-modes-lan.md).

## Server Mode

In Server Mode, a central HSYNC Server holds the canonical copy of each bucket.
Every client connects to the server, authenticates with an API key, and
synchronizes files through it.

```bash
hsync -s https://hsync.example.com:9753 -b mybucket -p ~/Sync
```

### How it works

1. The client connects to the server over HTTPS/WSS.
2. Authenticates with `HSYNC_API_KEY`.
3. Performs an initial full sync (bidirectional by default).
4. Enters watch mode: monitors the local folder and receives real-time events
   from the server via WebSocket.

### Sync Directions

| Direction | CLI Flag | Description |
|---|---|---|
| **Bidirectional** (default) | `--sync-direction bidirectional` | Send local changes to server AND receive remote changes. |
| **Upload-only** | `--sync-direction upload-only` | Send local changes, ignore remote. |
| **Download-only** | `--sync-direction download-only` | Receive remote changes, ignore local. |

### Watch Mode

When `--watch` is enabled:

- The client monitors the local folder for changes (create, modify, delete, rename).
- Changes are immediately uploaded to the server.
- The WebSocket pushes remote changes to the client in real time.

```bash
hsync --watch -s https://hsync.example.com:9753 -b mybucket -p ~/Sync
```

### Single Sync (one-shot)

One-shot mode is the **default** — when you omit `-w` / `--watch`, the client
runs **one full sync cycle** and exits. It compares local and remote files,
uploads changes, downloads updates, deletes obsolete files (unless
`--ignore-delete` is set), then shuts down.

This is ideal for:
- **Cron jobs** — schedule periodic sync with `cron` or systemd timers
- **CI/CD pipelines** — sync build artifacts after a deployment
- **Manual "sync now"** — run on demand without a persistent process

```bash
hsync -s https://hsync.example.com:9753 -b mybucket -p ~/Sync
```

To keep the client running and reacting to changes in real time, add `-w` /
`--watch`.

### Dry Run

Preview what would change without modifying files:

```bash
hsync --dry-run -s https://hsync.example.com:9753 -b mybucket -p ~/Sync
```

### Conflict Handling

The server is a **transport hub, not an authority**: it never decides conflicts
by role. Every peer resolves causally with the same vector-clock matrix:

1. Content identity (SHA-256) is checked first; causal dominance then decides
   the direction.
2. On a true concurrent divergence, the content with the highest SHA-256 wins
   deterministically (impartial to any node).
3. The losing content is preserved as
   `filename_conflicted_copy_<timestamp>_<cid>.<ext>`, by whichever side lost
   (the reconciling client or the upload arbitration on the server).

Stale uploads (causally dominated) are rejected with `409` and no copy is
created — nothing is lost. See [Conflict Resolution](conflict-resolution.md)
for the full algorithm.

### Further Reading

- [Server Mode Guide](server-mode.md) — detailed walkthrough
- [Configuration — Server Mode](configuration-server.md) — all server-mode CLI options
- [Authentication — Client-Side](authentication.md) — auth flow diagrams
