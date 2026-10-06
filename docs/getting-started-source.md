# Getting Started — From source

Build the client yourself when no prebuilt asset fits your platform, or when you
want to modify it.

## Prerequisites

- Python `>=3.12,<3.15` (CI target: **3.13**)
- [Poetry](https://python-poetry.org/) for dependency management
- **`hsync-common` cloned as a sibling directory** — it is declared as a Poetry
  path dependency (`../hsync-common`) and `poetry install` will fail without it
- Additional targets (Raspberry Pi, `.deb` cross-arch, WD OS5 `.bin`) require
  **Docker** with `qemu-user-static` (binfmt) configured for cross-architecture
  emulation

## Build the native binary

Clone both repositories side by side and install the dependencies with Poetry
(which requires `hsync-common` as a sibling) — see
[Contributing](../CONTRIBUTING.md). Then compile:

```bash
./compile.sh        # → dist/<os>-<arch>/hsync
```

The compile script auto-detects the host platform. Additional cross-arch targets
require Docker and run under QEMU emulation:

```bash
./compile.sh --target rpi           # Raspberry Pi armhf + arm64 (Docker + qemu)
./compile.sh --target deb-all       # .deb for amd64, arm64, armhf (Docker + qemu)
./compile.sh --target wd-os5-all    # .bin for all WD OS5 models (Docker + qemu)
./compile.sh --help                 # all options
```

## Next

- First run: [Getting Started](getting-started.md).
- Development workflow: [Contributing](../CONTRIBUTING.md).
