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

See [Sync Modes](sync-modes.md#operating-modes-server-vs-lan) for a full comparison.

### 3. Basic Usage

**Server Mode** — connect to a central HSYNC server:

```bash
# Single sync
hsync -s https://server.example.com:9753 -b mybucket -p /path/to/dir

# Continuous sync (watch mode)
hsync -s https://server.example.com:9753 -b mybucket -p /path/to/dir -w
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

### 4. Try it end-to-end (Server Mode, local)

The fastest way to see HSYNC in action is to run the server and a client on the
same machine. You need the [server](https://github.com/albertobasso/hsync-server)
running locally (Docker or Poetry) with `HSYNC_API_KEY` set.

```bash
# 1. Start the server (see the server docs for Docker/Poetry setup)
#    It listens on https://localhost:9753 with a self-signed cert.

# 2. Create two empty folders to sync
mkdir -p /tmp/hsync-a /tmp/hsync-b

# 3. Put a file in the first folder
echo "hello hsync" > /tmp/hsync-a/hello.txt

# 4. Sync folder A → server (one-shot, self-signed cert)
hsync -s https://localhost:9753 -b demo -p /tmp/hsync-a --insecure

# 5. Sync server → folder B (one-shot, self-signed cert)
hsync -s https://localhost:9753 -b demo -p /tmp/hsync-b --insecure

# 6. Verify the file arrived
cat /tmp/hsync-b/hello.txt   # → hello hsync
```

For continuous, real-time sync, add `-w` to both commands and edit files in
either folder — changes propagate within seconds.

### 5. Try it end-to-end (LAN Mode, local)

The fastest way to see LAN P2P sync in action is to run two clients on the same
machine (or two devices on the same network). No server, no API key — peers
discover each other via mDNS.

Open **two terminals** and run:

```bash
# 1. Create two empty folders to sync
mkdir -p /tmp/hsync-lan-a /tmp/hsync-lan-b

# 2. Put a file in the first folder
echo "hello hsync lan" > /tmp/hsync-lan-a/hello.txt

# 3. Terminal 1 — force this peer to be the master (it holds the canonical data,
#    so no deletion-risk warning is shown) and start LAN Mode
hsync -b demo-lan -p /tmp/hsync-lan-a --lan-mode --force-master -y

# 4. Terminal 2 — start a second peer; it discovers the master via mDNS and
#    downloads hello.txt
hsync -b demo-lan -p /tmp/hsync-lan-b --lan-mode -y

# 5. Verify the file arrived
cat /tmp/hsync-lan-b/hello.txt   # → hello hsync lan
```

Watch mode is **implicit** in LAN Mode: edit or create a file in either folder
and the change propagates to the other peer in real time. Press `Ctrl+C` in
both terminals to stop.

> **Why `--force-master` here?** The first peer holds the file, so it is the
> authoritative source. Forcing it to be master avoids the initial
> deletion-risk warning on a non-empty directory. On a real LAN with multiple
> devices, omit `--force-master` and let master election happen automatically
> (see [LAN Mode → Master Election](lan-mode.md#master-election)).

See [LAN Mode](lan-mode.md) for the P2P architecture, master election, the
deletion-risk warning, and the optional `HSYNC_LAN_KEY` shared secret.

### 6. What to expect in LAN Mode

The first time you run `--lan-mode` on a non-empty directory, the client prints
a **deletion-risk warning** and asks for confirmation: files that exist only on
this peer (and not on the elected master) are deleted during the initial sync.
This is intentional — it lets deletions made on the master reach offline peers.

- Use `-y` to skip the prompt in scripts/services.
- Use `--sync-direction upload-only` if you never want local files deleted.
- Use `--force-master` on the device that holds the canonical copy.

See [LAN Mode → Deletion Risk Warning](lan-mode.md#deletion-risk-warning) for
the full explanation.

## Environment Configuration

Set the required variables in your shell or export them in a `.env` file:

```bash
export HSYNC_SERVER="https://server.example.com:9753"
export HSYNC_API_KEY="your-api-key"
```

See [Configuration](configuration.md) for the full list of environment variables.
