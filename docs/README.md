# HSYNC Client — Documentation

HSYNC is a file-synchronization client. It keeps a local directory in sync either
with a central **HSYNC server** (Server Mode) or directly with other devices on
the same network (peer-to-peer **LAN Mode**). It ships as a single self-contained
binary — no Python runtime is required to run it.

## Install & quickstart

Download the installer script, a prebuilt binary, `.deb`, or WD OS5 `.bin` from
the [Releases page](https://github.com/albertobasso/hsync-client/releases), or
build it from source — full instructions are in [Installation](installation.md).

Once you have a working `hsync` binary, run a one-off sync against a server, or
peer-to-peer on the LAN:

```bash
# Server Mode (needs HSYNC_API_KEY)
hsync -s https://server:9753 -b mybucket -p /path/to/dir

# LAN Mode (peers discover each other via mDNS)
hsync -b mybucket -p /path/to/dir --lan-mode
```

See [Getting Started](getting-started.md) for the full walkthrough.

## Where to start

- **Installing for the first time?** → Start with [Installation](installation.md).
- **First sync?** → Follow the [Getting Started](getting-started.md) guide.
- **Coming from another tool?** → See the [Migration Guide](migration.md).
- **All CLI options?** → Check [Configuration](configuration.md).
- **Using a central server?** → Read [Server Mode](server-mode.md).
- **Peer-to-peer on the LAN?** → Read [LAN Mode](lan-mode.md).
- **Contributing?** → See the [Developer Guide](internal/developer.md).

## Documentation map

### Getting Started

| Document | What it covers |
| -------- | -------------- |
| [installation.md](installation.md) | Install via the installer script, prebuilt binary, `.deb`, or `.bin`, or build from source. |
| [debian-package.md](debian-package.md) | Detailed guide for the `.deb` package: service, custom paths, ACLs. |
| [getting-started.md](getting-started.md) | Build, choose a mode, and run your first sync. |
| [migration.md](migration.md) | Migrate to HSYNC from rsync, Syncthing, cloud storage, or manual copies. |

### Usage & Configuration

| Document | What it covers |
| -------- | -------------- |
| [usage.md](usage.md) | Complete command-line reference and common workflows. |
| [configuration.md](configuration.md) | Single reference for every CLI option and environment variable. |
| [sync-modes.md](sync-modes.md) | Server vs LAN modes, sync directions, and conflict handling. |
| [server-mode.md](server-mode.md) | Server mode guide: how it works, quick start, security, and troubleshooting. |
| [lan-mode.md](lan-mode.md) | Peer-to-peer LAN guide: how it works, master election, and pairing. |
| [examples.md](examples.md) | Practical, copy-paste usage recipes. |

### How It Works

| Document | What it covers |
| -------- | -------------- |
| [sync-flow.md](sync-flow.md) | Client-side sync flow: initial sync, watch mode, event queue, retry. |
| [faq.md](faq.md) | Frequently asked questions and why to choose HSYNC. |

### Internal / For Contributors

| Document | What it covers |
| -------- | -------------- |
| [internal/architecture.md](internal/architecture.md) | How the client is structured internally. |
| [internal/conflict-resolution.md](conflict-resolution.md) | Three-way merge algorithm, version store, and conflict handling. |
| [internal/lan-internals.md](internal/lan-internals.md) | Deep LAN protocol internals: diagrams, HTTP/WS API, echo suppression. |
| [internal/developer.md](internal/developer.md) | Internal implementation details: module structure, performance optimizations, error handling. |
| [internal/testing.md](internal/testing.md) | Test suite guide: structure, fixtures, mocking, cross-project testing. |
| [internal/autotune.md](internal/autotune.md) | Auto-tuning subsystem: environment-adaptive defaults for HTTP timeout, retry delay, and transfer concurrency. |

## Related projects

- [HSYNC Common](https://github.com/albertobasso/hsync-common/blob/main/README.md) - Shared models, constants, and utilities used by the client
- [HSYNC Server](https://github.com/albertobasso/hsync-server/blob/main/README.md) - Central synchronization server (Server Mode)
- [HSYNC Desktop](https://github.com/albertobasso/hsync-desktop/blob/main/docs/README.md) — desktop GUI front-end *(in development)*.
- [HSYNC Android](https://github.com/albertobasso/hsync-android/blob/main/docs/README.md) — the Android app *(in development)*.

> **Note:** The desktop GUI (`HSYNC Desktop`) and the Android app (`HSYNC Android`)
> are still under active development and are not yet ready for production use.
