# Examples — LAN P2P Mode

> Back to [Examples](examples.md)

Real-world usage examples for the `hsync` binary in **LAN peer-to-peer** deployments.

## Conventions

All examples assume the binary has been built (`./compile.sh`) and the
`hsync` executable is on your `$PATH`. No server or API key is needed — peers
discover each other automatically via mDNS on the local network.

## 2.1 Sync a folder between two devices on the same Wi-Fi

On device A (the master):
```bash
hsync -b shared -p ~/Shared --lan-mode
```

On device B (the slave):
```bash
hsync -b shared -p ~/Shared --lan-mode
```

Watch mode is implicit in LAN Mode: changes appear in real time.

## 2.2 Sync photo library across devices without internet

```bash
hsync -b photos -p ~/Pictures --lan-mode
```

All devices on the same LAN with the same bucket name will sync photos directly.

## 2.3 Backup laptop to a local NAS / always-on PC

On the NAS (force it to be master):
```bash
hsync -b backup -p /mnt/data/backup --lan-mode --force-master
```

On the laptop (slave):
```bash
hsync -b backup -p ~/Backup --lan-mode --sync-direction upload-only
```

Upload-only ensures files are never deleted from the laptop.

## 2.4 Keep a peer in sync but never let it become master

```bash
hsync -b shared -p ~/Shared --lan-mode --never-master
```

Useful for devices that are intermittently connected or have low resources.

## 2.5 One-shot LAN sync (no continuous listening)

```bash
hsync -b shared -p ~/Shared --lan-mode
```

Without `-w` (watch is implicit in LAN), the client syncs once and exits.

## 2.6 Read-only LAN slave (download-only)

```bash
hsync -b shared -p ~/Shared --lan-mode --sync-direction download-only
```

This peer receives changes but never uploads. Useful for read-only mirrors.

## 2.7 Multiple isolated groups on the same LAN

Group A:
```bash
hsync -b shared -p ~/Shared --lan-mode --lan-group team-a
```

Group B:
```bash
hsync -b shared -p ~/Shared --lan-mode --lan-group team-b
```

Devices in different groups are invisible to each other, even on the same Wi-Fi.

## 2.8 LAN with a fixed port (firewalled environments)

```bash
hsync -b shared -p ~/Shared --lan-mode --lan-port 8080
```

The client uses port 8080 for HTTPS and 8081 for WSS. Set firewall rules
accordingly.

## Authentication (optional)

Add a shared `LAN_KEY` for authenticated sync:

```bash
HSYNC_LAN_KEY="shared-secret" hsync -b shared -p ~/Shared --lan-mode
```

With `LAN_KEY`, the master validates every slave's identity. Without it, the
lightweight **hello handshake** lets you see which device made each change
(with an `(unauthenticated)` qualifier in logs).

See [Authentication — Client-Side](authentication.md) for details.

## See also

- [LAN Mode Guide](lan-mode.md) — detailed walkthrough
- [Configuration — LAN P2P Mode](configuration-lan.md) — all LAN-mode options
- [Authentication — Client-Side](authentication.md) — hello handshake + auth diagrams
