# Synchronization Modes

HSYNC supports different synchronization modes to fit various use cases.

## Operating Modes: Server vs LAN

Before choosing *how often* and in *which direction* to sync, you choose the
**operating mode** — the topology the client runs in. The two modes are
mutually exclusive and selected per invocation.

| Aspect | Server Mode | LAN Mode (P2P) |
|--------|-------------|----------------|
| Topology | Clients connect to a central server | Peers connect directly to each other |
| Enable with | `-s/--server` | `--lan-mode` |
| Server required | Yes | No |
| Authentication | `HSYNC_API_KEY` | `HSYNC_LAN_KEY` (optional) |
| Discovery | Explicit server URL | Automatic via mDNS/Bonjour |
| Watch mode | Opt-in (`-w`) | Implicit |
| Works offline / no internet | No (needs reachable server) | Yes (same LAN) |
| Best for | Remote sync, multiple sites, always-on hub | Devices on the same network |

```bash
# Server Mode — central hub
./dist/<platform>/hsync -s http://localhost:8765 -b mybucket -p /path/to/dir

# LAN Mode — peer-to-peer, no server (run on each device)
./dist/<platform>/hsync -b mybucket -p /path/to/dir --lan-mode
```

The sections below (single / watch / sync direction) apply to **both**
operating modes. See [LAN Mode](lan-mode.md) for the P2P architecture and
master election.

## Single Mode (Default)

Performs a synchronization and exits.

```bash
./dist/<platform>/hsync -s http://localhost:8765 -b mybucket -p /path/to/dir
```

## Watch Mode (`-w`)

Maintains a persistent connection and watches for changes:

- WebSocket connection with the server
- Receives remote events (add/modify/delete/rename)
- Monitors local directory with watchdog
- Propagates changes in real-time

```bash
./dist/<platform>/hsync -s http://localhost:8765 -b mybucket -p /path/to/dir -w
```

## Sync Direction

Controls the direction of file synchronization:

| Direction | Uploads | Downloads | Use Case |
|-----------|---------|-----------|----------|
| `bidirectional` (default) | ✅ | ✅ | Full two-way sync |
| `upload-only` | ✅ | ❌ | Push local to remote |
| `download-only` | ❌ | ✅ | Pull remote to local |

### Examples

```bash
# Upload-only: push local files to server
./dist/<platform>/hsync -s http://server:8765 -b backup -w --sync-direction upload-only

# Download-only: mirror remote content locally
./dist/<platform>/hsync -s http://server:8765 -b shared -w --sync-direction download-only
```

## LAN Mode (P2P)

Peer-to-peer synchronization without a centralized server:

```bash
# Basic LAN mode (HTTPS enabled)
./dist/<platform>/hsync -b photos -p ~/Pictures --lan-mode

# With authentication
HSYNC_LAN_KEY="mySecretKey" ./dist/<platform>/hsync -b photos -p ~/Pictures --lan-mode

# Disable HTTPS
./dist/<platform>/hsync -b photos -p ~/Pictures --lan-mode --no-lan-https
```

### How It Works

1. **Discovery** — Peers discover each other via mDNS (Bonjour/Avahi)
2. **Election** — Master is elected (priority: force_master > bidirectional > earliest startup > lowest CID)
3. **Sync** — Other peers sync files with master via HTTPS

See [LAN Mode](lan-mode.md) for detailed documentation.

## Watch Mode with Interval

Force full sync at regular intervals:

```bash
# Re-sync every 5 minutes
./dist/<platform>/hsync -s http://localhost:8765 -b mybucket -w -i 300
```

## Dry Run

Preview operations without making changes:

```bash
./dist/<platform>/hsync -s http://localhost:8765 -b mybucket -n
```
