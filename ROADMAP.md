# Roadmap

This document outlines the planned direction for HSYNC Client. Items are
organized by status and are not commitments — priorities may shift based on
community feedback and contributor availability.

## In Progress

- **Desktop GUI** — [hsync-desktop](https://github.com/albertobasso/hsync-desktop), a cross-platform graphical front-end
- **Android app** — [hsync-android](https://github.com/albertobasso/hsync-android), native file sync for Android

## Planned

- **Selective sync** — sync only specific subdirectories within a bucket (`--include` / `--exclude` patterns separate from `.hsyncignore`)
- **Encrypted buckets** — client-side encryption before upload (server never sees plaintext)
- **File versioning on client** — keep N previous versions locally (`.hsyncversions` directory)
- **Proxy support** — HTTP/HTTPS/SOCKS5 proxy for corporate environments
- **One-shot mode improvements** — exit codes for scripting, JSON output mode
- **Windows service** — run as a Windows service (in addition to systemd on Linux)
- **macOS launchd plist** — official launchd service definition

## Considering

- **iOS app** — native iOS client (Swift)
- **FUSE mount** — mount a bucket as a local read-only filesystem
- **Snapshot export** — export a bucket at a point in time (read-only copy)
- **Conflict resolution strategies** — configurable strategies beyond last-writer-wins (e.g., always keep local, always keep remote, manual merge tool)
- **GUI mode** — lightweight built-in TUI for headless servers

## Completed

- [x] Bidirectional file synchronization
- [x] Server Mode (REST + WebSocket)
- [x] LAN Mode (P2P with mDNS discovery and master election)
- [x] Real-time file watching (watchdog)
- [x] Parallel file transfers
- [x] SHA-256 integrity verification
- [x] Three-way merge conflict resolution
- [x] Zstandard compression
- [x] Bandwidth limiting
- [x] Compilable to standalone binary
- [x] .deb package with systemd service
- [x] Transfer only changed portions of large files (rsync-style rolling checksums)
- [x] Comprehensive documentation
