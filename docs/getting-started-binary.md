# Getting Started — Standalone binary

Every release publishes a self-contained executable (no Python runtime needed).
Download the archive for your platform, extract the `hsync` binary, and put it
somewhere on your `PATH`.

## Prerequisites

- The platform/architecture of your machine
- macOS only: permission to clear the quarantine flag (see below)

## Pick your asset

| OS | Architecture | Asset |
| -- | ------------ | ----- |
| <img src="assets/os/linux.svg" alt="Linux" width="16" height="16"> Linux | x86-64 | `hsync-client-<version>-linux-amd64.tar.gz` |
| <img src="assets/os/linux.svg" alt="Linux" width="16" height="16"> Linux | ARM64 | `hsync-client-<version>-linux-arm64.tar.gz` |
| <img src="assets/os/apple.svg" alt="macOS" width="16" height="16"> macOS | Apple Silicon | `hsync-client-<version>-darwin-arm64.tar.gz` |
| <img src="assets/os/apple.svg" alt="macOS" width="16" height="16"> macOS | Intel | `hsync-client-<version>-darwin-amd64.tar.gz` |
| <img src="assets/os/windows.svg" alt="Windows" width="16" height="16"> Windows | x86-64 | `hsync-client-<version>-windows-amd64.zip` |

Download it from the [latest release page](https://github.com/albertsdk/hsync-client/releases/latest)
and verify it against `SHA256SUMS` — see
[Installation → Verify the download](installation.md#verify-the-download).

The **latest stable** archive of each target is also available at a stable URL,
without knowing the version — replace `linux-amd64` with your target
(`linux-arm64`, `darwin-arm64`, `darwin-amd64`, `windows-amd64`):

```bash
curl -fLO "https://github.com/albertsdk/hsync-client/releases/latest/download/hsync-client-linux-amd64.tar.gz"
```

## Extract and install

Linux (x86-64):

```bash
tar -xzf hsync-client-VERSION-linux-amd64.tar.gz
mkdir -p ~/.local/bin
install -m 0755 hsync ~/.local/bin/hsync
```

macOS (Apple Silicon):

```bash
tar -xzf hsync-client-VERSION-darwin-arm64.tar.gz
mkdir -p ~/.local/bin
install -m 0755 hsync ~/.local/bin/hsync
```

Pick the archive that matches your machine (`...-linux-arm64`,
`...-darwin-arm64`, `...-darwin-amd64`). On macOS `~/.local/bin` is not on the
default `PATH`: the [installer script guide](getting-started-script.md) shows the
one-line `PATH` export. On Windows the `.zip` contains `hsync.exe`; the
[Windows installer](getting-started-windows.md) page covers extracting it and
adding it to `PATH`.

## macOS: clear the quarantine flag

The macOS binaries are **not** notarized, so Gatekeeper blocks them on first run.
Remove the quarantine attribute after extracting:

```bash
xattr -d com.apple.quarantine ./hsync
```

You only need to do this once per downloaded binary.

## Verify the installation

```bash
hsync --help
```

## Next

- Prefer one command? [Installer script](getting-started-script.md).
- On Debian/Ubuntu? [Debian package](getting-started-deb.md).
- No prebuilt asset for your platform? [Build from source](getting-started-source.md).
