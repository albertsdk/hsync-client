# Debian Package Guide

The HSYNC client is available as a `.deb` package for Debian / Ubuntu (amd64,
arm64, armhf). This guide covers installation, the systemd service, and how to
customise it for your environment.

> See [Getting Started — Debian package](getting-started-deb.md) for the basic
> install and service-enable steps.

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
- Operates in **p2p mode** with continuous watch by default

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

```ini
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
| `HSYNC_P2P_HTTPS` | `true` | Use HTTPS for p2p peer communication (always on, not configurable) |
| `HSYNC_P2P_GROUP` | *(empty)* | Restrict discovery to a group |
| `HSYNC_P2P_PASSPHRASE` | *(empty)* | Shared password for peer authentication (at least 12 characters; derived to the group key). Mutually exclusive with `HSYNC_P2P_KEY` |
| `HSYNC_P2P_KEY` | *(empty)* | Full raw group key (at least 32 characters) for peer authentication (also the bootnode group key for protected/private bootnodes) |
| `HSYNC_DEVICE_DIR` | `/var/lib/hsync/device` | Directory for the persistent device identity (device ID + p2p keypair) and the per-folder sync-state DBs (`syncstate/<uuid>.db`, `syncstate/index.db`) |
| `HSYNC_STATE_DIR` | `/var/lib/hsync/state` | Installation state directory: the server's per-bucket state and the default parent of the device identity, trust stores and p2p pins when their own override is unset |
| `HSYNC_LOCKS_DIR` | `/var/lib/hsync/locks` | Directory for the per-directory instance locks (`<sha256>.lock` per synced directory) |
| `HSYNC_P2P_PIN_FILE` | `/var/lib/hsync/p2p_known_peers.json` | File storing pinned fingerprints of known p2p peers (TOFU) |
| `HSYNC_BOOTNODE_TRUST_FILE` | `/var/lib/hsync/bootnode_known_hosts.json` | File storing trusted bootnode fingerprints (known-hosts store) |
| `HSYNC_SERVER_TRUST_FILE` | `/var/lib/hsync/server_known_hosts.json` | File storing trusted server fingerprints (known-hosts store) |
| `HSYNC_SERVER_TRUST` | `chain` | Server TLS trust policy (`chain`/`ask`/`accept-new`/`strict`/`none`) |
| `HSYNC_DISCOVERY_TIMEOUT` | `0` | Seconds to wait for the first peer (0 = indefinite) |
| `HSYNC_NEVER_MASTER` | `false` | Never become master; always remain a client/slave |
| `HSYNC_SERVER` | *(empty)* | Server endpoint: `host`/`host:port`/`[IPv6]`/`[IPv6]:port` or full `https://` URL (for server mode) |
| `HSYNC_API_KEY` | *(empty)* | API key (for server mode) |
| `HSYNC_IGNORE_DELETE` | `false` | Do not propagate file deletions (backup mode) |
| `HSYNC_COMPRESSION_ENABLED` | `true` | Enable zstd compression for file transfers |

> Variables marked with † are used in the unit's `ExecStart` line and are not
> read by the hsync binary itself. The rest are `HSYNC_*` environment variables
> consumed directly by the binary.

## See also

- [Getting Started — Debian package](getting-started-deb.md) — basic install and service setup.
- [Configuration](configuration.md) — every CLI option and environment variable.
- [FAQ](faq.md) — frequently asked questions.
