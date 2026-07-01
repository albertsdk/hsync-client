# Installation

There are two ways to get the HSYNC client: download a **prebuilt binary** from a
GitHub Release (recommended for most users) or **build it from source**.

## Install prebuilt (recommended)

Every tagged release publishes ready-to-run assets on the
[Releases page](https://github.com/albertobasso/hsync-client/releases). Pick the
asset that matches your platform.

### Standalone binary

| OS | Architecture | Asset |
| -- | ------------ | ----- |
| Linux | x86-64 | `hsync-<version>-linux-amd64.tar.gz` |
| Linux | ARM64 | `hsync-<version>-linux-arm64.tar.gz` |
| macOS | Apple Silicon | `hsync-<version>-darwin-arm64.tar.gz` |
| macOS | Intel | `hsync-<version>-darwin-amd64.tar.gz` |
| Windows | x86-64 | `hsync-<version>-windows-amd64.zip` |

```bash
# Example: Linux x86-64
tar xzf hsync-<version>-linux-amd64.tar.gz
chmod +x hsync
./hsync --help
```

On Windows, unzip the archive and run `hsync.exe` from a terminal.

### Verify the download

Each release also ships a `SHA256SUMS` file. Verify your asset before running it:

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

### macOS: clear the quarantine flag

The macOS binaries are **not** notarized, so Gatekeeper blocks them on first run.
Remove the quarantine attribute after extracting:

```bash
xattr -d com.apple.quarantine ./hsync
```

You only need to do this once per downloaded binary.

### Debian / Ubuntu (.deb)

Native packages are provided for `amd64`, `arm64` and `armhf`:

```bash
sudo dpkg -i hsync-client_<version>_amd64.deb
hsync --help
```

The package installs the `hsync` command system-wide.

#### Run as a boot service (systemd)

The `.deb` ships a systemd unit that starts `hsync` automatically at boot.
Configuration is read from `/etc/hsync/hsync.conf` (an environment file loaded
by the unit). By default the service runs in **LAN mode** with continuous
watch as the `_hsync` system user; see the comments in
`/etc/hsync/hsync.conf` to switch to Server Mode.

> ⚠️ The service runs non-interactively (`--yes`) with bidirectional sync, so
> `HSYNC_LAN_KEY` **must be set** in `/etc/hsync/hsync.conf` before enabling
> it — otherwise the client aborts with `[AUTH_REQUIRED]`. All peers in the
> same LAN group must share the same key. The default config also sets
> `HSYNC_LAN_CERT_DIR=/var/lib/hsync/lan_cert` because the `_hsync` system
> user has no home directory.

```bash
# 1. Edit the configuration (bucket, path, LAN key, …)
sudo nano /etc/hsync/hsync.conf

# 2. Enable and start the service at boot
sudo systemctl enable --now hsync.service

# 3. Check status / logs
sudo systemctl status hsync.service
sudo tail -f /var/log/hsync/hsync.log
```

> ⚙️ **Custom sync path**
>
> The default `HSYNC_PATH` is `/var/lib/hsync`. If you change it to a directory
> under `/home` (e.g. `/home/alice/Documents`), the service can **read** those
> files (thanks to `ProtectHome=read-only`) but cannot **write** to them unless
> you also update the service's `ReadWritePaths`:
>
> ```bash
> sudo systemctl edit hsync.service
> ```
>
> Add the following (replace the path with your own):
>
> ```ini
> [Service]
> ReadWritePaths=/home/alice/Documents
> ```

For the full list of configuration parameters (environment variables and CLI
options), see the [Configuration guide](configuration.md).

### WD My Cloud OS5 (.bin)

Per-model `.bin` installers are provided for supported WD My Cloud OS5 devices
(file name pattern `<MODEL>_hsync_<version>.bin`). Install the `.bin` that matches
your model through the OS5 dashboard. See the
[WD OS5 packaging guide](../packaging/wd-os5/README.md) for the full procedure.

## Build from source

Requires Python ≥3.12, <3.15 and [Poetry](https://python-poetry.org/).

```bash
cd hsync-client
poetry install      # first time only (build dependencies)
./compile.sh        # produces dist/<os>-<arch>/hsync
```

The compile script auto-detects the host platform. Additional targets:

```bash
./compile.sh --target deb-all       # .deb for amd64, arm64, armhf (Docker)
./compile.sh --target wd-os5-all    # .bin for all WD OS5 models (Docker)
./compile.sh --help                 # all options
```

See [Getting Started](getting-started.md) for the first-run walkthrough.
