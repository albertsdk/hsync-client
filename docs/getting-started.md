# Getting Started

This page helps you pick the right path and points to the complete walkthrough
for each mode. No procedures are embedded here — follow exactly one link and
copy the commands from that page.

## Requirements

To **run** HSYNC you only need the compiled binary — no Python runtime is
required. To **build** it from source, see the prerequisites in
[Installation → Build from source](installation.md#build-from-source).

## Choose an operating mode

HSYNC runs in two mutually exclusive modes, selected per invocation:

| Mode | Topology | How to enable | Best for | Walkthrough |
|------|----------|---------------|----------|-------------|
| **LAN Mode** | Peer-to-peer (no server) | `hsync lan` | Devices on the same network, no server | [Getting Started — LAN P2P Mode](getting-started-lan.md) |
| **Server Mode** | Client ↔ central server | `hsync server -s URL` (+ `HSYNC_API_KEY`) | Remote / internet sync, multiple sites | [Getting Started — Server Mode](getting-started-server.md) |

See [Sync Modes](sync-modes.md) for a full comparison of the two topologies.

## Key terms

A few concepts appear throughout the docs; here is a one-line reference:

| Term | Meaning |
|------|---------|
| **Bucket** | The unit of synchronization: a named set of files that stays in sync across devices. Selected with `-b/--bucket`, mapped to a local directory with `-p/--path`. |
| **Server Mode** | Topology where a central **server** relays sync and keeps a copy of the content; each device runs a **client** that uploads/downloads through it. Enabled with `hsync server`. |
| **LAN Mode** | Serverless topology where peers discover each other via mDNS and elect a **master** that coordinates the sync group; all other peers are **slaves**. Enabled with `hsync lan`. |
| **Master / Slave** | LAN Mode roles: the master serves files over HTTPS/WSS and coordinates sync; slaves connect to the master. The master is elected automatically (see [LAN Mode](lan-mode.md#master-election)). |
| **Continuous mode** | Continuous real-time sync, the default in both modes (implicit in LAN Mode). A periodic full resync acts as a safety net for missed events. |
| **Folder marker** | Hidden `.hsyncfolder` file at the bucket root. Identifies the folder so sync state stored outside the bucket can follow a rename or move. Safe to ignore. |

## Next steps

- [Configuration](configuration.md) — all shared CLI options and environment variables
- [Server Mode](server-mode.md) — how Server Mode works (concepts)
- [LAN Mode](lan-mode.md) — how LAN Mode works (concepts)
- [Examples](examples.md) — practical usage recipes
- [FAQ](faq.md) — common questions
