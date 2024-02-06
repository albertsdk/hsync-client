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

# The binary will be in dist/hsync
```

### 2. Basic Usage

```bash
# Single sync
./dist/hsync -s http://localhost:8765 -b mybucket -p /path/to/dir

# Continuous sync (watch mode)
./dist/hsync -s http://localhost:8765 -b mybucket -p /path/to/dir -w
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
