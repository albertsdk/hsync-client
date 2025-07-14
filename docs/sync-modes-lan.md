# Sync Modes — LAN P2P Mode

HSYNC supports two operating topologies. This page covers **LAN P2P Mode**.

> For Server Mode, see [Sync Modes — Server Mode](sync-modes-server.md).

## LAN P2P Mode

In LAN P2P Mode, devices communicate directly over the local network without
a central server. One device is elected **master** and serves files to all
other devices (**slaves**).

```bash
hsync -b mybucket -p ~/Sync --lan-mode
```

### How it works

1. Each peer announces itself on the LAN via mDNS.
2. A deterministic **master election** picks one peer as the master.
3. The master runs HTTPS/WSS servers; slaves connect to it.
4. File changes are broadcast in real time: the master pushes metadata, slaves
   pull bytes (or upload to the master if they made local changes).

### Master Election

| Priority | Criterion | Winner |
|---|---|---|
| 1 | `server_ready` (incumbent) | Already-serving master keeps the role |
| 2 | `force_master` flag | Operator-pinned master wins over ordinary peers |
| 3 | Bidirectional `sync_direction` | Fully bidirectional preferred |
| 4 | Earliest `startup_time` | Node that started first wins |
| 5 | Lowest `cid` (lexicographic) | Deterministic tiebreaker |

### Sync Directions

In LAN mode, the **slave**'s sync direction controls what it sends to the master:

| Direction | CLI Flag | Description |
|---|---|---|
| **Bidirectional** (default) | `--sync-direction bidirectional` | Send changes to master AND receive from master. |
| **Upload-only** | `--sync-direction upload-only` | Send changes, ignore what other peers do. |
| **Download-only** | `--sync-direction download-only` | Only receive from master, never upload. |

### Watch Mode

In LAN mode, watch mode is **always active** — there is no `--watch` flag.
Once a peer connects, it continuously monitors the local folder and the
master's event stream.

### Single Sync (one-shot)

In LAN Mode, the client is **persistent by default**: it stays connected and
reacts to changes continuously (watch mode is implicit). To run a **single
sync cycle and exit**, use `--no-lan-persistent`:

```bash
hsync -b mybucket -p ~/Sync --lan-mode --no-lan-persistent
```

This compares local and remote files, syncs changes, and shuts down — useful
for cron jobs or manual "sync now" operations on a LAN.

### Dry Run

Preview what would change without modifying files:

```bash
hsync --dry-run -b mybucket -p ~/Sync --lan-mode
```

### Conflict Handling

The master is the authoritative source. Conflicts are resolved identically to
Server Mode: the master's version wins, the slave's version is renamed to
`.hsyncconflict`.

See [Conflict Resolution](conflict-resolution.md) for the full algorithm.

### Further Reading

- [LAN Mode Guide](lan-mode.md) — detailed walkthrough
- [Configuration — LAN P2P Mode](configuration-lan.md) — all LAN-mode CLI options
- [Authentication — Client-Side](authentication.md) — auth flow diagrams (LAN with/without key)
- [LAN Protocol spec](https://github.com/albertobasso/hsync-common/blob/main/docs/lan-protocol.md) — authoritative election + security reference
