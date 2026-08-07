# Sync Flow — Server Mode

This document describes how the HSYNC client handles file synchronization when
connected to a central server.

> For LAN P2P sync flow, see [Sync Flow — LAN P2P Mode](sync-flow-lan.md).

## 1. Initial Connection

1. **Config Load**: The client reads environment variables (`HSYNC_SERVER`,
   `HSYNC_API_KEY`, `HSYNC_BUCKET`, `HSYNC_PATH`).
2. **Server Connection**: The client connects to the server over HTTPS/WSS.
3. **WebSocket Auth**: The client sends an auth frame with its API key, app
   version, protocol version, and capabilities. The server validates and replies.
4. **Initial Sync**: The client performs a full bidirectional sync — comparing
   local files with the server's file list and transferring differences.

## 2. Real-Time Watch Mode

Once the initial sync completes, the client enters watch mode:

1. **File Watcher (watchdog)**: Monitors the local bucket for create, modify,
   delete, rename events.
2. **WebSocket Listener**: Receives remote change events from the server.
3. **Event Loop**: Both sources feed into a unified event queue.

### Event Flow (Server Mode)

```
Local FS change (watchdog)
  │
  ├── file created/modified → upload to server (HTTP POST)
  ├── file deleted → notify server (HTTP DELETE)
  └── file renamed → notify server (HTTP PATCH)

Remote WS event (server)
  │
  ├── "added" → download file (HTTP GET)
  ├── "modified" → download file (HTTP GET)
  ├── "deleted" → delete local file/dir
  └── "renamed" → rename local file/dir
```

## 3. Conflict Handling

The server is the **authoritative source**. When a conflict occurs:

1. A three-way merge is attempted using the stored base state.
2. If the merge cannot resolve automatically, the **server version wins**.
3. The conflict is logged so the user can manually resolve it.

See [Conflict Resolution](conflict-resolution.md) for the full algorithm.

## 4. Connection Recovery

- If the WebSocket connection drops, the client retries with exponential backoff.
- On reconnect, a full sync is triggered to reconcile any missed events.
