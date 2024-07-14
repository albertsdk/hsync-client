# Getting Started

This guide will help you get HSYNC up and running quickly.

## Requirements

For building:
- Python ≥3.12, <3.14
- Poetry (for dependency management)

For running:
- The compiled binary (no Python required)

## Quick Start

### 1. Build the Client

```bash
cd hsync-client

# Install dependencies (first time only)
poetry install

# Compile the binary
./compile.sh

# The binary will be in dist/<os>-<arch>/hsync
# e.g. dist/linux-amd64/hsync, dist/darwin-arm64/hsync, dist/windows-amd64/hsync.exe
```

> **Note**: `<platform>` in examples below is a shorthand for `<os>-<arch>`
> (e.g. `linux-amd64`, `darwin-arm64`, `windows-amd64`).
> The compile script detects the current platform automatically.

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

## Build Options

```bash
./compile.sh           # Build the binary
./compile.sh --clean   # Clean artifacts and rebuild
./compile.sh --help    # Show help
```

## Environment Configuration

The client provides environment file templates:

```bash
# Development (HTTP, localhost)
source dev.env

# Production (HTTPS, remote server)
source prod.env
```

See [Configuration](configuration.md) for details.
