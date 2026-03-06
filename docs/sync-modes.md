# Sync Modes

HSYNC has two orthogonal axes of operation: **topology** (where files live) and
**run mode** (how long the client runs).

## Topology

| Mode | Description | Guide |
|------|-------------|-------|
| **Server Mode** | Central server relays sync and keeps a copy of the synced content; all clients sync through it | [Server Mode](sync-modes-server.md) |
| **LAN P2P Mode** | Peer-to-peer over the local network; no server required | [LAN P2P Mode](sync-modes-lan.md) |

## Run mode

| Mode | Flag | Behavior |
|------|------|----------|
| **Continuous / daemon** | *(default)* | Runs continuously: keeps the WebSocket open, syncs changes in real time, and retries an unreachable server forever (like LAN mode). Never exits on its own. |
| **One-shot sync** | `--once` | Runs one full sync cycle (compare, upload, download, delete) and exits (exit non-zero if the server is unreachable after the startup retries). Ideal for cron jobs, CI/CD pipelines, or manual "sync now" operations. The exit code reports the outcome for scripts — see [Scheduling & Automation](scheduling.md). |

> **Continuous is the default.** If you run `hsync` without `--once`, the
> client stays active and reacts to changes, retrying the connection
> indefinitely. Add `--once` to run a single cycle and exit. In LAN P2P Mode
> the client runs continuously by default too.

## Choosing a combination

| Topology | One-shot example | Continuous example |
|----------|-----------------|---------------|
| Server Mode | `hsync server server.example.com -b mybucket -p ~/Sync --once` | `hsync server server.example.com -b mybucket -p ~/Sync` |
| LAN P2P Mode | `hsync lan -b mybucket -p ~/Sync --once` | `hsync lan -b mybucket -p ~/Sync` |

For LAN P2P Mode, the client runs continuously by default. Use `--once` to
run a single sync cycle and exit in either mode.

To schedule one-shot runs with cron or systemd timers (and to learn the
documented exit-code contract), see [Scheduling & Automation](scheduling.md).
