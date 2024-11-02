# Getting Started

This guide will help you get HSYNC up and running quickly.

> **Already have a binary?** If you've installed a prebuilt binary, `.deb`, or
> WD OS5 `.bin` (see [Installation](installation.md)), jump straight to
> [Choose an Operating Mode](#2-choose-an-operating-mode).

## Requirements

To **run** HSYNC you only need the compiled binary — no Python runtime is
required. To **build** it from source, see the prerequisites in
[Installation → Build from source](installation.md#build-from-source).

## Quick Start

### 1. Get the Client

Download a prebuilt binary (recommended) or build it from source — both paths are
covered in [Installation](installation.md). The rest of this guide assumes you
have a working `hsync` binary.

> **Note**: `<platform>` in the examples below is a shorthand for `<os>-<arch>`
> (e.g. `linux-amd64`, `darwin-arm64`, `windows-amd64`).

### 2. Choose an Operating Mode

HSYNC can run in two mutually exclusive modes, selected per invocation:

| Mode | Topology | How to enable | Best for |
|------|----------|---------------|----------|
| **Server Mode** | Client ↔ central server | `-s/--server` (+ `HSYNC_API_KEY`) | Remote / internet sync, multiple sites |
| **LAN Mode** | Peer-to-peer (no server) | `--lan-mode` | Devices on the same network, no server |

See [Sync Modes](sync-modes.md#operating-modes-server-vs-lan) for a full comparison.

### 3. Basic Usage

**Server Mode** — connect to a central HSYNC server:

```bash
# Single sync
./dist/<platform>/hsync -s http://localhost:8765 -b mybucket -p /path/to/dir

# Continuous sync (watch mode)
./dist/<platform>/hsync -s http://localhost:8765 -b mybucket -p /path/to/dir -w
```

**LAN Mode** — peer-to-peer, no server required (run on each device):

```bash
# Peers discover each other via mDNS; watch mode is implicit
./dist/<platform>/hsync -b mybucket -p /path/to/dir --lan-mode
```

See [LAN Mode](lan-mode.md) for the P2P architecture and master election.

## Environment Configuration

The client provides environment file templates:

```bash
# Development (HTTP, localhost)
source dev.env

# Production (HTTPS, remote server)
source prod.env
```

See [Configuration](configuration.md) for details.
