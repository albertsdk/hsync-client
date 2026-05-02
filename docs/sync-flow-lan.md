# Sync Flow — LAN P2P Mode

This document describes how the HSYNC client handles file synchronization in
peer-to-peer LAN mode.

> For Server Mode sync flow, see [Sync Flow — Server Mode](sync-flow-server.md).

## 1. Discovery & Master Election

1. **mDNS Announcement**: Each peer announces itself on the LAN via mDNS,
   advertising its CID, bucket, group, and capabilities.
2. **Peer Discovery**: `Peermanager` collects all visible peers in the same group.
3. **Master Election**: A deterministic algorithm selects the master:
   - `server_ready` (incumbent) beats newcomers
   - `force_master` beats ordinary peers
   - Bidirectional `sync_direction` preferred
   - Earliest `startup_time` wins
   - Lowest `cid` lexicographically breaks ties
4. **Role Assignment**: The winner becomes **master**, all others become **slaves**.

See [LAN Protocol spec](https://github.com/albertosdk/hsync-common/blob/main/docs/lan-protocol.md) for the complete
election algorithm.

## 2. Master WebSocket Hub

The master runs a WebSocket server that broadcasts file change events to all
connected slaves.

### Auth Flow (with LAN_KEY)

```
Slave                              Master
  │── WSS connect ───────────────────▶│
  │◀── {"type": "auth_required"} ─────│
  │── {"type": "auth",                │
  │    "token": "<LAN_KEY>",          │
  │    "cid": "<CID>"} ──────────────▶│
  │◀── {"type": "auth", "ok"} ────────│
  │◀── file change events ────────────│
  │── upload/delete/rename (HTTPS) ──▶│
```

### Hello Handshake (without LAN_KEY)

```
Slave                              Master
  │── WSS connect ───────────────────▶│
  │◀── {"type": "hello"} ─────────────│
  │── {"type": "hello",               │
  │    "cid": "<CID>"} ──────────────▶│
  │◀── file change events ────────────│  (CID: <CID>, unauthenticated)
  │── upload/delete/rename (HTTPS) ──▶│
```

## 3. Event Propagation (Asymmetric)

- **Master → Slave (metadata push)**: The master watches its local bucket. When
  a file is closed, it broadcasts a `modified` event with metadata (path, size,
  SHA-256). Slaves **pull** the bytes over HTTPS and verify against the hash.
- **Slave → Master (byte push)**: A slave uploads bytes to the master over
  HTTPS. The master re-broadcasts the metadata to all other slaves. Before each
  upload, a **file stability guard** prevents uploading half-written files.

## 4. Echo Suppression

To prevent infinite loops where a master re-broadcasts an event the slave just
uploaded:

1. The slave sends its CID with every HTTP request (`?cid=`)
2. The master marks the resulting filesystem event with the originator's CID
3. When broadcasting, the CID is substituted into the event
4. The originator recognizes its own CID and discards the echo

Without `LAN_KEY`, the declared CID is **unauthenticated** and cannot be used
for echo suppression — the master's own CID is used instead.

## 5. Slave Upload Cycle

1. **Watchdog Trigger**: File created/closed/modified in the slave's bucket.
2. **Stability Check**: The file must be stable (size + mtime unchanged).
3. **Upload**: File is POSTed to the master with SHA-256 verification.
4. **Broadcast**: Master distributes metadata to all other slaves.

## 6. Periodic Resync

As a safety net against missed inotify events, the slave runs a full resync at
the configured `HSYNC_RESYNC` interval.

## 7. Conflict Handling

The master is a **transport hub, not an authority**: it serves bytes and
relays events but never decides conflicts by role. Every peer applies the same
causal vector-clock matrix (content identity → causal dominance → later
`corrected_mtime`, SHA-256 if missing or tied). On a true concurrent divergence, the losing content is
preserved as `_conflicted_copy_<ts>_<cid>` — by the uploader if its upload loses
the arbitration, or by the reconciling peer if its local content loses the
tie-break.

See [Conflict Resolution](conflict-resolution.md) for details.
