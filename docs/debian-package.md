# Debian Package Guide

The HSYNC client is available as a `.deb` package for Debian / Ubuntu (amd64,
arm64, armhf). This guide covers installation, the systemd service, and how to
customise it for your environment.

> See [Installation](installation.md#debian--ubuntu-deb) for basic install
> instructions.

## Table of Contents

- [Service architecture](#service-architecture)
- [Customising the sync path](#customising-the-sync-path)
- [ProtectHome and directory visibility](#protecthome-and-directory-visibility)
- [Understanding write access](#understanding-write-access)
- [Running as a different user](#running-as-a-different-user)
- [Environment variables](#environment-variables)

## Service architecture

The `.deb` installs a systemd service (`hsync.service`) that:

- Runs as the dedicated **`_hsync`** system user (minimum privilege principle)
- Reads configuration from **`/etc/hsync/hsync.conf`** (an environment file)
- Synchronises **`/var/lib/hsync`** by default
- Writes logs to **`/var/log/hsync/hsync.log`**
- Operates in **LAN mode** with continuous watch by default

The service unit applies systemd sandboxing for security:

| Directive | Value | Purpose |
|-----------|-------|---------|
| `ProtectHome` | `read-only` | Can read files under `/home` but cannot write outside `ReadWritePaths` |
| `ProtectSystem` | `strict` | Most of the filesystem is read-only |
| `ReadWritePaths` | `/var/lib/hsync`, `/var/log/hsync` | Only these paths are writable |
| `PrivateTmp` | `yes` | Isolated `/tmp` |
| `NoNewPrivileges` | `yes` | Cannot escalate privileges |

## Customising the sync path

The default sync directory is `/var/lib/hsync`. To use a different path
(e.g. `/home/alice/Documents`), four steps are required:

### 1. Edit the configuration

```bash
sudo nano /etc/hsync/hsync.conf
```

Change `HSYNC_PATH` to your desired directory:

```bash
HSYNC_PATH="/home/alice/Documents"
```

### 2. Grant filesystem access to `_hsync`

The `_hsync` system user needs the `x` (traverse) permission on **every parent
directory** and `rwx` on the target directory itself. For
`/home/alice/Documents`:

```bash
sudo setfacl -m u:_hsync:x /home/alice
sudo setfacl -m u:_hsync:x /home/alice/Documents
sudo setfacl -m u:_hsync:rwx /home/alice/Documents
```

Verify with:

```bash
getfacl /home/alice/Documents
```

> Without the `x` permission on parent directories, the service will report
> `Directory '…' does not exist` even though the directory is there.

### 3. Update systemd sandboxing

The service uses `ProtectHome=read-only`, so it can read files under `/home`
but cannot write anywhere that is not explicitly listed in `ReadWritePaths`.
Add your path:

```bash
sudo systemctl edit hsync.service
```

Insert:

```ini
[Service]
ReadWritePaths=/home/alice/Documents
```

### 4. Reload and restart

```bash
sudo systemctl daemon-reload
sudo systemctl restart hsync.service
```

Check the logs to confirm it works:

```bash
sudo journalctl -u hsync.service -n 20 --no-pager
```

## ProtectHome and directory visibility

The systemd unit uses `ProtectHome=read-only` (see the
[systemd.exec(5)](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#ProtectHome=)
man page).

When set to `=yes` (the old default in earlier package versions), systemd mounts
an empty `tmpfs` over `/home`, making **every** directory under it invisible to
the service. The fix is `ProtectHome=read-only` — which the current package
ships.

If you are still seeing `Directory '…' does not exist` after upgrading, verify
the running unit:

```bash
systemctl cat hsync.service | grep ProtectHome
```

If it still shows `ProtectHome=yes`, the unit was overridden or the package was
not reinstalled. Run `sudo systemctl daemon-reload` after a package upgrade.

## Understanding write access

Two independent layers control whether the service can write to a directory.
**Both must be satisfied** — fixing only one will still result in write errors.

### Layer 1: systemd sandboxing

`ProtectHome=read-only` blocks writes to `/home` unless the path is listed in
`ReadWritePaths`. Fix this with `systemctl edit hsync.service` (see
[customising the sync path](#customising-the-sync-path)).

### Layer 2: POSIX permissions / ACLs

The `_hsync` system user must have write permission on the directory. The
easiest way is an ACL entry:

```bash
sudo setfacl -m u:_hsync:rwx /path/to/directory
```

To verify: `getfacl /path/to/directory`.

## Running as a different user

If you prefer the service to run as your own user (avoiding ACLs on your home
directory), override the service user:

```bash
sudo systemctl edit hsync.service
```

Insert:

```ini
[Service]
User=alice
Group=alice
ProtectHome=read-only
ReadWritePaths=/home/alice/Documents
```

Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart hsync.service
```

> **Security note**: the service will run with your user's privileges. Evaluate
> the implications for your environment.

## Environment variables

The following `HSYNC_*` variables are read from `/etc/hsync/hsync.conf` (or the
environment). Variables marked with a dagger (†) are substituted into the
systemd unit's `ExecStart`; the rest are consumed directly by the hsync binary.

| Variable | Default | Purpose |
|----------|---------|---------|
| `HSYNC_BUCKET` † | `hsync` | Remote bucket name |
| `HSYNC_PATH` † | `/var/lib/hsync` | Local directory to synchronise |
| `HSYNC_LAN_HTTPS` | `true` | Use HTTPS for LAN peer communication (always on, not configurable) |
| `HSYNC_LAN_GROUP` | *(empty)* | Restrict discovery to a group |
| `HSYNC_LAN_KEY` | *(empty)* | Shared secret for peer authentication |
| `HSYNC_DEVICE_DIR` | `/var/lib/hsync/device` | Directory for the persistent device identity (device ID + LAN keypair) |
| `HSYNC_STATE_DIR` | `/var/lib/hsync/state` | Directory for `syncstate.db` (causal versions and tombstones) |
| `HSYNC_LAN_PIN_FILE` | `/var/lib/hsync/lan_known_peers.json` | File storing pinned fingerprints of known LAN peers (TOFU) |
| `HSYNC_DISCOVERY_TIMEOUT` | `0` | Seconds to wait for the first peer (0 = indefinite) |
| `HSYNC_NEVER_MASTER` | `false` | Never become master; always remain a client/slave |
| `HSYNC_SERVER` | *(empty)* | Server URL (for server mode) |
| `HSYNC_API_KEY` | *(empty)* | API key (for server mode) |
| `HSYNC_IGNORE_DELETE` | `false` | Do not propagate file deletions (backup mode) |
| `HSYNC_COMPRESSION_ENABLED` | `true` | Enable zstd compression for file transfers |

> Variables marked with † are used in the unit's `ExecStart` line and are not
> read by the hsync binary itself. The rest are `HSYNC_*` environment variables
> consumed directly by the binary.

## See also

- [Installation](installation.md) — basic install instructions.
- [Configuration](configuration.md) — every CLI option and environment variable.
- [FAQ](faq.md) — frequently asked questions.
