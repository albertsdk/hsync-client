# Configuration — p2p Mode

p2p configuration for peer-to-peer synchronization without a central server.

> For shared options (sync, compression, delta, logging), see [Configuration](configuration.md).
> For server connection options, see [Configuration — Server Mode](configuration-server.md).

## p2p Mode Options

| Option | Description | Default |
|---|---|---|
| `p2p` subcommand | Enable p2p mode (selected with `hsync p2p`) | — (CLI only) |
| `BOOTNODE` (positional) | Bare bootnode hostname for cross-network discovery (optional; omit for mDNS only) | — |
| `--port` | UDP (QUIC) port of the remote bootnode; refused without `BOOTNODE` | `443` |
| `--no-upnp` | Do not map the master TCP port with UPnP when a bootnode is used | `false` (`UPnP` on with a bootnode) |
| `--bootnode-fingerprint` | Pin the bootnode certificate fingerprint (`sha256:<hex>` or bare hex); repeatable up to two for a rotation window; refused without `BOOTNODE` | — (fail-closed without a pin) |
| `--bootnode-tofu` | Trust the bootnode certificate on first use; a later mismatch is refused | `false` |
| `--group` | p2p group name (only peers in the same group see each other) | — (all peers visible) |
| `--listen-port` | Port when acting as p2p master; `0` = pick a free port | `0` (auto) |
| `--force-master` | Force this device to be the master | `false` (CLI only) |
| `--never-master` | Prevent this device from ever becoming master | `false` |
| `--require-pairing` | Require out-of-band confirmation before trusting a new master | `false` |
| `--no-peer-download` | Disable peer-assisted downloads for this run; overrides an enabled environment setting | `false` |
| `--allow-unauthenticated-writes` | Dangerously allow write-capable sync without `HSYNC_P2P_KEY` | `false` |
| `--discovery-timeout` | Seconds to wait for p2p peer discovery (`0` = wait indefinitely) | `0` (indefinite) |

The shared secret is read from the `HSYNC_P2P_KEY` environment variable. With
no key, reads and download-only operation remain available, but write-capable
sync is refused unless the dangerous CLI or environment opt-in is explicit.
Parent processes (desktop GUI) may pass it on a pipe with the hidden
`--p2p-key-fd` flag.

With a bootnode (see [Sync across networks](p2p-bootnode.md)) the same key is also
the bootnode credential: the client derives the `bucket_token`
(`HMAC-SHA256(key, "boot/1" || bucket)[:32]`) it must present to a **protected
bucket** or a **private node**, so it has to match the operator's `bucket=<key>`
entry.

The `p2p` subcommand and `--force-master` have no environment-variable equivalents.

## Master Election Controls

| Option | Description |
|---|---|
| `--force-master` | This device wins elections against non-force-master peers. Multiple force-master peers are resolved by the deterministic election algorithm. |
| `--never-master` | This device always stays a slave. If all peers are never-master, no master is elected and syncing stops. |

`--require-pairing` applies to the elected master only: a first-seen master
certificate is refused until its fingerprint is confirmed out of band and pinned
(see [FAQ — p2p Mode](faq-p2p.md#security--safety) for the steps). Peer
seeders are selected through that master and receive their own persistent
`(device ID, bucket)` TOFU pin before the requester opens a connection.

## File Stability & Upload

| Option | Description | Default |
|---|---|---|
| `--stability-delay` | Seconds a file must stay unchanged before upload. Already-idle files (mtime older than this) skip the wait. After a writer close, a 0.3s re-check is used instead. | `5` |
| `--max-wait-timeout` | Maximum seconds to wait for a file to stabilize | `120` |

## Advanced p2p Options

| Option | Description | Default |
|---|---|---|
| `--subdir` | Sync only files under this subdirectory | — (sync everything) |
| `--http-timeout` | HTTP request timeout in seconds | auto (or `30` with `--no-autotune`) |
| `--min-free-disk-space` | Minimum free disk space (MB) before syncing stops | `500` (`0` = disabled) |

## Environment Variables — p2p Mode

| Variable | Description | Default |
|---|---|---|
| `HSYNC_P2P_GROUP` | p2p group name (alternative to `--group`) | — |
| `HSYNC_P2P_KEY` | Shared secret for peer authentication; with a bootnode, also the group key the `bucket_token` is derived from (must match the bootnode's key for protected/private buckets) | — (reads only unless explicitly opted into unauthenticated writes) |
| `HSYNC_P2P_PEER_DOWNLOAD` | Ask the master for exact-hash sources and try eligible peers before master full-download fallback | `true` |
| `HSYNC_P2P_PEER_READ_CAP` | Maximum concurrent response bodies served by each continuous slave seeder (announced on the handshake so requesters stay within it) | `2` |
| `HSYNC_P2P_PEER_SOURCES_MAX` | Maximum unique peer devices attempted per file before master fallback | `3` |
| `HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES` | Dangerously allow p2p filesystem mutations with no shared key | `false` |
| `HSYNC_P2P_LISTEN_PORT` | p2p master port (`1`–`65535`); unset keeps CLI auto (`0`) | — |
| `HSYNC_BOOTNODE` | Bare bootnode host for cross-network discovery (QUIC/UDP, no scheme); unset = mDNS-only | — |
| `HSYNC_BOOTNODE_FINGERPRINT` | Comma-separated bootnode certificate fingerprint(s) to pin (up to two, `sha256:<hex>` or bare hex) | — |
| `HSYNC_BOOTNODE_TOFU` | Trust the bootnode certificate on first use (`1` = on) | `false` |
| `HSYNC_BOOTNODE_TRUST` | Bootnode TLS trust policy: `ask` (prompt on a terminal and remember, fail closed without one), `accept-new`, `strict`, `none` | `ask` |
| `HSYNC_UPNP_ENABLED` | Map the master TCP port with UPnP (`true`/`false`); unset = on only when a bootnode is used | — |
| `HSYNC_NEVER_MASTER` | Never become master | `false` |
| `HSYNC_P2P_REQUIRE_PAIRING` | Require pairing confirmation | `false` |
| `HSYNC_DISCOVERY_TIMEOUT` | p2p peer discovery timeout in seconds (`0` = indefinitely) | `0` |
| `HSYNC_HEARTBEAT_INTERVAL` | LAN mDNS re-announce / liveness-evaluation cadence in seconds (`1`–`60`) | `5` |
| `HSYNC_BOOTNODE_REFRESH_INTERVAL` | `boot/1` lease/query cadence in seconds (`1`–`300`): how often the client registers, refreshes and queries the bootnode. Sized against the node's rate limits (spec refresh: 30 s) | `30` |
| `HSYNC_PEER_TIMEOUT` | Seconds without a peer's heartbeat before it is considered dead and excluded from election (`3`–`600`); must be ≥ 2× the heartbeat | `15` |
| `HSYNC_STABILITY_DELAY` | Seconds a file must stay unchanged before upload (already-idle files skip the wait) | `5` |
| `HSYNC_DELTA_MIN_FILE_SIZE_P2P` | Min file size for delta transfer in p2p mode | `1048576` (1 MB) |

`HSYNC_P2P_MODE` and `HSYNC_P2P_FORCE_MASTER` are **not** read. Enable LAN
with the `p2p` subcommand; pin a master with `--force-master`.

`--no-peer-download` is a final opt-out: when present, peer downloading stays
disabled even if `HSYNC_P2P_PEER_DOWNLOAD=true`. One-shot slaves may request
from peers, but they never advertise or run a seeder. Continuous non-dry-run
slaves serve only exact-hash `GET` downloads; all mutating routes are disabled.

## Environment files

The client does **not** load `./.env` by itself. Example:

```bash
export HSYNC_P2P_KEY="shared-secret"    # required for writes (a keyless p2p is read-only)
hsync p2p --env-file ./hsync.env -b documents -p "$HOME/Documents"
```

`--env-file` only applies recognised `HSYNC_*` keys (see the table above and
[Configuration](configuration.md)). The `p2p` subcommand, `--force-master`,
bucket, and path remain CLI arguments.

## See also

- [p2p Mode Guide](p2p-mode.md) — detailed walkthrough
- [Authentication — Client-Side](authentication.md) — hello handshake + auth diagrams
- [LAN Protocol spec](https://github.com/albertsdk/hsync-common/blob/main/docs/p2p-protocol.md) — election + security reference
