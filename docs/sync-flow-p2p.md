# Sync Flow — p2p Mode

This document describes how the HSYNC client handles file synchronization in
peer-to-peer p2p mode.

> For Server Mode sync flow, see [Sync Flow — Server Mode](sync-flow-server.md).

## 1. Rendezvous & Master Election

1. **Announcement**: Each peer announces itself — via mDNS on the LAN (default),
   or by registering with the configured `rdv/1` rendezvous for a group spread
   across networks — advertising its CID, bucket, group, and capabilities.
2. **Peer Rendezvous**: `Peermanager` collects all visible peers in the same group.
3. **Master Election**: A deterministic algorithm selects the master:
   - `server_ready` (incumbent) beats newcomers
   - `prefer_master` beats ordinary peers
   - Bidirectional `sync_direction` preferred
   - Earliest `startup_time` wins
   - Lowest `cid` lexicographically breaks ties
4. **Role Assignment**: The winner becomes **master**, all others become **slaves**.

See [p2p protocol spec](https://github.com/albertsdk/hsync-common/blob/main/docs/p2p-protocol.md) for the complete
election algorithm.

## 2. Master WebSocket Hub

The master runs a WebSocket hub on the same TLS port as the REST data plane
(`/api/v1/buckets/{bucket}/events/{cid}`) and broadcasts file change events to
all connected slaves.

### Auth Flow (with P2P_KEY)

```text
Slave                              Master
  │── WSS connect ───────────────────▶│
  │◀── {"type": "auth_required"} ─────│
  │── {"type": "auth",                │
  │    "token": "<P2P_KEY>",          │
  │    "cid": "<CID>"} ──────────────▶│
  │◀── {"type": "auth", "ok"} ────────│
  │◀── file change events ────────────│
  │── upload/delete/rename (HTTPS) ──▶│
```

### Hello Handshake (without P2P_KEY)

```text
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

Without `P2P_KEY`, the declared CID is **unauthenticated** and cannot be used
for echo suppression — the master's own CID is used instead.

## 5. Slave Upload Cycle

1. **Watchdog Trigger**: File created/closed/modified in the slave's bucket.
2. **Stability Check**: The file must be stable (size + mtime unchanged).
3. **Upload**: File is POSTed to the master with SHA-256 verification.
4. **Broadcast**: Master distributes metadata to all other slaves.

A **rename** is not an upload in this cycle: the slave announces it with
`PATCH …/files?src=&dst=` and the master moves its own copy, so the file is
never re-sent. The same request is used by a periodic resync when the rename
happened while the slave was not watching (both sides advertise the same
SHA-256 and size, and the request carries the expected checksum). A peer that
learns about a rename afterwards as "delete the old path + download the new
one" applies it as a local move, so no peer downloads the file either. The
master's watcher records the vacated path's deletion marker and its version
store carries the clock to the new path — a rename is not a content edit.

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
