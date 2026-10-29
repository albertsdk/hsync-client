# Getting Started — Debian/Ubuntu package

Native packages are provided for `amd64`, `arm64` and `armhf`. The package
installs the `hsync` command system-wide and ships a systemd unit that can start
it at boot.

## Prerequisites

- <img src="assets/os/debian.svg" alt="Debian" width="16" height="16"> <img src="assets/os/ubuntu.svg" alt="Ubuntu" width="16" height="16"> Debian or Ubuntu, `amd64`, `arm64` or `armhf`
- A root shell or `sudo`

## Install

```bash
curl -fLO "https://github.com/albertsdk/hsync-client/releases/latest/download/hsync-client-amd64.deb"
sudo dpkg -i hsync-client-amd64.deb
hsync --help
```

Replace `amd64` with your architecture (`arm64` / `armhf`). The URL always points
to the **latest stable** release; to pin a version, use
`https://github.com/albertsdk/hsync-client/releases/download/vX.Y.Z/hsync-client_<version>_<arch>.deb`
(see [Installation](installation.md) for the exact names).

## Run as a boot service (systemd)

The `.deb` ships a systemd unit that starts `hsync` automatically at boot.
Configuration is read from `/etc/hsync/hsync.conf` (an environment file loaded
by the unit). By default the service runs in **p2p mode** with continuous watch
as the `_hsync` system user.

> ⚠️ The service runs non-interactively (`--allow-deletions`) with bidirectional
> sync, so a shared secret (`HSYNC_P2P_PASSPHRASE` or the raw `HSYNC_P2P_KEY`)
> **must be set** in `/etc/hsync/hsync.conf` before
> enabling it — otherwise the client aborts with `[AUTH REQUIRED]` (a keyless
> read-only service must instead set `HSYNC_P2P_ALLOW_UNAUTHENTICATED=true`).
> All peers in the same p2p
> group must share the same key. The default config also sets
> `HSYNC_DEVICE_DIR=/var/lib/hsync/device`,
> `HSYNC_LOCKS_DIR=/var/lib/hsync/locks`,
> `HSYNC_P2P_PIN_FILE=/var/lib/hsync/p2p_known_peers.json` and
> `HSYNC_RENDEZVOUS_SERVER_TRUST_FILE=/var/lib/hsync/rendezvous_server_known_hosts.json`
> because the `_hsync` system user has no home directory.

```bash
# 1. Edit the configuration (bucket, path, p2p key, …)
sudo nano /etc/hsync/hsync.conf

# 2. Enable and start the service at boot
sudo systemctl enable --now hsync.service

# 3. Check status / logs
sudo systemctl status hsync.service
sudo tail -f /var/log/hsync/hsync.log
```

## Next

- Custom paths, ACLs, `ProtectHome` and running as another user:
  [Debian Package Guide](debian-package.md).
- Every CLI option and environment variable: [Configuration](configuration.md).
- Prefer no systemd? [Installer script](getting-started-script.md) or
  [standalone binary](getting-started-binary.md).
