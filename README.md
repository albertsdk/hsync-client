# HSYNC Client

CLI client for HTTP-based file synchronization with WebSocket support for real-time events.

## Synchronization Overview

```mermaid
flowchart LR
    subgraph LocalA["🖥️ Client A"]
        FSA["📂 File System"]
        CA["⚙️ HSYNC Client"]
    end

    subgraph Remote["☁️ Remote"]
        SERVER["🗄️ HSYNC Server"]
    end

    subgraph LocalB["💻 Client B"]
        FSB["📂 File System"]
        CB["⚙️ HSYNC Client"]
    end

    %% Client A changes flow
    FSA -->|"1️⃣ File changed"| CA
    CA -->|"2️⃣ Upload"| SERVER
    SERVER -.->|"3️⃣ Notify"| CB
    CB -->|"4️⃣ Download"| FSB

    %% Client B changes flow
    FSB -->|"1️⃣ File changed"| CB
    CB -->|"2️⃣ Upload"| SERVER
    SERVER -.->|"3️⃣ Notify"| CA
    CA -->|"4️⃣ Download"| FSA

    style FSA fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    style CA fill:#e3f2fd,stroke:#1565c0,color:#0d47a1
    style SERVER fill:#fff3e0,stroke:#e65100,color:#e65100
    style FSB fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    style CB fill:#f3e5f5,stroke:#7b1fa2,color:#7b1fa2
```

<details>
<summary>📷 View as image (if Mermaid is not rendered)</summary>

![Synchronization Overview](docs/images/README-1.png)

</details>

**Bidirectional Sync:**
- **Client A → Client B**: A detects change → uploads → server notifies B → B downloads
- **Client B → Client A**: B detects change → uploads → server notifies A → A downloads

## Features

- 🔄 Bidirectional file synchronization
- ⚡ Parallel file transfers (configurable concurrency)
- 👁️ Real-time file system monitoring (watchdog)
- 📡 WebSocket connection for remote events
- ✅ MD5-based file integrity verification
- 🔐 API Key authentication (secure first-message auth)
- 🔒 SSL/TLS support
- 📦 Compilable to standalone binary (PyInstaller)
- 💓 WebSocket heartbeat for reliable connections
- 🔗 Symbolic link synchronization support
- 📦 Event queue with exponential backoff retry
- 🌐 **LAN Mode**: P2P sync without server (mDNS discovery)

## Requirements

For building:
- Python ≥3.12, <3.14
- Poetry (for dependency management)

For running:
- The compiled binary (no Python required)

## Build

The client must be compiled before use. The resulting binary is standalone and doesn't require Python.

```bash
cd hsync-client

# Install dependencies (first time only)
poetry install

# Compile the binary
./compile.sh

# The binary will be in dist/hsync
```

### Build Options

```bash
./compile.sh           # Build the binary
./compile.sh --clean   # Clean artifacts and rebuild
./compile.sh --help    # Show help
```

## Configuration

### Environment Files

The client provides environment file templates for easy configuration:

```bash
# Development (HTTP, localhost)
source dev.env
./dist/hsync -b mybucket -w

# Production (HTTPS, remote server)
source prod.env
./dist/hsync -b mybucket -w
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HSYNC_SERVER` | Server URL | (required or use `-s`) |
| `HSYNC_API_KEY` | API key for authentication | (required) |
| `HSYNC_MAX_CONCURRENT_TRANSFERS` | Max parallel file transfers | `5` |

## Usage

### Basic Syntax

```bash
./dist/hsync -s <server_url> -b <bucket_name> [options]
```

> ⚠️ **Note**: Do not run the client directly with Python. Always use the compiled binary.

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
| `--ca-cert` | Path to CA certificate for self-signed server certs |
| `--insecure` | Skip SSL certificate verification |
| `--no-color` | Disable colored output |
| `--help-symbols` | Show output symbols legend |
| `-V, --version` | Show version |
| `--lan-mode` | Enable LAN P2P mode (no server required) |
| `--lan-port` | Port for LAN HTTP server (0 = random) |
| `--lan-key` | Shared secret for LAN authentication |
| `--lan-group` | Group name for peer filtering |
| `--discovery-timeout` | Peer discovery timeout in seconds |

### Examples

**Single synchronization:**
```bash
export HSYNC_API_KEY="your-api-key"
./dist/hsync -s http://localhost:8765 -b mybucket -p /path/to/local/dir
```

**Continuous synchronization (watch mode):**
```bash
./dist/hsync -s http://localhost:8765 -b mybucket -p /path/to/dir -w
```

**With automatic re-sync every 5 minutes:**
```bash
./dist/hsync -s http://localhost:8765 -b mybucket -w -i 300
```

**Dry-run (preview operations):**
```bash
./dist/hsync -s http://localhost:8765 -b mybucket -n
```

**With file logging:**
```bash
./dist/hsync -s http://localhost:8765 -b mybucket -w -o /var/log/hsync.log
```

## SSL/TLS (HTTPS/WSS)

The client supports encrypted communication via HTTPS and WSS (WebSocket Secure).

### Connecting to a Server with Valid SSL Certificate

```bash
# Standard HTTPS connection (certificate verified against system CA store)
./dist/hsync -s https://server.example.com:8765 -b mybucket
```

### Connecting to a Server with Self-Signed Certificate

When the server uses a self-signed certificate, you have two options:

**Option 1: Trust the server certificate (recommended)**

```bash
# Copy the server's certificate to the client machine
scp server:/path/to/certs/server.crt ./server.crt

# Connect using the CA certificate
./dist/hsync -s https://server:8765 -b mybucket --ca-cert server.crt
```

**Option 2: Disable SSL verification (not recommended for production)**

```bash
# Skip certificate verification (insecure!)
./dist/hsync -s https://server:8765 -b mybucket --insecure
```

> ⚠️ **Warning**: Using `--insecure` disables all SSL certificate verification, making the connection vulnerable to man-in-the-middle attacks. Only use this for testing.

### SSL Examples

**Watch mode with self-signed certificate:**
```bash
./dist/hsync -s https://server:8765 -b mybucket -w --ca-cert server.crt
```

**Dry-run with HTTPS:**
```bash
./dist/hsync -s https://server:8765 -b mybucket -n --ca-cert server.crt
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
| `L` | Create/update symlink |

Log levels (ERROR, WARNING, INFO, DEBUG) are shown by the logging system.

## Synchronization Modes

### Single (default)
Performs a synchronization and exits.

### Watch (`-w`)
- Maintains WebSocket connection with the server
- Receives remote events (add/modify/delete/rename)
- Monitors local directory with watchdog
- Propagates changes in real-time

### LAN Mode (`--lan-mode`)

Peer-to-peer synchronization without a centralized server:

```bash
# On all devices (they discover each other automatically)
./dist/hsync -b photos -p ~/Pictures --lan-mode

# With authentication (recommended)
./dist/hsync -b photos -p ~/Pictures --lan-mode --lan-key "mySecretKey"
```

**How it works:**
1. Peers discover each other via mDNS (Bonjour/Avahi)
2. The peer with the lowest Client ID becomes the master
3. Other peers sync files with the master via HTTP

See [LAN Mode documentation](docs/lan-mode.md) for details.

## Documentation

- [Architecture](docs/architecture.md) - Client architecture and component overview
- [Usage Guide](docs/usage.md) - Detailed usage instructions with examples
- [LAN Mode](docs/lan-mode.md) - P2P synchronization without server

## Project Structure

```
hsync-client/
├── hsync_client/           # Main Python package (source code)
│   ├── __init__.py
│   ├── __main__.py         # Module entry point
│   ├── main.py             # Main CLI logic
│   ├── cli.py              # CLI argument parsing
│   ├── config.py           # Configuration management
│   ├── constants.py        # Application constants
│   ├── sync.py             # Sync engine with parallel transfers
│   ├── transfer.py         # File upload/download operations
│   ├── websocket_handler.py # WebSocket event handling
│   ├── logger.py           # Logging configuration
│   ├── event_queue.py      # Event queue with retry logic
│   └── models/             # Data models
│       ├── __init__.py
│       ├── dir.py
│       ├── event.py
│       ├── file.py
│       ├── symlink.py      # Symbolic link model
│       └── sync_action.py
├── docs/                   # Documentation
│   ├── architecture.md
│   └── usage.md
├── tests/                  # Test files
├── dist/                   # Compiled binary (created by compile.sh)
│   └── hsync               # Standalone executable
├── build/                  # Build artifacts (created by compile.sh)
├── dev.env                 # Development environment template
├── prod.env                # Production environment template
├── compile.sh              # Build script (PyInstaller with optimizations)
├── pyproject.toml
├── poetry.lock
└── README.md
```

## Version

0.1.0
