# HSYNC Client — Documentation

HSYNC is a file-synchronization client. It keeps a local directory in sync in
real-time either with a central **HSYNC server** (Server Mode) or directly with
other devices on the same network (peer-to-peer **LAN Mode**). It ships as a
single self-contained binary —
no Python runtime is required to run it.

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
| [getting-started.md](getting-started.md) | Build, choose a mode, and pick your path (Server or LAN P2P). |
| [getting-started-server.md](getting-started-server.md) | Server Mode quickstart: connect to a central server. |
| [getting-started-lan.md](getting-started-lan.md) | LAN P2P quickstart: sync directly with peers on your network. |
| [migration.md](migration.md) | Migrate to HSYNC from rsync, Syncthing, cloud storage, or manual copies. |

### Usage & Configuration

| Document | What it covers |
| -------- | -------------- |
| [usage.md](usage.md) | Complete command-line reference and common workflows. |
| [configuration.md](configuration.md) | Shared options: sync, compression, delta, logging, auto-tuning. |
| [configuration-server.md](configuration-server.md) | Server-mode options: server URL, API key, SSL/TLS, environment variables. |
| [configuration-lan.md](configuration-lan.md) | LAN P2P options: discovery, master election, LAN_KEY, file stability. |
| [sync-modes.md](sync-modes.md) | Index: Server vs LAN modes (split into separate guides). |
| [sync-modes-server.md](sync-modes-server.md) | Server Mode: sync directions, watch mode, single sync, dry run, conflict handling. |
| [sync-modes-lan.md](sync-modes-lan.md) | LAN P2P: master election, sync directions, watch mode, conflict handling. |
| [server-mode.md](server-mode.md) | Server mode guide: how it works, quick start, security, and troubleshooting. |
| [lan-mode.md](lan-mode.md) | Peer-to-peer LAN guide: how it works, master election, and pairing. |
| [examples.md](examples.md) | Index: practical usage recipes (split by mode). |
| [examples-server.md](examples-server.md) | Server Mode examples: sync, backup, multi-device, bandwidth-limited. |
| [examples-lan.md](examples-lan.md) | LAN P2P examples: Wi-Fi sync, NAS backup, groups, fixed ports. |
| [log-labels.md](log-labels.md) | Every action label that can appear in the logs and what it means. |

### How It Works

| Document | What it covers |
| -------- | -------------- |
| [authentication.md](authentication.md) | Authentication flows: Server Mode (API key), LAN P2P with/without LAN_KEY, hello handshake. |
| [sync-flow.md](sync-flow.md) | Index: client-side sync flow (split into separate guides). |
| [sync-flow-server.md](sync-flow-server.md) | Server Mode sync flow: connection, watch mode, event loop, conflict handling. |
| [sync-flow-lan.md](sync-flow-lan.md) | LAN P2P sync flow: master election, WebSocket hub, echo suppression, slave upload. |
| [faq.md](faq.md) | Index: frequently asked questions (split by mode). |
| [faq-server.md](faq-server.md) | Server Mode FAQ: security, sync behavior, files, performance, Debian package. |
| [faq-lan.md](faq-lan.md) | LAN P2P FAQ: master election, groups, ports, hello handshake, authentication. |

### Internal / For Contributors

| Document | What it covers |
| -------- | -------------- |
| [internal/architecture.md](internal/architecture.md) | How the client is structured internally. |
| [conflict-resolution.md](conflict-resolution.md) | Causal conflict model (ADR-0022): decision tree of all cases, version store, conflicted-copy handling, upload arbitration on server/LAN master, and causal tombstones (ADR-0003) — with diagrams. |
| [internal/lan-internals.md](internal/lan-internals.md) | Deep LAN protocol internals: diagrams, HTTP/WS API, echo suppression. |
| [internal/developer.md](internal/developer.md) | Internal implementation details: module structure, performance optimizations, error handling. |
| [internal/testing.md](internal/testing.md) | Test suite guide: structure, fixtures, mocking, cross-project testing. |
| [internal/autotune.md](internal/autotune.md) | Auto-tuning subsystem: environment-adaptive defaults for HTTP timeout, retry delay, and transfer concurrency. |

## Related projects

- [HSYNC Common](https://github.com/albertobasso/hsync-common/blob/main/README.md) - Shared models, constants, and utilities used by the client
- [HSYNC Server](https://github.com/albertobasso/hsync-server/blob/main/README.md) - Central synchronization server (Server Mode)
- [HSYNC Desktop](https://github.com/albertobasso/hsync-desktop/blob/main/docs/README.md) — Desktop client GUI front-end *(in development)*.
- [HSYNC Android](https://github.com/albertobasso/hsync-android/blob/main/docs/README.md) — Android client app *(in development)*.

> **Note:** The desktop GUI (`HSYNC Desktop`) and the Android app (`HSYNC Android`)
> are still under active development and are not yet ready for production use.
