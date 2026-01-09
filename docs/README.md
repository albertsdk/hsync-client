# HSYNC Client — Documentation

HSYNC is a file-synchronization client. It keeps a local directory in sync in
real-time either with a central **HSYNC server** (Server Mode) or directly with
other devices on the same network (peer-to-peer **LAN Mode**). It ships as a
single self-contained binary — no Python runtime is required to run it.

## Start here

This guide is organised as a linear journey; pick the step that matches where
you are.

### 1. Install

Download the installer script, a prebuilt binary, `.deb`, or WD OS5 `.bin` from
the [Releases page](https://github.com/albertobasso/hsync-client/releases), or
build it from source — full instructions are in [Installation](installation.md).

- [`.deb` package guide](debian-package.md) — systemd service, custom paths, ACLs.
- [Migration Guide](migration.md) — coming from rsync, Syncthing, or manual copies.

### 2. Choose a mode and run your first sync

HSYNC runs in two mutually exclusive modes. The [Getting Started](getting-started.md)
chooser picks the right path; each path is a complete, copy-paste walkthrough:

| Mode | Walkthrough | Best for |
|------|-------------|----------|
| **Server Mode** (central server) | [Getting Started — Server Mode](getting-started-server.md) | remote / internet sync, multiple sites |
| **LAN Mode** (peer-to-peer) | [Getting Started — LAN P2P Mode](getting-started-lan.md) | devices on the same network, no server |

For the concepts behind each mode (how they work, security, master election),
see [Server Mode](server-mode.md) and [LAN Mode](lan-mode.md).

### 3. Configure

- [Configuration](configuration.md) — shared options: sync, compression, delta, logging, auto-tuning.
- [Configuration — Server Mode](configuration-server.md) — server URL, API key, SSL/TLS, env vars.
- [Configuration — LAN P2P Mode](configuration-lan.md) — discovery, master election, `HSYNC_LAN_KEY`, stability.
- [Sync Modes](sync-modes.md) — server vs LAN topologies and how to combine flags.

### 4. Operate

| Task | Guide |
|------|-------|
| Practical recipes (backup, mirror, multi-device, bandwidth) | [Examples](examples.md) → `examples-server.md` / `examples-lan.md` |
| Schedule one-shot syncs (cron, systemd timers, alerts) | [Scheduling](scheduling.md) |
| Read the client logs | [Log Labels](log-labels.md) |
| Troubleshooting | [FAQ](faq.md) → `faq-server.md` / `faq-lan.md` |

### 5. Understand

- [Authentication](authentication.md) — API key, LAN_KEY, hello handshake.
- [Sync Flow](sync-flow.md) — client-side flow, split by mode.
- [Conflict Resolution](conflict-resolution.md) — causal model and decision trees.

### 6. Reference & internals

- [Usage](usage.md) — CLI syntax and behaviour (symlinks, error handling, auto-tuning).
- [Internal architecture](internal/architecture.md) — how the client is structured.
- [LAN protocol internals](internal/lan-internals.md) — deep P2P details.
- [Auto-tuning internals](internal/autotune.md) — environment-adaptive defaults.
- [Developer Guide](internal/developer.md) — for contributors.
- [Test Suite Guide](internal/testing.md) — test structure, fixtures, cross-project testing.

## Related projects

- [HSYNC Common](https://github.com/albertobasso/hsync-common/blob/main/README.md) - Shared models, constants, and utilities used by the client
- [HSYNC Server](https://github.com/albertobasso/hsync-server/blob/main/README.md) - Central synchronization server (Server Mode)
- [HSYNC Desktop](https://github.com/albertobasso/hsync-desktop/blob/main/docs/README.md) — Desktop client GUI front-end *(in development)*.
- [HSYNC Android](https://github.com/albertobasso/hsync-android/blob/main/docs/README.md) — Android client app *(in development)*.

> **Note:** The desktop GUI (`HSYNC Desktop`) and the Android app (`HSYNC Android`)
> are still under active development and are not yet ready for production use.
