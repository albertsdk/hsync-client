# Roadmap

This document outlines the planned direction for HSYNC Client. Items are
organized by status and are not commitments — priorities may shift based on
community feedback and contributor availability.

## In Progress

- **Desktop GUI** — [hsync-desktop](https://github.com/albertosdk/hsync-desktop), a cross-platform graphical front-end
- **Android app** — [hsync-android](https://github.com/albertosdk/hsync-android), native file sync for Android

## Planned

- **Selective sync** — sync only specific subdirectories within a bucket (`--include` / `--exclude` patterns separate from `.hsyncignore`)
- **Encrypted buckets** — client-side encryption before upload (server never sees plaintext)
- **File versioning on client** — keep N previous versions locally (`.hsyncversions` directory)
- **Proxy support** — HTTP/HTTPS/SOCKS5 proxy for corporate environments
- **One-shot mode improvements** — JSON output mode
- **Windows service** — run as a Windows service (in addition to systemd on Linux)
- **macOS launchd plist** — official launchd service definition

## Considering

- **iOS app** — native iOS client (Swift)
- **FUSE mount** — mount a bucket as a local read-only filesystem
- **Snapshot export** — export a bucket at a point in time (read-only copy)
- **Conflict resolution strategies** — configurable strategies beyond the causal tie-break (e.g., always keep local, always keep remote, manual merge tool)
- **GUI mode** — lightweight built-in TUI for headless servers
