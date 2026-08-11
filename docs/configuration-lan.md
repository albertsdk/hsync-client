# Configuration — LAN P2P Mode

LAN P2P configuration for peer-to-peer synchronization without a central server.

> For shared options (sync, compression, delta, logging), see [Configuration](configuration.md).
> For server connection options, see [Configuration — Server Mode](configuration-server.md).

## LAN Mode Options

| Option | Description | Default |
|---|---|---|
| `--lan-mode` | Enable LAN P2P mode | `false` |
| `--lan-group` | LAN group name (only peers in the same group see each other) | — (all peers visible) |
| `--lan-key` | Shared secret for LAN peer authentication | — (no auth, hello handshake) |
| `--lan-port` | Base port for LAN servers (HTTP + WS) | `9753` |
| `--force-master` | Force this device to be the master | `false` |
| `--never-master` | Prevent this device from ever becoming master | `false` |
| `--require-pairing` | Require out-of-band confirmation before trusting a new master | `false` |
| `--discovery-timeout` | Seconds to wait for LAN peer discovery | `30` |
| `--lan-persistent` / `--no-lan-persistent` | Keep waiting for peers (persistent) or exit after a single attempt when none are found | `true` (persistent) |

## Master Election Controls

| Option | Description |
|---|---|
| `--force-master` | This device wins elections against non-force-master peers. Multiple force-master peers are resolved by the deterministic election algorithm. |
| `--never-master` | This device always stays a slave. If all peers are never-master, no master is elected and syncing stops. |

## File Stability & Upload

| Option | Description | Default |
|---|---|---|
| `--stability-delay` | Seconds to wait before uploading a modified file | `5` |
| `--max-wait-timeout` | Maximum seconds to wait for a file to stabilize | `300` |

## Advanced LAN Options

| Option | Description | Default |
|---|---|---|
| `--filter-path` | Sync only files under this subdirectory | — (sync everything) |
| `--timeout` | HTTP request timeout in seconds | `30` |
| `--min-free-disk-space` | Minimum free disk space (MB) before syncing stops | `0` (disabled) |

## Environment Variables — LAN Mode

| Variable | Description | Default |
|---|---|---|
| `HSYNC_LAN_MODE` | Enable LAN P2P mode | `false` |
| `HSYNC_LAN_GROUP` | LAN group name | — |
| `HSYNC_LAN_KEY` | Shared secret for peer authentication | — |
| `HSYNC_LAN_PORT` | Base port for LAN servers | `9753` |
| `HSYNC_LAN_FORCE_MASTER` | Force master role | `false` |
| `HSYNC_LAN_NEVER_MASTER` | Never become master | `false` |
| `HSYNC_LAN_REQUIRE_PAIRING` | Require pairing confirmation | `false` |
| `HSYNC_LAN_PERSISTENT` | Keep waiting for peers instead of exiting when none are found (`--no-lan-persistent`) | `true` |
| `HSYNC_DISCOVERY_TIMEOUT` | LAN peer discovery timeout (seconds) | `30` |
| `HSYNC_STABILITY_DELAY` | File stability wait (seconds) | `5` |
| `HSYNC_MAX_WAIT_TIMEOUT` | Max file stability wait (seconds) | `300` |
| `HSYNC_DELTA_MIN_FILE_SIZE_LAN` | Min file size for delta transfer in LAN mode | `1048576` (1 MB) |

## Configuration via `.env` file

Create a `.env` file in your working directory:

```bash
# LAN P2P Mode
HSYNC_LAN_MODE=true
HSYNC_LAN_GROUP="mygroup"        # optional
HSYNC_LAN_KEY="shared-secret"    # optional, recommended
HSYNC_BUCKET="documents"
HSYNC_PATH="$HOME/Documents"
```

Then simply run `hsync` — the client reads `.env` automatically.

## See also

- [LAN Mode Guide](lan-mode.md) — detailed walkthrough
- [Authentication — Client-Side](authentication.md) — hello handshake + auth diagrams
- [LAN Protocol spec](https://github.com/albertobasso/hsync-common/blob/main/docs/lan-protocol.md) — election + security reference
