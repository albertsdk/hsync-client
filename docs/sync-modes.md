# Sync Modes

HSYNC has two orthogonal axes of operation: **topology** (where files live) and
**run mode** (how long the client runs).

## Topology

| Mode | Description | Guide |
|------|-------------|-------|
| **Server Mode** | Central server relays sync and keeps a copy of the synced content; all clients sync through it | [Server Mode](sync-modes-server.md) |
| **p2p Mode** | Peer-to-peer; no server required. Same local network (mDNS) or across networks through an optional bootnode | [p2p Mode](sync-modes-p2p.md) |

## Run mode

| Mode | Flag | Behavior |
|------|------|----------|
| **Continuous / daemon** | *(default)* | Runs continuously: keeps the WebSocket open, syncs changes in real time, and retries an unreachable server forever (like p2p mode). Never exits on its own. |
| **One-shot sync** | `--once` | Runs one full sync cycle (compare, upload, download, delete) and exits (exit non-zero if the server is unreachable after the startup retries). Ideal for cron jobs, CI/CD pipelines, or manual "sync now" operations. The exit code reports the outcome for scripts — see [Scheduling & Automation](scheduling.md). |

> **Continuous is the default.** If you run `hsync` without `--once`, the
> client stays active and reacts to changes, retrying the connection
> indefinitely. Add `--once` to run a single cycle and exit. In p2p Mode
> the client runs continuously by default too.

## Choosing a combination

| Topology | One-shot example | Continuous example |
|----------|-----------------|---------------|
| Server Mode | `hsync server --server-url server.example.com -b mybucket -p ~/Sync --once` | `hsync server --server-url server.example.com -b mybucket -p ~/Sync` |
| p2p Mode | `HSYNC_P2P_PASSPHRASE=a-long-shared-password hsync p2p -b mybucket -p ~/Sync --once` | `HSYNC_P2P_PASSPHRASE=a-long-shared-password hsync p2p -b mybucket -p ~/Sync` |

For p2p Mode, the client runs continuously by default. Use `--once` to
run a single sync cycle and exit in either mode.

> In p2p Mode the shared secret (`HSYNC_P2P_PASSPHRASE` or the raw
> `HSYNC_P2P_KEY`) authorizes writes: a keyless p2p is
> read-only, and a write-capable run without a key is refused
> (`[AUTH REQUIRED]`). Only `--sync-direction download-only` needs no key. See
> [p2p Mode → Security & Pairing](p2p-mode.md#security--pairing).

To schedule one-shot runs with cron or systemd timers (and to learn the
documented exit-code contract), see [Scheduling & Automation](scheduling.md).
