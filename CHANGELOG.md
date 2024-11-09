# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2025-12-xx

### Added
- Initial release: bidirectional file synchronization client
- Server Mode: connect to a central HSYNC Server via REST + WebSocket
- LAN Mode (P2P): peer-to-peer sync with mDNS discovery and master election
- Real-time file system monitoring (watchdog)
- Parallel file transfers (configurable concurrency)
- SHA-256-based file integrity verification
- Zstandard (zstd) transfer compression
- Symbolic link synchronization support
- `.hsyncignore` (gitignore-style) filtering
- Bandwidth limiting (token bucket)
- Compilable to standalone binary (PyInstaller)
- Prebuilt binaries for Linux (amd64, arm64), macOS (amd64, arm64), Windows (amd64)
- `.deb` package with systemd service
- WD My Cloud OS5 `.bin` installer
- Runtime stdin command protocol (direction change, resync, exit)
- Three-way merge conflict resolution with `.hsyncstate` version store
- Mass deletion guard
- Event queue with exponential backoff retry
- Comprehensive documentation

[1.0.0]: https://github.com/albertobasso/hsync-client/releases/tag/v1.0.0
