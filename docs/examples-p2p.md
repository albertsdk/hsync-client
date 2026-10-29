# Examples — p2p Mode

> Back to [Examples](examples.md)

Real-world usage examples for the `hsync` binary in **p2p peer-to-peer** deployments.

## Conventions

All examples assume the binary has been built (`./compile.sh`) and the
`hsync` executable is on your `$PATH`. No server and no API key is needed —
peers discover each other automatically via mDNS on a local network, or by
registering with a rendezvous for a group spread across networks (see
[Sync across networks](p2p-rendezvous.md)).

> **Write policy:** a keyless p2p is **read-only**. Every write-capable run
> (the default bidirectional mode, and `upload-only`) must authorize writes with
> a shared key, so the examples below assume:
>
> ```bash
> export HSYNC_P2P_PASSPHRASE="a-long-shared-password"   # same value on every device
> ```
>
> `--sync-direction download-only` needs no key; the deliberate, dangerous
> `--allow-unauthenticated-writes` opt-in exists for a fully trusted LAN and is
> not used in these examples. See
> [p2p Mode → Security & Pairing](p2p-mode.md#security--pairing).

> **Note — same-machine experiments:** the examples below assume *distinct
> devices* on the LAN, each with its own installation (and therefore its own
> device identity — see [p2p Mode → Device Identity](p2p-mode.md#device-identity)).
> To simulate two peers on **one** machine, give each process its own device
> directory, e.g.:
>
> ```bash
> HSYNC_P2P_PASSPHRASE="a-long-shared-password" \
HSYNC_DEVICE_DIR=~/.local/state/hsync/device-alt \
  hsync p2p -b shared -p ~/Other
> ```
>
> Otherwise the two instances share one device ID and ignore each other during
> rendezvous.

## 2.1 Sync a folder between two devices on the same Wi-Fi

On device A (the master):
```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b shared -p ~/Shared
```

On device B (the slave):
```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b shared -p ~/Shared
```

Continuous mode is implicit in p2p Mode: changes appear in real time.

## 2.2 Sync photo library across devices without internet

```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b photos -p ~/Pictures
```

All devices on the same LAN with the same bucket name will sync photos directly.

## 2.3 Backup laptop to a local NAS / always-on PC

On the NAS (prefer it as master):
```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b backup -p /mnt/data/backup --prefer-master
```

On the laptop (slave):
```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b backup -p ~/Backup \
  --sync-direction upload-only --deletion-policy keep
```

`--sync-direction upload-only` pushes local changes without pulling remote ones;
`--deletion-policy keep` keeps the NAS from applying deletions, so a file removed on
the laptop stays on the backup.

## 2.4 Keep a peer in sync but never let it become master

```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b shared -p ~/Shared --never-master
```

Useful for devices that are intermittently connected or have low resources.

## 2.5 One-shot p2p sync (no continuous listening)

```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b shared -p ~/Shared --once
```

`--once` runs a single slave sync cycle and exits; the master role is
inherently continuous.

## 2.6 Read-only p2p slave (download-only)

```bash
hsync p2p -b shared -p ~/Shared --sync-direction download-only
```

This peer receives changes but never uploads. Useful for read-only mirrors.
Because it never writes, it needs no shared secret.

## 2.7 Multiple isolated groups on the same LAN

Group A:
```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b shared -p ~/Shared --group team-a
```

Group B:
```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b shared -p ~/Shared --group team-b
```

Devices in different groups are invisible to each other, even on the same Wi-Fi.

## 2.8 p2p with a fixed port (firewalled environments)

```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b shared -p ~/Shared --listen-port 8080
```

The client serves the REST API (HTTPS) and the real-time event stream (WSS) on
the same port 8080. Set firewall rules for that single port accordingly.

## 2.9 Cross-network group with a rendezvous

Devices on different networks (different sites, home ↔ office) meet through a
`rdv/1` rendezvous node instead of mDNS. Point every device of the group at the
same node and opt in to UPnP so this device can publish its master port when it
serves:

```bash
# Same command on every device of the group.
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b mybucket -p ~/Sync \
  --map-data-port boot.example.net
```

On a terminal the first contact is SSH-like: the client prints the node's
SHA-256 fingerprint and asks to confirm it once (answer `yes`), then remembers
it and reconnects silently. Read the fingerprint without trusting it at any
time with `hsync trust rendezvous-server fingerprint boot.example.net`.

A run with no terminal (cron, systemd, CI) cannot answer the prompt, so it must
state the trust decision explicitly. Accept the first sighting:

```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b mybucket -p ~/Sync \
  boot.example.net --rendezvous-server-tofu
```

Once the operator has published the stable `sha256:<hex>` fingerprint
(`hsync-rendezvous identity show`), pin it — the hardened form:

```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b mybucket -p ~/Sync \
  boot.example.net --rendezvous-server-fingerprint YOUR_RENDEZVOUS_FINGERPRINT
```

The `ask` trust prompt only appears on a terminal, so it is not enough for a
service run: pin the fingerprint (as above) or use `--rendezvous-server-trust accept-new`.
`--insecure` skips verification and is for loopback tests only.

A device that cannot map its port stays **slave-only** (it still syncs, it just
never serves). Verify the plan in the log: `Rendezvous: mDNS on the local network
+ rendezvous boot.example.net:443 (cross-network; UPnP on)`. Details, fingerprint
rotation and troubleshooting: [Sync across networks](p2p-rendezvous.md).

## Authentication (shared key)

The key assumed in [Conventions](#conventions) is what authorizes writes:

```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b shared -p ~/Shared
```

With a `P2P_KEY`, the master validates every slave's identity and accepts
mutations. Without it the group is read-only: the lightweight **hello
handshake** still tells you which device made each listed change (with an
`(unauthenticated)` qualifier in logs), but write-capable runs are refused with
`[AUTH REQUIRED]` unless they are `download-only` or opt in with
`--allow-unauthenticated-writes`.

See [Authentication — Client-Side](authentication.md) and
[p2p Mode → Security & Pairing](p2p-mode.md#security--pairing) for details.

## See also

- [p2p Mode Guide](p2p-mode.md) — detailed walkthrough
- [Configuration — p2p Mode](configuration-p2p.md) — all p2p-mode options
- [Authentication — Client-Side](authentication.md) — hello handshake + auth diagrams
