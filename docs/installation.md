# Installation

There are two ways to get the HSYNC client: download a **prebuilt binary** from a
GitHub Release (recommended for most users) or **build it from source**.

This page is the **asset reference** — which file to download for your platform
and how to verify it. Step-by-step install procedures live in the
[Getting Started](getting-started.md) chooser; pick a method there and follow
exactly one page.

## Available formats

Every release publishes all the assets below for every supported platform. Pick
the row that matches your OS and architecture, then follow the corresponding
procedure.

| Format | OS | Architecture | Asset name | Procedure |
| ------ | -- | ------------ | ---------- | --------- |
| Installer script | Linux/macOS | x86-64/ARM64 | `install.sh` | [Installer script](getting-started-script.md) |
| Standalone binary | Linux | x86-64 | `hsync-client-<version>-linux-amd64.tar.gz` | [Standalone binary](getting-started-binary.md) |
| Standalone binary | Linux | ARM64 | `hsync-client-<version>-linux-arm64.tar.gz` | [Standalone binary](getting-started-binary.md) |
| Standalone binary | macOS | Apple Silicon | `hsync-client-<version>-darwin-arm64.tar.gz` | [Standalone binary](getting-started-binary.md) |
| Standalone binary | macOS | Intel | `hsync-client-<version>-darwin-amd64.tar.gz` | [Standalone binary](getting-started-binary.md) |
| Standalone binary | Windows | x86-64 | `hsync-client-<version>-windows-amd64.zip` | [Standalone binary](getting-started-binary.md) |
| Debian package | Debian/Ubuntu | amd64 | `hsync-client_<version>_amd64.deb` | [Debian package](getting-started-deb.md) |
| Debian package | Debian/Ubuntu | arm64 | `hsync-client_<version>_arm64.deb` | [Debian package](getting-started-deb.md) |
| Debian package | Debian/Ubuntu | armhf | `hsync-client_<version>_armhf.deb` | [Debian package](getting-started-deb.md) |
| WD OS5 installer | WD My Cloud OS5 | model-specific | `<MODEL>_hsync_<version>.bin` | [WD OS5 packaging](../packaging/wd-os5/README.md) |
| Checksums | all | all | `SHA256SUMS` | [Verify the download](#verify-the-download) |

> **Note**: all assets share the `hsync-client` prefix. Binary archives use
> hyphens (`hsync-client-<version>-<target>.tar.gz`) while Debian packages
> follow the Debian convention with underscores (`hsync-client_<version>_<arch>.deb`).
> Both contain the same `hsync` executable.

Every tag (`vX.Y.Z`) publishes the full set on the
[Releases page](https://github.com/albertsdk/hsync-client/releases). Building
from source is covered in
[Getting Started — From source](getting-started-source.md).

## Verify the download

Each release ships a `SHA256SUMS` file covering every asset. Always verify your
download before running it:

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

## Next

- [Getting Started](getting-started.md) — pick an install method and run the
  first sync.
- [Configuration](configuration.md) — every CLI option and environment variable.
