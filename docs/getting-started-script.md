# Getting Started — Installer script

The `install.sh` installer is the simplest way to get the client on Linux and
macOS. It detects your platform, downloads the correct binary, verifies its
checksum, installs it to `~/.local/share/hsync-client`, and creates a symlink in
`~/.local/bin`.

## Prerequisites

- Linux or macOS (`x86-64` or `arm64`)
- `curl`
- A writable `~/.local` (the installer never invokes `sudo`)

## Install

### Quick install (one command, latest stable release)

```bash
curl -fsSL https://github.com/albertsdk/hsync-client/releases/latest/download/install.sh | bash
```

### Verified install (checksum)

Download and verify the installer before running it:

```bash
curl -fLO "https://github.com/albertsdk/hsync-client/releases/latest/download/install.sh"
curl -fLO "https://github.com/albertsdk/hsync-client/releases/latest/download/SHA256SUMS"
sha256sum -c SHA256SUMS --ignore-missing
chmod 700 install.sh
./install.sh
```

See [Installation → Verify the download](installation.md#verify-the-download)
for the checksum reference.

## Verify the installation

```bash
hsync --help
```

If `hsync` is not found, `~/.local/bin` is not on your `PATH` — add it or re-run
the installer without `--no-symlink`.

## Manage and update

Re-running the installer updates the binary to the selected version while
preserving the install directory. To install a **specific version** instead of
the latest stable one, download the installer from that release (replace
`latest/download` with `download/vX.Y.Z` in the URLs above) or pass the version
explicitly:

```bash
./install.sh --version v1.0.0
```

Conversely, `--version latest` makes any installer resolve and install the latest
stable release automatically. Run `./install.sh --help` for options (`--version`,
`--install-dir`, `--no-symlink`, `--dry-run`).

## Next

- Prefer to manage the binary yourself? [Standalone binary](getting-started-binary.md).
- On Debian/Ubuntu? [Debian package](getting-started-deb.md).
- No prebuilt asset for your platform? [Build from source](getting-started-source.md).
