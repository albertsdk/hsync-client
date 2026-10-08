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

Download it from the [Releases page](https://github.com/albertsdk/hsync-client/releases)
and verify it against `SHA256SUMS` — see
[Installation → Verify the download](installation.md#verify-the-download).

The **latest stable** archive of each target is also available at a stable URL,
without knowing the version — replace `linux-amd64` with your target
(`linux-arm64`, `darwin-arm64`, `darwin-amd64`, `windows-amd64`):

```bash
curl -fLO "https://github.com/albertsdk/hsync-client/releases/latest/download/hsync-client-linux-amd64.tar.gz"
```

## Extract and install

```bash
# Linux / macOS
tar -xzf hsync-client-VERSION-linux-amd64.tar.gz
install hsync ~/.local/bin/hsync
```

Requires the standalone `hsync` binary to be executable and on your `PATH`.
Alternatively, extract it into any directory and invoke it by full path.

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
