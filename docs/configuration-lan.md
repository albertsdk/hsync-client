# Configuration — LAN P2P Mode

LAN P2P configuration for peer-to-peer synchronization without a central server.

> For shared options (sync, compression, delta, logging), see [Configuration](configuration.md).
> For server connection options, see [Configuration — Server Mode](configuration-server.md).

## LAN Mode Options

| Option | Description | Default |
|---|---|---|
| `lan` subcommand | Enable LAN P2P mode (selected with `hsync lan`) | — (CLI only) |
| `--group` | LAN group name (only peers in the same group see each other) | — (all peers visible) |
| `--listen-port` | Port when acting as LAN master; `0` = pick a free port | `0` (auto) |
| `--force-master` | Force this device to be the master | `false` (CLI only) |
| `--never-master` | Prevent this device from ever becoming master | `false` |
| `--require-pairing` | Require out-of-band confirmation before trusting a new master | `false` |
| `--discovery-timeout` | Seconds to wait for LAN peer discovery (`0` = wait indefinitely) | `0` (indefinite) |

The shared secret is read from the `HSYNC_LAN_KEY` environment variable.
Parent processes (desktop GUI) may pass it on a pipe with the hidden
`--lan-key-fd` flag.

The `lan` subcommand and `--force-master` have no environment-variable equivalents.

## Master Election Controls

| Option | Description |
|---|---|
| `--force-master` | This device wins elections against non-force-master peers. Multiple force-master peers are resolved by the deterministic election algorithm. |
| `--never-master` | This device always stays a slave. If all peers are never-master, no master is elected and syncing stops. |

## File Stability & Upload

| Option | Description | Default |
|---|---|---|
| `--stability-delay` | Seconds a file must stay unchanged before upload. Already-idle files (mtime older than this) skip the wait. After a writer close, a 0.3s re-check is used instead. | `5` |
| `--max-wait-timeout` | Maximum seconds to wait for a file to stabilize | `120` |

## Advanced LAN Options

| Option | Description | Default |
|---|---|---|
| `--subdir` | Sync only files under this subdirectory | — (sync everything) |
| `--http-timeout` | HTTP request timeout in seconds | auto (or `30` with `--no-autotune`) |
| `--min-free-disk-space` | Minimum free disk space (MB) before syncing stops | `500` (`0` = disabled) |

## Environment Variables — LAN P2P Mode

| Variable | Description | Default |
|---|---|---|
| `HSYNC_LAN_GROUP` | LAN group name (alternative to `--group`) | — |
| `HSYNC_LAN_KEY` | Shared secret for peer authentication | — (hello handshake, unauthenticated) |
| `HSYNC_LAN_PORT` | LAN master port (`1`–`65535`); unset keeps CLI auto (`0`) | — |
| `HSYNC_NEVER_MASTER` | Never become master | `false` |
| `HSYNC_LAN_REQUIRE_PAIRING` | Require pairing confirmation | `false` |
| `HSYNC_DISCOVERY_TIMEOUT` | LAN peer discovery timeout in seconds (`0` = indefinitely) | `0` |
| `HSYNC_STABILITY_DELAY` | Seconds a file must stay unchanged before upload (already-idle files skip the wait) | `5` |
| `HSYNC_DELTA_MIN_FILE_SIZE_LAN` | Min file size for delta transfer in LAN mode | `1048576` (1 MB) |

`HSYNC_LAN_MODE` and `HSYNC_LAN_FORCE_MASTER` are **not** read. Enable LAN
with the `lan` subcommand; pin a master with `--force-master`.

## Environment files

The client does **not** load `./.env` by itself. Example:

```bash
export HSYNC_LAN_KEY="shared-secret"    # optional, recommended
hsync lan --env-file ./hsync.env -b documents -p "$HOME/Documents"
```

`--env-file` only applies recognised `HSYNC_*` keys (see the table above and
[Configuration](configuration.md)). The `lan` subcommand, `--force-master`,
bucket, and path remain CLI arguments.

## See also

- [LAN Mode Guide](lan-mode.md) — detailed walkthrough
- [Authentication — Client-Side](authentication.md) — hello handshake + auth diagrams
- [LAN Protocol spec](https://github.com/albertobasso/hsync-common/blob/main/docs/lan-protocol.md) — election + security reference
