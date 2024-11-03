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
hsync -s https://server.example.com:8765 -b mybucket -p /path/to/dir

# Continuous sync (watch mode)
hsync -s https://server.example.com:8765 -b mybucket -p /path/to/dir -w
```

> For local development with a self-signed certificate, add `--insecure` and
> use `https://` (the client refuses to send API keys over plain HTTP).
> See [SSL/TLS](configuration.md#ssltls-httpswss) for details.

**LAN Mode** — peer-to-peer, no server required (run on each device):

```bash
# Peers discover each other via mDNS; watch mode is implicit
hsync -b mybucket -p /path/to/dir --lan-mode
```

See [LAN Mode](lan-mode.md) for the P2P architecture and master election.

## Environment Configuration

Set the required variables in your shell or export them in a `.env` file:

```bash
export HSYNC_SERVER="https://server.example.com:8765"
export HSYNC_API_KEY="your-api-key"
```

See [Configuration](configuration.md) for the full list of environment variables.
