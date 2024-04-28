# Getting Started

This guide will help you get HSYNC up and running quickly.

## Requirements

For building:
- Python ≥3.12, <3.14
- Poetry (for dependency management)

For running:
- The compiled binary (no Python required)

## Quick Start

### 1. Build the Client

```bash
cd hsync-client

# Install dependencies (first time only)
poetry install

# Compile the binary
./compile.sh

# The binary will be in dist/<os>-<arch>/hsync
# e.g. dist/linux-amd64/hsync, dist/darwin-arm64/hsync, dist/windows-amd64/hsync.exe
```

> **Note**: `<platform>` in examples below is a shorthand for `<os>-<arch>`
> (e.g. `linux-amd64`, `darwin-arm64`, `windows-amd64`).
> The compile script detects the current platform automatically.

### 2. Basic Usage

```bash
# Single sync
./dist/<platform>/hsync -s http://localhost:8765 -b mybucket -p /path/to/dir

# Continuous sync (watch mode)
./dist/<platform>/hsync -s http://localhost:8765 -b mybucket -p /path/to/dir -w
```

## Build Options

```bash
./compile.sh           # Build the binary
./compile.sh --clean   # Clean artifacts and rebuild
./compile.sh --help    # Show help
```

## Environment Configuration

The client provides environment file templates:

```bash
# Development (HTTP, localhost)
source dev.env

# Production (HTTPS, remote server)
source prod.env
```

See [Configuration](configuration.md) for details.
