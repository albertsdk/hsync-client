# Installation

There are two ways to get the HSYNC client: download a **prebuilt binary** from a
GitHub Release (recommended for most users) or **build it from source**.

## Available formats

Every release publishes all the assets below for every supported platform. Pick
the row that matches your OS and architecture, then follow the corresponding
section for the install steps.

| Format | OS | Architecture | Asset name | Install |
| ------ | -- | ------------ | ---------- | ------- |
| Installer script | Linux/macOS | x86-64/ARM64 | `install.sh` | [Installer](#installer-script-recommended) |
| Standalone binary | Linux | x86-64 | `hsync-client-<version>-linux-amd64.tar.gz` | [Binary](#standalone-binary) |
| Standalone binary | Linux | ARM64 | `hsync-client-<version>-linux-arm64.tar.gz` | [Binary](#standalone-binary) |
| Standalone binary | macOS | Apple Silicon | `hsync-client-<version>-darwin-arm64.tar.gz` | [Binary](#standalone-binary) |
| Standalone binary | macOS | Intel | `hsync-client-<version>-darwin-amd64.tar.gz` | [Binary](#standalone-binary) |
| Standalone binary | Windows | x86-64 | `hsync-client-<version>-windows-amd64.zip` | [Binary](#standalone-binary) |
| Debian package | Debian/Ubuntu | amd64 | `hsync-client_<version>_amd64.deb` | [Debian/Ubuntu](#debian--ubuntu-deb) |
| Debian package | Debian/Ubuntu | arm64 | `hsync-client_<version>_arm64.deb` | [Debian/Ubuntu](#debian--ubuntu-deb) |
| Debian package | Debian/Ubuntu | armhf | `hsync-client_<version>_armhf.deb` | [Debian/Ubuntu](#debian--ubuntu-deb) |
| WD OS5 installer | WD My Cloud OS5 | model-specific | `<MODEL>_hsync_<version>.bin` | [WD My Cloud OS5](#wd-my-cloud-os5-bin) |
| Checksums | all | all | `SHA256SUMS` | [Verify the download](#verify-the-download) |

> **Note**: all assets share the `hsync-client` prefix. Binary archives use
> hyphens (`hsync-client-<version>-<target>.tar.gz`) while Debian packages
> follow the Debian convention with underscores (`hsync-client_<version>_<arch>.deb`).
> Both contain the same `hsync` executable.

## Install prebuilt (recommended)

Every tagged release (`vX.Y.Z`) publishes a complete set of ready-to-run assets
on the [Releases page](https://github.com/albertsdk/hsync-client/releases):
standalone binaries for all supported platforms, Debian/Ubuntu `.deb` packages,
WD My Cloud OS5 `.bin` installers, an `install.sh` installer script,
and a `SHA256SUMS` checksum file. See the
[Available formats](#available-formats) table for the full list.

### Installer script (recommended)

The installer script is the simplest way to get the client on Linux and macOS.
It detects your platform, downloads the correct binary, verifies its checksum,
installs it to `~/.local/share/hsync-client`, and creates a symlink in
`~/.local/bin`.

**Quick install (one command, latest stable release):**

```bash
curl -fsSL https://github.com/albertsdk/hsync-client/releases/latest/download/install.sh | bash
```

**Verified install (checksum):**

```bash
# Download and verify the installer (latest stable release)
curl -fLO "https://github.com/albertsdk/hsync-client/releases/latest/download/install.sh"
curl -fLO "https://github.com/albertsdk/hsync-client/releases/latest/download/SHA256SUMS"
sha256sum -c SHA256SUMS --ignore-missing

# Run it
chmod 700 install.sh
./install.sh

# Verify the installation
hsync --help
```

The installer never invokes `sudo`; it uses a user-writable directory. Re-running
it updates the binary to the selected version while preserving the install
directory. Run `./install.sh --help` for options (`--version`,
`--install-dir`, `--no-symlink`, `--dry-run`).

To install a **specific version** instead of the latest stable one, download the
installer from that release (replace `latest/download` with
`download/vX.Y.Z` in the URLs above) or pass the version explicitly:
`./install.sh --version v1.0.0`. Conversely, `--version latest` makes any
installer resolve and install the latest stable release automatically.

### Standalone binary

| OS | Architecture | Asset |
| -- | ------------ | ----- |
| Linux | x86-64 | `hsync-client-<version>-linux-amd64.tar.gz` |
| Linux | ARM64 | `hsync-client-<version>-linux-arm64.tar.gz` |
| macOS | Apple Silicon | `hsync-client-<version>-darwin-arm64.tar.gz` |
| macOS | Intel | `hsync-client-<version>-darwin-amd64.tar.gz` |
| Windows | x86-64 | `hsync-client-<version>-windows-amd64.zip` |

### Verify the download

Each release ships a `SHA256SUMS` file covering every asset. Always verify your
download before running it:

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
> it — otherwise the client aborts with `[AUTH REQUIRED]`. All peers in the
> same LAN group must share the same key. The default config also sets
> `HSYNC_DEVICE_DIR=/var/lib/hsync/device` because the `_hsync` system
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

Building from source is only needed when no prebuilt asset fits your platform or
you want to modify the client.

### Prerequisites

- Python `>=3.12,<3.15` (CI target: **3.13**)
- [Poetry](https://python-poetry.org/) for dependency management
- **`hsync-common` cloned as a sibling directory** — it is declared as a Poetry
  path dependency (`../hsync-common`) and `poetry install` will fail without it.
- Additional targets (`.deb` cross-arch, WD OS5 `.bin`) require **Docker** with
  `qemu-user-static` (binfmt) configured for cross-architecture emulation.

### Build the native binary

Clone both repositories side by side, install dependencies, then compile:

```bash
# 1. Clone hsync-client and hsync-common as siblings
git clone https://github.com/albertsdk/hsync-client.git
git clone https://github.com/albertsdk/hsync-common.git

# 2. Install dependencies (creates .venv, includes the PyInstaller build group)
cd hsync-client
poetry install

# 3. Compile the standalone binary for the host platform
./compile.sh        # output: dist/<os>-<arch>/hsync
```

The compile script auto-detects the host platform. Additional cross-arch targets
require Docker and run under QEMU emulation:

```bash
./compile.sh --target deb-all       # .deb for amd64, arm64, armhf (Docker + qemu)
./compile.sh --target wd-os5-all    # .bin for all WD OS5 models (Docker + qemu)
./compile.sh --help                 # all options
```

See [Getting Started](getting-started.md) for the first-run walkthrough and
[Contributing](../CONTRIBUTING.md) for the development workflow.
