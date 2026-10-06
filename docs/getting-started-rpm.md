# Getting Started — RPM package (RHEL/Rocky/Alma/Fedora)

Native RPM packages are provided for `x86_64` and `aarch64`. The package
installs the `hsync` command system-wide and ships a systemd unit that can start
it at boot.

> The RPM binaries are built on a Rocky Linux 9 baseline (glibc 2.34), so they
> run on RHEL/Rocky/Alma 9+, recent Fedora, and any distro with glibc ≥ 2.34.
> `armv7`/`armhf` hosts are covered by the [Debian package](getting-started-deb.md)
> and the Raspberry Pi binaries instead.

## Prerequisites

- RHEL, Rocky Linux, AlmaLinux (9+) or Fedora, on `x86_64` or `aarch64`
- A root shell or `sudo`

## Install

```bash
curl -fLO "https://github.com/albertsdk/hsync-client/releases/latest/download/hsync-client-x86_64.rpm"
sudo dnf install hsync-client-x86_64.rpm
hsync --help
```

The URL always points to the **latest stable** release; use `aarch64` instead of
`x86_64` on ARM. To pin a version, use
`https://github.com/albertsdk/hsync-client/releases/download/vX.Y.Z/hsync-client-<version>-1.el9.<arch>.rpm`.

To build from the checkout instead, follow the build steps in
[Contributing](../CONTRIBUTING.md).

## Run as a boot service (systemd)

The `.rpm` ships the same systemd unit as the Debian package. Configuration is
read from `/etc/hsync/hsync.conf`; the service runs in **p2p mode** as the
`_hsync` system user and is **not** enabled automatically — you opt in.

```bash
# 1. Edit the configuration (bucket, path, p2p key, …)
sudo nano /etc/hsync/hsync.conf

# 2. Enable and start the service at boot
sudo systemctl enable --now hsync.service

# 3. Check status / logs
sudo systemctl status hsync.service
sudo tail -f /var/log/hsync/hsync.log
```

> ⚠️ `HSYNC_P2P_KEY` must be set in `/etc/hsync/hsync.conf` before enabling the
> service, exactly as documented for the
> [Debian package](getting-started-deb.md#run-as-a-boot-service-systemd).

## Update / remove

```bash
sudo dnf upgrade hsync-client-*.rpm
sudo dnf remove hsync-client
```

## Next

- Every CLI option and environment variable: [Configuration](configuration.md).
- Prefer no systemd? [Installer script](getting-started-script.md) or
  [standalone binary](getting-started-binary.md).
