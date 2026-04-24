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
| `--no-peer-download` | Disable peer-assisted downloads for this run; overrides an enabled environment setting | `false` |
| `--allow-unauthenticated-writes` | Dangerously allow write-capable sync without `HSYNC_LAN_KEY` | `false` |
| `--discovery-timeout` | Seconds to wait for LAN peer discovery (`0` = wait indefinitely) | `0` (indefinite) |

The shared secret is read from the `HSYNC_LAN_KEY` environment variable. With
no key, reads and download-only operation remain available, but write-capable
sync is refused unless the dangerous CLI or environment opt-in is explicit.
Parent processes (desktop GUI) may pass it on a pipe with the hidden
`--lan-key-fd` flag.

The `lan` subcommand and `--force-master` have no environment-variable equivalents.

## Master Election Controls

| Option | Description |
|---|---|
| `--force-master` | This device wins elections against non-force-master peers. Multiple force-master peers are resolved by the deterministic election algorithm. |
| `--never-master` | This device always stays a slave. If all peers are never-master, no master is elected and syncing stops. |

`--require-pairing` applies to the elected master only: a first-seen master
certificate is refused until its fingerprint is confirmed out of band and pinned
(see [FAQ — LAN P2P Mode](faq-lan.md#security--safety) for the steps). Peer
seeders are selected through that master and receive their own persistent
`(device ID, bucket)` TOFU pin before the requester opens a connection.

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
| `HSYNC_LAN_KEY` | Shared secret for peer authentication | — (reads only unless explicitly opted into unauthenticated writes) |
| `HSYNC_LAN_PEER_DOWNLOAD` | Ask the master for exact-hash sources and try eligible peers before master full-download fallback | `true` |
| `HSYNC_LAN_PEER_DOWNLOAD_MIN_SIZE` | Minimum listed file size eligible for peer-assisted download (`0` permits all sizes) | `1048576` (1 MB) |
| `HSYNC_LAN_PEER_READ_CAP` | Maximum concurrent response bodies served by each continuous slave seeder | `2` |
| `HSYNC_LAN_PEER_SOURCES_MAX` | Maximum unique peer devices attempted per file before master fallback | `3` |
| `HSYNC_LAN_ALLOW_UNAUTHENTICATED_WRITES` | Dangerously allow LAN filesystem mutations with no shared key | `false` |
| `HSYNC_LAN_PORT` | LAN master port (`1`–`65535`); unset keeps CLI auto (`0`) | — |
| `HSYNC_NEVER_MASTER` | Never become master | `false` |
| `HSYNC_LAN_REQUIRE_PAIRING` | Require pairing confirmation | `false` |
| `HSYNC_DISCOVERY_TIMEOUT` | LAN peer discovery timeout in seconds (`0` = indefinitely) | `0` |
| `HSYNC_HEARTBEAT_INTERVAL` | LAN mDNS re-announce / liveness-evaluation cadence in seconds (`1`–`60`) | `5` |
| `HSYNC_PEER_TIMEOUT` | Seconds without a peer's heartbeat before it is considered dead and excluded from election (`3`–`600`); must be ≥ 2× the heartbeat | `15` |
| `HSYNC_STABILITY_DELAY` | Seconds a file must stay unchanged before upload (already-idle files skip the wait) | `5` |
| `HSYNC_DELTA_MIN_FILE_SIZE_LAN` | Min file size for delta transfer in LAN mode | `1048576` (1 MB) |

`HSYNC_LAN_MODE` and `HSYNC_LAN_FORCE_MASTER` are **not** read. Enable LAN
with the `lan` subcommand; pin a master with `--force-master`.

`--no-peer-download` is a final opt-out: when present, peer downloading stays
disabled even if `HSYNC_LAN_PEER_DOWNLOAD=true`. One-shot slaves may request
from peers, but they never advertise or run a seeder. Continuous non-dry-run
slaves serve only exact-hash `GET` downloads; all mutating routes are disabled.

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
