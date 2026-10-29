# Configuration — p2p Mode

p2p configuration for peer-to-peer synchronization without a central server.

> For shared options (sync, compression, delta, logging), see [Configuration](configuration.md).
> For server connection options, see [Configuration — Server Mode](configuration-server.md).

## p2p Mode Options

| Option | Description | Default |
|---|---|---|
| `p2p` subcommand | Enable p2p mode (selected with `hsync p2p`) | — (CLI only) |
| `--rendezvous-server` | Rendezvous-server endpoint for cross-network rendezvous: `HOST[:PORT]`, QUIC/UDP, no scheme (optional; omit for mDNS only). Repeatable, and each value may carry a comma-separated list; the client leases and queries every listed server and merges the peers | — |
| `--map-data-port` | Map the p2p data-plane TCP port with UPnP when a rendezvous server is used (opt-in) | `false` (UPnP off; enable explicitly) |
| `--rendezvous-server-fingerprint` | Pin a rendezvous server's certificate fingerprint (`sha256:<hex>` or bare hex); repeatable up to two per server for a rotation window; refused without `--rendezvous-server` | — (`ask`: confirm at the prompt on a terminal, fail closed without one) |
| `--rendezvous-server-tofu` | Trust a rendezvous server's certificate on first use; a later mismatch is refused | `false` |
| `--rendezvous-server-trust` | Rendezvous-server TLS trust policy: `ask` (prompt on a terminal and remember, fail closed without one), `accept-new`, `strict`, `none` | `ask` |
| `--group` | p2p group name (only peers in the same group see each other) | — (all peers visible) |
| `--listen-port` | Port when acting as p2p master; `0` = pick a free port | `0` (auto) |
| *(data relay)* | No client option: when no advertised candidate is reachable (both peers behind carrier-grade NAT) the client relays the data plane through the rendezvous automatically, if the node runs with `--relay` | — (last resort; see [Sync across networks](p2p-rendezvous.md#behind-carrier-grade-nat-mobile-hotspot-guest-network)) |
| `--prefer-master` | Prefer this device as the master | `false` (CLI only) |
| `--never-master` | Prevent this device from ever becoming master | `false` |
| `--p2p-trust` | P2P master trust policy: `ask` (confirm an unknown master on a terminal, fail closed without one), `accept-new` (pin the first sighting silently) or `strict` (require an explicit pin) | `ask` |
| `--no-peer-download` | Disable peer-assisted downloads for this run; overrides an enabled environment setting | `false` |
| `--allow-unauthenticated-writes` | Dangerously allow write-capable sync without a shared secret | `false` |
| `--peer-rendezvous-timeout` | Seconds to wait for p2p peer rendezvous (`0` = wait indefinitely) | `0` (indefinite) |

The shared secret comes from `HSYNC_P2P_PASSPHRASE` (at least 12 characters;
the client derives the 64-hex group key with scrypt) or from `HSYNC_P2P_KEY`
(a full key of at least 32 characters, used as-is). Never set both. With no
secret, reads and download-only operation remain available, but write-capable
sync is refused unless the dangerous CLI or environment opt-in is explicit.
Parent processes (desktop GUI) may pass it on a pipe with the hidden
`--p2p-passphrase-fd` (password) or `--p2p-key-fd` (raw key) flag.

The **raw key** (`HSYNC_P2P_KEY`) must be at least 32 characters: it is the only
entropy behind the `bucket_token`, so the client refuses to start with a shorter
value (the rendezvous enforces the same floor on its own key source). Generate one
with `openssl rand -hex 32` and share that exact value between the peers that must
trust each other. A user-typed `HSYNC_P2P_PASSPHRASE` only needs 12 characters
because the client expands it to that 32-byte key with scrypt.

With a rendezvous (see [Sync across networks](p2p-rendezvous.md)) the same key is also
the rendezvous credential: the client derives the `bucket_token`
(`HMAC-SHA256(key, "rdv/1" || bucket)[:32]`) it must present to a **protected
bucket** or a **private node**, so it has to match the operator's `bucket=<key>`
entry.

The `p2p` subcommand and `--prefer-master` have no environment-variable equivalents.

## Master Election Controls

| Option | Description |
|---|---|
| `--prefer-master` | This device is preferred in the election; a primary that is already serving keeps the role. When several peers prefer the role, the deterministic election algorithm breaks the tie. |
| `--never-master` | This device always stays a slave. If all peers are never-master, no master is elected and syncing stops. |

`--p2p-trust` applies to the elected master only (ADR-0040): `ask` (the
default) confirms a first-seen master certificate on a terminal and fails closed
without one, `accept-new` pins it silently, and `strict` requires an explicit pin
before any connection. An unknown master is therefore never trusted implicitly;
see [FAQ — p2p Mode](faq-p2p.md#security--safety) for the out-of-band steps.
Peer seeders are selected through that master and receive their own persistent
`(device ID, bucket)` TOFU pin before the requester opens a connection.

## File Stability & Upload

| Option | Description | Default |
|---|---|---|
| `--stability-delay` | Seconds a file must stay unchanged before upload. Already-idle files (mtime older than this) skip the wait. After a writer close, a 0.3s re-check is used instead. | `5` |
| `--max-wait-timeout` | Maximum seconds one upload waits for a file to stop being written (the upload is deferred when exceeded) | `120` |

## Advanced p2p Options

| Option | Description | Default |
|---|---|---|
| `--subdir` | Sync only files under this subdirectory | — (sync everything) |
| `--http-timeout` | HTTP request timeout in seconds | auto (or `30` with `--no-autotune`) |
| `--min-free-disk-space` | Minimum free disk space before syncing stops, as a size with an explicit unit (`500MB`, `2GB`) | `500MB` (`0` = disabled) |

## Security opt-ins

| Option | Description | Default |
|---|---|---|
| `--allow-unauthenticated-writes` | Dangerous: allow p2p filesystem mutations with no shared key (`HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES`) | `false` |
| `--allow-unauthenticated-p2p` | Acknowledge a keyless p2p run: peers are unauthenticated and reads are open (`HSYNC_P2P_ALLOW_UNAUTHENTICATED`) | `false` |

Both must be explicit: a TTY or `--allow-deletions` never grants them.

## Environment Variables — p2p Mode

| Variable | Description | Default |
|---|---|---|
| `HSYNC_P2P_GROUP` | p2p group name (alternative to `--group`) | — |
| `HSYNC_P2P_PASSPHRASE` | Shared password (**at least 12 characters**); derived to the 64-hex group key with scrypt. The usual choice for end users. Mutually exclusive with `HSYNC_P2P_KEY` | — |
| `HSYNC_P2P_KEY` | Full raw group key (**at least 32 characters**), used as-is; with a rendezvous, also the key the `bucket_token` is derived from (must match the rendezvous's key for protected/private buckets) | — (reads only unless explicitly opted into unauthenticated writes) |
| `HSYNC_P2P_PEER_DOWNLOAD` | Ask the master for exact-hash sources and try eligible peers before master full-download fallback | `true` |
| `HSYNC_P2P_PEER_READ_CAP` | Maximum concurrent response bodies served by each continuous slave seeder (announced on the handshake so requesters stay within it) | `2` |
| `HSYNC_P2P_PEER_SOURCES_MAX` | Maximum unique peer devices attempted per file before master fallback | `3` |
| `HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES` | Dangerously allow p2p filesystem mutations with no shared key | `false` |
| `HSYNC_P2P_ALLOW_UNAUTHENTICATED` | Explicitly acknowledge running p2p without a shared key (peers unauthenticated, reads open) | `false` |
| `HSYNC_P2P_LISTEN_PORT` | p2p master port (`1`–`65535`); unset keeps CLI auto (`0`) | — |
| `HSYNC_RENDEZVOUS_SERVER_ENDPOINTS` | Ordered, comma-separated rendezvous-server endpoints for cross-network rendezvous (`host`, `host:port`, `[IPv6]`, `[IPv6]:port`; QUIC/UDP, no scheme). The client leases **and queries every reachable entry** and merges the peers. Unset = mDNS-only | — |
| `HSYNC_RENDEZVOUS_SERVER_EXTRA_PORTS` | Extra UDP ports tried on **each** endpoint, comma-separated and ordered (dial fallback); e.g. `53` so a client whose **egress firewall** blocks UDP 443 can still reach the same node on 53. Tried after the endpoint's own/default port | — |
| `HSYNC_RENDEZVOUS_SERVER_FINGERPRINTS` | Pins **per endpoint**: `ENDPOINT=FINGERPRINT[|FINGERPRINT]`, comma-separated (`sha256:<hex>` or bare hex; `|` separates the rotation window). A pin must name a configured endpoint; a bare fingerprint is accepted only with one endpoint. With several servers each keeps its own identity | — |
| `HSYNC_RENDEZVOUS_SERVER_TOFU` | Trust a rendezvous server's certificate on first use (`1` = on) | `false` |
| `HSYNC_RENDEZVOUS_SERVER_TRUST` | Rendezvous-server TLS trust policy: `ask` (prompt on a terminal and remember, fail closed without one), `accept-new`, `strict` (each configured server needs its pin), `none` | `ask` |
| `HSYNC_MAP_DATA_PORT` | Map the master TCP port with UPnP (`true`/`false`); off by default | `false` |
| `HSYNC_NEVER_MASTER` | Never become master | `false` |
| `HSYNC_DEVICE_DIR` | Device identity dir (device ID + p2p keypair + per-folder sync-state DBs `syncstate/<uuid>.db`, `syncstate/index.db`) | `$XDG_STATE_HOME/hsync/device` or `~/.local/state/hsync/device` |
| `HSYNC_LOCKS_DIR` | Instance-lock dir (`<sha256>.lock` per synced directory) | `$XDG_RUNTIME_DIR/hsync/locks`, else `<device-dir>/locks` |
| `HSYNC_P2P_PIN_FILE` | Pinned fingerprints of known p2p peers (TOFU) | `$HSYNC_STATE_DIR/p2p_known_peers.json`; `$XDG_STATE_HOME/hsync/…` or `~/.local/state/hsync/…` when the state dir is unset |
| `HSYNC_RENDEZVOUS_SERVER_TRUST_FILE` | Trusted rendezvous fingerprints (known-hosts store) | `$HSYNC_STATE_DIR/rendezvous_server_known_hosts.json`; the XDG/local-state path when the state dir is unset |
| `HSYNC_P2P_TRUST` | P2P master trust policy: `ask`, `accept-new` or `strict` | `ask` |
| `HSYNC_PEER_RENDEZVOUS_TIMEOUT` | p2p peer rendezvous timeout in seconds (`0` = indefinitely) | `0` |
| `HSYNC_HEARTBEAT_INTERVAL` | LAN mDNS re-announce / liveness-evaluation cadence in seconds (`1`–`60`) | `5` |
| `HSYNC_RENDEZVOUS_SERVER_REFRESH_INTERVAL` | `rdv/1` lease/query cadence in seconds (`1`–`300`): how often the client registers, refreshes and queries the rendezvous. Sized against the node's rate limits (spec refresh: 30 s) | `30` |
| `HSYNC_PEER_TIMEOUT` | Seconds without a peer's heartbeat before it is considered dead and excluded from election (`3`–`600`); must be ≥ 2× the heartbeat, and with a rendezvous configured the client raises it to at least 2× `HSYNC_RENDEZVOUS_SERVER_REFRESH_INTERVAL` (default 60 s) so a relayed peer is not expired between two random samples | `15` |
| `HSYNC_STABILITY_DELAY` | Seconds a file must stay unchanged before upload (already-idle files skip the wait) | `5` |
| `HSYNC_DELTA_MIN_FILE_SIZE_P2P` | Min file size for delta transfer in p2p mode | `1048576` (1 MB) |

`HSYNC_P2P_MODE` and `HSYNC_P2P_PREFER_MASTER` are **not** read. Enable p2p mode
with the `p2p` subcommand; prefer a primary with `--prefer-master`.

`--no-peer-download` is a final opt-out: when present, peer downloading stays
disabled even if `HSYNC_P2P_PEER_DOWNLOAD=true`. One-shot slaves may request
from peers, but they never advertise or run a seeder. Continuous non-dry-run
slaves serve only exact-hash `GET` downloads; all mutating routes are disabled.

## Environment files

The client does **not** load `./.env` by itself. Example:

```bash
# Usual choice: a shared password, identical on every peer (>=12 characters).
export HSYNC_P2P_PASSPHRASE="a-long-shared-password"
# Advanced: a full raw key (>=32 characters); rendezvouss also use this form.
# export HSYNC_P2P_KEY="$(openssl rand -hex 32)"
hsync p2p --env-file ./hsync.env -b documents -p "$HOME/Documents"
```

`--env-file` only applies recognised `HSYNC_*` keys (see the table above and
[Configuration](configuration.md)). The `p2p` subcommand, `--prefer-master`,
bucket, and path remain CLI arguments.

## See also

- [p2p Mode Guide](p2p-mode.md) — detailed walkthrough
- [Authentication — Client-Side](authentication.md) — hello handshake + auth diagrams
- [p2p protocol spec](https://github.com/albertsdk/hsync-common/blob/main/docs/p2p-protocol.md) — election + security reference
