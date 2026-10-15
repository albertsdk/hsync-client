# Sync Modes — p2p Mode

HSYNC supports two operating topologies. This page covers **p2p Mode**.

> For Server Mode, see [Sync Modes — Server Mode](sync-modes-server.md).

## p2p Mode

In p2p Mode, devices communicate directly without a central server. One device
is elected **master** and serves files to all other devices (**slaves**).
Discovery has two plans: **mDNS** on the same local network (default) and an
optional **bootnode** for groups spread across networks; the election and the
data plane are the same in both.

```bash
# The shared key authorizes writes; without it the group is read-only
HSYNC_P2P_KEY="shared-secret" hsync p2p -b mybucket -p ~/Sync
```

### How it works

1. Each peer announces itself — via mDNS on the local network, or by
   registering with the configured bootnode (see
   [Sync across networks](p2p-bootnode.md)).
2. A deterministic **master election** picks one peer as the master.
3. The master runs HTTPS/WSS servers; slaves connect to it.
4. File changes are broadcast in real time: the master pushes metadata, slaves
   pull bytes (or upload to the master if they made local changes).

### Master Election

| Priority | Criterion | Winner |
|---|---|---|
| 1 | `server_ready` (incumbent) | Already-serving master keeps the role |
| 2 | `prefer_master` flag | Operator-pinned master wins over ordinary peers |
| 3 | Bidirectional `sync_direction` | Fully bidirectional preferred |
| 4 | Earliest `startup_time` | Node that started first wins |
| 5 | Lowest `cid` (lexicographic) | Deterministic tiebreaker |

### Sync Directions

In p2p mode, the **slave**'s sync direction controls what it sends to the master:

| Direction | CLI Flag | Description |
|---|---|---|
| **Bidirectional** (default) | `--sync-direction bidirectional` | Send changes to master AND receive from master. |
| **Upload-only** | `--sync-direction upload-only` | Send changes, ignore what other peers do. |
| **Download-only** | `--sync-direction download-only` | Only receive from master, never upload. |

### Continuous Mode

Continuous operation is the **default** in both p2p and server mode. In p2p
mode it is also implicit: peers monitor the local folder and the master's
event stream without any extra flag.

### Single Sync (one-shot)

In p2p Mode, the client **runs continuously by default**: it stays connected
and reacts to changes (continuous mode is implicit). To run a **single
sync cycle and exit**, pass `--once` (the universal one-shot selector):

```bash
HSYNC_P2P_KEY="shared-secret" hsync p2p -b mybucket -p ~/Sync --once
```

This compares local and remote files, syncs changes, and shuts down — useful
for cron jobs or manual "sync now" operations in a p2p group.

### Dry Run

Preview what would change without modifying files (a dry run never writes, so
no key is required):

```bash
hsync p2p -n -b mybucket -p ~/Sync
```

### Conflict Handling

The elected master is a **transport hub, not an authority**: it serves bytes and
relays events but never decides conflicts by role. Conflicts are resolved
causally on every peer, identically to Server Mode — content identity first,
then vector-clock dominance, then on true concurrency the later
`corrected_mtime` (ADR-0023), with SHA-256 only if that field is missing or
tied. The losing content is preserved as
`_conflicted_copy_<timestamp>_<cid>`, by whichever side lost (the reconciling
peer, or the upload arbitration on the master).

See [Conflict Resolution](conflict-resolution.md) for the full algorithm.

### Further Reading

- [p2p Mode Guide](p2p-mode.md) — detailed walkthrough
- [Configuration — p2p Mode](configuration-p2p.md) — all p2p-mode CLI options
- [Authentication — Client-Side](authentication.md) — auth flow diagrams (p2p with/without key)
- [p2p protocol spec](https://github.com/albertsdk/hsync-common/blob/main/docs/p2p-protocol.md) — authoritative election + security reference
