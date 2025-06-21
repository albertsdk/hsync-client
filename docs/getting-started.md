# Getting Started

This guide will help you get HSYNC up and running quickly.

> **Already have a binary?** If you've installed the client via the installer
> script, a prebuilt binary, `.deb`, or WD OS5 `.bin` (see
> [Installation](installation.md)), jump straight to
> [Choose an Operating Mode](#2-choose-an-operating-mode).

## Requirements

To **run** HSYNC you only need the compiled binary — no Python runtime is
required. To **build** it from source, see the prerequisites in
[Installation → Build from source](installation.md#build-from-source).

## Key terms

A few concepts appear throughout the docs; here is a one-line reference:

| Term | Meaning |
|------|---------|
| **Bucket** | The unit of synchronization: a named set of files that stays in sync across devices. Selected with `-b/--bucket`, mapped to a local directory with `-p/--path`. |
| **Server Mode** | Topology where a central **server** holds the canonical copy; each device runs a **client** that uploads/downloads from it. Enabled with `-s/--server`. |
| **LAN Mode** | Serverless topology where peers discover each other via mDNS and elect a **master** (source of truth); all other peers are **slaves** that sync with the master. Enabled with `--lan-mode`. |
| **Master / Slave** | LAN Mode roles: the master serves files over HTTPS/WSS and coordinates sync; slaves connect to the master. The master is elected automatically (see [LAN Mode](lan-mode.md#master-election)). |
| **Watch mode** | Continuous real-time sync enabled with `-w` (implicit in LAN Mode). A periodic full resync acts as a safety net for missed events. |
| **Side-cars** | Internal files (`.hsyncstate`, `.hsynctombstones`) the client creates at the bucket root to track sync state. Safe to ignore. |

## Quick Start

### 1. Get the Client

The quickest way on Linux and macOS is the
[installer script](installation.md#installer-script-recommended); for all other
platforms and formats see [Installation](installation.md). The rest of this
guide assumes you have a working `hsync` binary.

### 2. Choose an Operating Mode

HSYNC can run in two mutually exclusive modes, selected per invocation:

| Mode | Topology | How to enable | Best for |
|------|----------|---------------|----------|
| **Server Mode** | Client ↔ central server | `-s/--server` (+ `HSYNC_API_KEY`) | Remote / internet sync, multiple sites |
| **LAN Mode** | Peer-to-peer (no server) | `--lan-mode` | Devices on the same network, no server |

See [Sync Modes — Server Mode](sync-modes-server.md) or [Sync Modes — LAN P2P Mode](sync-modes-lan.md) for a full comparison.

### 3. Follow the guide for your mode

Choose your path:

- **[Getting Started — Server Mode](getting-started-server.md)** — connect to a central HSYNC server, with a step-by-step end-to-end walkthrough
- **[Getting Started — LAN P2P Mode](getting-started-lan.md)** — peer-to-peer on your local network, with a step-by-step end-to-end walkthrough and deletion-risk explanation

## Next Steps

- [Configuration](configuration.md) — all CLI options and environment variables
- [Server Mode Guide](server-mode.md) — detailed server-mode walkthrough
- [LAN Mode Guide](lan-mode.md) — detailed LAN P2P walkthrough
- [Examples](examples.md) — practical usage recipes
- [FAQ](faq.md) — common questions
