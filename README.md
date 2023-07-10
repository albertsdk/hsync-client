# HSYNC Client

CLI client for HTTP-based file synchronization with WebSocket support for real-time events.

## Features

- 🔄 Bidirectional file synchronization
- 👁️ Real-time file system monitoring (watchdog)
- 📡 WebSocket connection for remote events
- ✅ MD5-based file integrity verification
- 🔐 API Key authentication
- 🔒 SSL/TLS support
- 📦 Compilable to standalone binary (PyInstaller)

## Requirements

- Python ≥3.12, <3.14
- Poetry (for dependency management)

## Installation

```bash
cd hsync-client

# Install dependencies with Poetry
poetry install

# Activate the virtual environment
poetry shell
```

## Configuration

The client uses environment variables:

| Variable | Description | Required |
|----------|-------------|----------|
| `HSYNC_SERVER_URL` | Server URL | No (use `-s`) |
| `HSYNC_API_KEY` | API key for authentication | Yes |

## Usage

### Basic Syntax

```bash
# Using poetry run
poetry run hsync -s <server_url> -b <bucket_name> [options]

# Or run as a module
poetry run python -m hsync_client -s <server_url> -b <bucket_name> [options]
```

### CLI Options

| Option | Description |
|--------|-------------|
| `-s, --server` | Server URL |
| `-b, --bucket` | Bucket name (required) |
| `-p, --path` | Local directory path (default: cwd) |
| `-w, --watch` | Watch for changes and sync continuously |
| `--order-by` | Compare files by: `mtime` or `ctime` (default: mtime) |
| `-i, --interval` | Force full sync every N seconds (requires `-w`) |
| `--retry-delay` | Retry connection after N seconds (default: 5) |
| `-n, --dry-run` | Show what would be done without making changes |
| `-v, --verbose` | Enable verbose output |
| `-q, --quiet` | Suppress all output |
| `-o, --output` | Write output to file |
| `--insecure` | Skip SSL certificate verification |
| `--no-color` | Disable colored output |
| `--help-symbols` | Show output symbols legend |
| `-V, --version` | Show version |

### Examples

**Single synchronization:**
```bash
export HSYNC_API_KEY="your-api-key"
poetry run hsync -s http://localhost:8765 -b mybucket -p /path/to/local/dir
```

**Continuous synchronization (watch mode):**
```bash
poetry run hsync -s http://localhost:8765 -b mybucket -p /path/to/dir -w
```

**With automatic re-sync every 5 minutes:**
```bash
poetry run hsync -s http://localhost:8765 -b mybucket -w -i 300
```

**Dry-run (preview operations):**
```bash
poetry run hsync -s http://localhost:8765 -b mybucket -n
```

**With file logging:**
```bash
poetry run hsync -s http://localhost:8765 -b mybucket -w -o /var/log/hsync.log
```

## Log Legend

| Symbol | Meaning |
|--------|---------|
| `·` | File unchanged (skipped) |
| `↓` | Download file |
| `↑` | Upload file |
| `→` | Move/Rename file/dir |
| `✕` | Remove file/dir |
| `+` | Create directory |
| `⚙` | Set permissions |

Log levels (ERROR, WARNING, INFO, DEBUG) are shown by the logging system.

## Build Standalone Binary

```bash
# Build with PyInstaller
./compile.sh

# The binary will be in dist/hsync
./dist/hsync -s http://localhost:8765 -b mybucket
```

## Synchronization Modes

### Single (default)
Performs a synchronization and exits.

### Watch (`-w`)
- Maintains WebSocket connection with the server
- Receives remote events (add/modify/delete/rename)
- Monitors local directory with watchdog
- Propagates changes in real-time

## Project Structure

```
hsync-client/
├── hsync_client/           # Main Python package
│   ├── __init__.py
│   ├── __main__.py         # Entry point: python -m hsync_client
│   ├── cli.py              # CLI logic
│   ├── logging.py          # Logging configuration
│   └── models/             # Data models
│       ├── __init__.py
│       ├── dir.py
│       ├── event.py
│       ├── file.py
│       └── sync_action.py
├── tests/                  # Test files
├── pyproject.toml
├── poetry.lock
└── README.md
```

## Version

0.1.0
