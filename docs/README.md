# HSYNC Client — Documentation

HSYNC is a file-synchronization client. It keeps a local directory in sync either
with a central **HSYNC server** (Server Mode) or directly with other devices on
the same network (peer-to-peer **LAN Mode**). It ships as a single self-contained
binary — no Python runtime is required to run it.

## Install & quickstart

Download a prebuilt binary, `.deb`, or WD OS5 `.bin` from the
[Releases page](https://github.com/albertobasso/hsync-client/releases), or build
it from source — full instructions are in [Installation](installation.md).

Once you have a working `hsync` binary, run a one-off sync against a server, or
peer-to-peer on the LAN:

```bash
# Server Mode (needs HSYNC_API_KEY)
./dist/<platform>/hsync -s https://server:8765 -b mybucket -p /path/to/dir

# LAN Mode (peers discover each other via mDNS)
./dist/<platform>/hsync -b mybucket -p /path/to/dir --lan-mode
```

See [Getting Started](getting-started.md) for the full walkthrough.

## Documentation map

| Document | What it covers |
| -------- | -------------- |
| [installation.md](installation.md) | Install a prebuilt binary, `.deb`, or `.bin`, or build from source. |
| [debian-package.md](debian-package.md) | Detailed guide for the `.deb` package: service, custom paths, ACLs. |
| [getting-started.md](getting-started.md) | Build, choose a mode, and run your first sync. |
| [usage.md](usage.md) | Complete command-line reference and common workflows. |
| [sync-modes.md](sync-modes.md) | Server vs LAN modes, sync directions, and conflict handling. |
| [lan-mode.md](lan-mode.md) | Peer-to-peer LAN guide: how it works, master election, and pairing. |
| [configuration.md](configuration.md) | Single reference for every CLI option and environment variable. |
| [examples.md](examples.md) | Practical, copy-paste usage recipes. |
| [sync-flow.md](sync-flow.md) | Client-side sync flow: initial sync, watch mode, conflict resolution, retry. |
| [faq.md](faq.md) | Frequently asked questions and why to choose HSYNC. |
| [architecture.md](architecture.md) | How the client is structured internally. |
| [lan-internals.md](lan-internals.md) | Deep LAN protocol internals: diagrams, HTTP/WS API, echo suppression. |

## Related projects

- [hsync-server](../../hsync-server/docs/README.md) — the central server.
- [hsync-desktop](../../hsync-desktop/docs/README.md) — desktop GUI front-end.
- [hsync-android](../../hsync-android/docs/README.md) — the Android app.
- [hsync-common](../../hsync-common/docs/README.md) — shared models and protocol.
