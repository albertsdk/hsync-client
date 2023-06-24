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
| `HSYNC_ENDPOINT_URL` | HSYNC server URL | No (use `-u`) |
| `HSYNC_API_KEY` | API key for authentication | Yes |

## Usage

### Basic Syntax

```bash
# Using poetry run
poetry run hsync -u <server_url> -b <bucket_name> [options]

# Or run as a module
poetry run python -m hsync_client -u <server_url> -b <bucket_name> [options]
```

### CLI Options

| Option | Description |
|--------|-------------|
| `-u, --endpoint-url` | HSYNC server URL |
| `-b, --bucket` | Bucket name (required) |
| `-d, --dir` | Local directory to sync (default: cwd) |
| `-c, --continuous` | Continuous synchronization mode |
| `--sync-order` | Sync order: `mtime` or `ctime` (default: mtime) |
| `--resync` | Interval (sec) for automatic re-synchronization |
| `--reconnect-delay` | Reconnection delay (default: 5 sec) |
| `--dryrun` | Show operations without executing them |
| `--debug` | Enable debug messages |
| `-q, --quiet` | Suppress output |
| `-l, --logfile` | Log to file |
| `--no-verify-ssl` | Disable SSL verification |
| `--legend` | Show log legend |
| `-v, --version` | Show version |

### Examples

**Single synchronization:**
```bash
export HSYNC_API_KEY="your-api-key"
poetry run hsync -u http://localhost:8765 -b mybucket -d /path/to/local/dir
```

**Continuous synchronization:**
```bash
poetry run hsync -u http://localhost:8765 -b mybucket -d /path/to/dir -c
```

**With automatic re-sync every 5 minutes:**
```bash
poetry run hsync -u http://localhost:8765 -b mybucket -c --resync 300
```

**Dry-run (preview operations):**
```bash
poetry run hsync -u http://localhost:8765 -b mybucket --dryrun
```

**With file logging:**
```bash
poetry run hsync -u http://localhost:8765 -b mybucket -c -l /var/log/hsync.log
```

## Log Legend

| Symbol | Meaning |
|--------|---------|
| `-` | File unchanged |
| `D` | Download file |
| `U` | Upload file |
| `M` | Move/Rename file/dir |
| `R` | Remove file/dir |
| `P` | Set permissions |
| `I` | Info |
| `W` | Warning |
| `E` | Error |

## Build Standalone Binary

```bash
# Build with PyInstaller
poetry run pyinstaller --onefile hsync_client/cli.py --name hsync

# The binary will be in dist/hsync
./dist/hsync -u http://localhost:8765 -b mybucket
```

## Synchronization Modes

### Single (default)
Performs a synchronization and exits.

### Continuous (`-c`)
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
