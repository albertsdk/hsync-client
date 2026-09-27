# Authentication — Client-Side

This document describes how the **HSYNC Client** authenticates to a remote peer
in both **Server Mode** and **p2p Mode**. For the shared contracts (CID format,
message schema, protocol versioning), see
[`hsync-common/docs/authentication.md`](https://github.com/albertsdk/hsync-common/blob/main/docs/authentication.md).
For the server-side perspective in Server Mode, see
[`hsync-server/docs/authentication.md`](https://github.com/albertsdk/hsync-server/blob/main/docs/authentication.md).

---

## 1. Server Mode — Client to Central Server

The client connects to a central HSYNC Server over WSS, presents an API key, and
receives real-time file change events.

### Diagram

```
HSYNC Client                        HSYNC Server
  │                                       │
  │── WSS connect ───────────────────────▶│
  │     /api/v1/buckets/{bucket}/events/{cid} │
  │                                       │
  │── {"type": "auth",                    │
  │    "token": "<HSYNC_API_KEY>",        │
  │    "app_version": "1.2.3",            │
  │    "protocol_version": "ws/1",        │
  │    "capabilities": ["compression",    │
  │                     "delta"]} ───────▶│
  │                                       │── validate protocol, CID, API key
  │                                       │
  │◀── {"type": "auth",                   │
  │     "status": "ok",                   │
  │     "capabilities": [...]} ───────────│
  │                                       │
  │◀── file change events ────────────────│
  │── upload/delete/rename (HTTP) ───────▶│
```

### Key points

- **Account token via `HSYNC_API_KEY`** environment variable, sent as the `token` field
  (the value is an `hs_<id>_<secret>` account token issued by the server)
- **CID is in the URL path** and in the auth frame — validated by the server
- **Protocol version** (`ws/1`) is mandatory; mismatch closes the connection
- **Capabilities** are negotiated: the server advertises `compression` and `delta`;
  the client adapts accordingly

### Source code

| Component | File |
|---|---|
| WebSocket connect + auth | `hsync_client/remote_websocket.py` |
| Auth frame builder | `hsync_common/protocol/handshake.py` |

---

## 2. p2p with `P2P_KEY` — Authenticated Master/Slave

When a shared secret (`P2P_KEY`) is configured, the master authenticates every
incoming slave connection. The slave must present the correct key and its CID.

### Diagram

```
Slave                                Master
  │                                       │
  │── WSS connect ───────────────────────▶│
  │                                       │
  │◀── {"type": "auth_required"} ─────────│
  │                                       │
  │── {"type": "auth",                    │
  │    "token": "<P2P_KEY>",              │
  │    "cid": "<CID>"} ─────────────v────▶│
  │                                       │── is_valid_cid(CID)?
  │                                       │   ├── ❌ → {"status": "failed",
  │                                       │   │          "reason": "invalid_cid"}
  │                                       │   │          → close
  │                                       │   └── ✅ → continue
  │                                       │── hmac.compare_digest(token, P2P_KEY)?
  │                                       │   ├── ❌ → {"status": "failed"}
  │                                       │   │          → close
  │                                       │   └── ✅ → {"status": "ok"}
  │                                       │
  │◀── {"type": "auth",                   │
  │     "status": "ok"} ──────────────────│
  │                                       │
  │◀── file change events ────────────────│  Log: (CID: <CID>)
  │── upload/delete/rename (HTTPS) ──────▶│  CID used for echo suppression
```

### Key points

- **HMAC comparison** (`hmac.compare_digest`) prevents timing attacks
- **CID is validated first** (before the token check) with `is_valid_cid()`;
  a malformed CID is rejected regardless of the token
- **Echo suppression** uses the authenticated CID: the master substitutes the
  slave's CID into broadcast events so the originator can discard its own echo
- **CID is registered** in `_authenticatedCids` (Python: `_clients` with `authenticated=True`;
  Android: `_authenticatedCids` map)

### Source code (Python)

| Component | File |
|---|---|
| Server-side auth | `hsync_client/p2p_websocket.py` → `P2PWebSocketServer._handle_client` |
| Client-side auth | `hsync_client/p2p_websocket.py` → `p2p_websocket_client` |
| CID validation | `hsync_common/models/event.py` → `is_valid_cid()`, `CID_PATTERN` |

### Source code (Android)

| Component | File |
|---|---|
| Server-side auth + hello | `P2PWebSocketServer.kt` → `performAuth()` |
| Client-side auth + hello | `WebSocketClient.kt` → `handleLanMessage()` |
| CID validation | `P2PWebSocketServer.kt` companion → `CID_PATTERN`, `isValidCid()` |

---

## 3. p2p without `P2P_KEY` — Unauthenticated Hello Handshake

When no shared secret is configured, the master still wants to know the slave's
CID for log attribution. A lightweight **hello handshake** lets the slave declare
its CID. The CID is **not cryptographically authenticated** — the log carries an
explicit `(unauthenticated)` qualifier.

A keyless p2p is **read-only for writes**: discovery, listings and downloads
keep working, but a write-capable sync is refused with `[AUTH REQUIRED]` unless
it runs `--sync-direction download-only` or the dangerous
`--allow-unauthenticated-writes` opt-in is explicit on both sides.

### Diagram

```
Slave                                Master
  │                                       │
  │── WSS connect ───────────────────────▶│
  │                                       │
  │◀── {"type": "hello"} ─────────────────│
  │                                       │
  │── {"type": "hello",                   │
  │    "cid": "<CID>"} ──────────────────▶│
  │                                       │── is_valid_cid(CID)?
  │                                       │   ├── ❌ → log warning, stay "unknown"
  │                                       │   └── ✅ → CID = declared CID
  │                                       │            Log: (CID: <CID>,
  │                                       │                  unauthenticated)
  │                                       │
  │◀── file change events ────────────────│  (echo suppression uses master CID)
  │── upload/delete/rename (HTTPS) ──────▶│
```

### Key points

- **No authentication**: the CID is whatever the slave declares; there is no
  shared secret to verify
- **CID is still validated** with `is_valid_cid()`: a malformed declaration is
  rejected (logged as a warning, CID stays `"unknown"`)
- **`(unauthenticated)` qualifier** in every log line: makes the security posture
  explicit — the operator knows the CID is self-declared
- **Echo suppression is disabled** for this connection: `consume_inbound_origin`
  returns `None` for unauthenticated entries, so the master's own CID is used in
  broadcast events
- **5-second timeout**: if the slave doesn't respond to `"hello"` in time, the
  master falls back to `CID: unknown` and the connection proceeds normally
- **Legacy compatibility**: a client that doesn't recognize `"hello"` (old client)
  will not respond; the master times out gracefully and logs `CID: unknown`

### Source code (Python)

| Component | File |
|---|---|
| Server-side hello | `hsync_client/p2p_websocket.py` → `_handle_client` (else branch) |
| Client-side hello response | `hsync_client/p2p_websocket.py` → `p2p_websocket_client` |
| Echo suppression gate | `hsync_client/p2p_websocket.py` → `consume_inbound_origin` |

### Source code (Android)

| Component | File |
|---|---|
| Server-side hello | `P2PWebSocketServer.kt` → `performAuth()` (lanKey empty branch) |
| Client-side hello response | `WebSocketClient.kt` → `handleLanMessage()` ("hello" branch) |
| CID validation | `P2PWebSocketServer.kt` companion → `CID_PATTERN`, `isValidCid()` |

---

## Configuration reference

| Variable | Mode | Purpose |
|---|---|---|
| `HSYNC_API_KEY` | Server | Account token (`hs_<id>_<secret>`) for central server authentication |
| `HSYNC_P2P_KEY` | p2p | Shared secret for p2p peer authentication — required by any write-capable run (a keyless p2p is read-only). With a bootnode it is also the group key: the client derives the `bucket_token` from it for protected buckets and private nodes |

The `cid` carried in the LAN auth frames is derived from the local **device
identity** — it is not configurable via environment.

When `HSYNC_P2P_KEY` is set and non-empty:
- The **master** sends `{"type": "auth_required"}` to incoming slaves
- The **slave** sends `{"type": "auth", "token": "<P2P_KEY>", "cid": "<CID>"}`

When `HSYNC_P2P_KEY` is empty or unset:
- The **master** sends `{"type": "hello"}` to incoming slaves
- The **slave** sends `{"type": "hello", "cid": "<CID>"}`
