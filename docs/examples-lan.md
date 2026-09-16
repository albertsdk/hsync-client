# Examples — LAN P2P Mode

> Back to [Examples](examples.md)

Real-world usage examples for the `hsync` binary in **LAN peer-to-peer** deployments.

## Conventions

All examples assume the binary has been built (`./compile.sh`) and the
`hsync` executable is on your `$PATH`. No server and no API key is needed —
peers discover each other automatically via mDNS on the local network.

> **Write policy:** a keyless LAN is **read-only**. Every write-capable run
> (the default bidirectional mode, and `upload-only`) must authorize writes with
> a shared key, so the examples below assume:
>
> ```bash
> export HSYNC_LAN_KEY="shared-secret"   # same value on every device
> ```
>
> `--sync-direction download-only` needs no key; the deliberate, dangerous
> `--allow-unauthenticated-writes` opt-in exists for a fully trusted LAN and is
> not used in these examples. See
> [LAN Mode → Security & Pairing](lan-mode.md#security--pairing).

> **Note — same-machine experiments:** the examples below assume *distinct
> devices* on the LAN, each with its own installation (and therefore its own
> device identity — see [LAN Mode → Device Identity](lan-mode.md#device-identity)).
> To simulate two peers on **one** machine, give each process its own device
> directory, e.g.:
>
> ```bash
> HSYNC_LAN_KEY="shared-secret" HSYNC_DEVICE_DIR=~/.local/state/hsync/device-alt hsync lan -b shared -p ~/Other
> ```
>
> Otherwise the two instances share one device ID and ignore each other during
> discovery.

## 2.1 Sync a folder between two devices on the same Wi-Fi

On device A (the master):
```bash
HSYNC_LAN_KEY="shared-secret" hsync lan -b shared -p ~/Shared
```

On device B (the slave):
```bash
HSYNC_LAN_KEY="shared-secret" hsync lan -b shared -p ~/Shared
```

Continuous mode is implicit in LAN Mode: changes appear in real time.

## 2.2 Sync photo library across devices without internet

```bash
HSYNC_LAN_KEY="shared-secret" hsync lan -b photos -p ~/Pictures
```

All devices on the same LAN with the same bucket name will sync photos directly.

## 2.3 Backup laptop to a local NAS / always-on PC

On the NAS (force it to be master):
```bash
HSYNC_LAN_KEY="shared-secret" hsync lan -b backup -p /mnt/data/backup --force-master
```

On the laptop (slave):
```bash
HSYNC_LAN_KEY="shared-secret" hsync lan -b backup -p ~/Backup --sync-direction upload-only
```

Upload-only ensures files are never deleted from the laptop.

## 2.4 Keep a peer in sync but never let it become master

```bash
HSYNC_LAN_KEY="shared-secret" hsync lan -b shared -p ~/Shared --never-master
```

Useful for devices that are intermittently connected or have low resources.

## 2.5 One-shot LAN sync (no continuous listening)

```bash
HSYNC_LAN_KEY="shared-secret" hsync lan -b shared -p ~/Shared --once
```

`--once` runs a single slave sync cycle and exits; the master role is
inherently continuous.

## 2.6 Read-only LAN slave (download-only)

```bash
hsync lan -b shared -p ~/Shared --sync-direction download-only
```

This peer receives changes but never uploads. Useful for read-only mirrors.
Because it never writes, it needs no `HSYNC_LAN_KEY`.

## 2.7 Multiple isolated groups on the same LAN

Group A:
```bash
HSYNC_LAN_KEY="shared-secret" hsync lan -b shared -p ~/Shared --group team-a
```

Group B:
```bash
HSYNC_LAN_KEY="shared-secret" hsync lan -b shared -p ~/Shared --group team-b
```

Devices in different groups are invisible to each other, even on the same Wi-Fi.

## 2.8 LAN with a fixed port (firewalled environments)

```bash
HSYNC_LAN_KEY="shared-secret" hsync lan -b shared -p ~/Shared --listen-port 8080
```

The client uses port 8080 for HTTPS and 8081 for WSS. Set firewall rules
accordingly.

## Authentication (shared key)

The key assumed in [Conventions](#conventions) is what authorizes writes:

```bash
HSYNC_LAN_KEY="shared-secret" hsync lan -b shared -p ~/Shared
```

With a `LAN_KEY`, the master validates every slave's identity and accepts
mutations. Without it the group is read-only: the lightweight **hello
handshake** still tells you which device made each listed change (with an
`(unauthenticated)` qualifier in logs), but write-capable runs are refused with
`[AUTH REQUIRED]` unless they are `download-only` or opt in with
`--allow-unauthenticated-writes`.

See [Authentication — Client-Side](authentication.md) and
[LAN Mode → Security & Pairing](lan-mode.md#security--pairing) for details.

## See also

- [LAN Mode Guide](lan-mode.md) — detailed walkthrough
- [Configuration — LAN P2P Mode](configuration-lan.md) — all LAN-mode options
- [Authentication — Client-Side](authentication.md) — hello handshake + auth diagrams
