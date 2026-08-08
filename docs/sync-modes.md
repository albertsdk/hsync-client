# Sync Modes

HSYNC has two orthogonal axes of operation: **topology** (where files live) and
**run mode** (how long the client runs).

## Topology

| Mode | Description | Guide |
|------|-------------|-------|
| **Server Mode** | Central server holds the canonical copy; all clients sync through it | [Server Mode](sync-modes-server.md) |
| **LAN P2P Mode** | Peer-to-peer over the local network; no server required | [LAN P2P Mode](sync-modes-lan.md) |

## Run mode

| Mode | Flag | Behavior |
|------|------|----------|
| **One-shot sync** | *(default — omit `-w`)* | Runs one full sync cycle (compare, upload, download, delete) and exits. Ideal for cron jobs, CI/CD pipelines, or manual "sync now" operations. |
| **Continuous watch** | `-w` / `--watch` | Monitors the local folder and keeps the WebSocket connection open. Changes are synced in real time as they happen. In LAN P2P Mode, watch mode is implicit and always active. |

> **One-shot is the default.** If you run `hsync` without `-w`, it syncs once
> and exits. Add `-w` to keep it running and reacting to changes.

## Choosing a combination

| Topology | One-shot example | Watch example |
|----------|-----------------|---------------|
| Server Mode | `hsync -s https://server:9753 -b mybucket -p ~/Sync` | `hsync -s https://server:9753 -b mybucket -p ~/Sync -w` |
| LAN P2P Mode | `hsync -b mybucket -p ~/Sync --lan-mode --no-lan-persistent` | `hsync -b mybucket -p ~/Sync --lan-mode` |

For LAN P2P Mode, the client is persistent by default (watch mode is implicit).
Use `--no-lan-persistent` to run a single sync cycle and exit.
