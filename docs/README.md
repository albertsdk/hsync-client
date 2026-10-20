# HSYNC Client — Documentation

HSYNC is a file-synchronization client. It keeps a local directory in sync in
real-time either with a central **HSYNC server** (Server Mode) or directly with
other devices (peer-to-peer **p2p Mode**) — on the same local network via mDNS,
or across networks through an optional bootnode. It ships as a
single self-contained binary — no Python runtime is required to run it.

## Start here

This guide is organised as a linear journey; pick the step that matches where
you are.

### 1. Install

Pick a method in the [Getting Started](getting-started.md) chooser (installer
script, standalone binary, `.deb`, WD My Cloud OS5, or from source); the asset
names and checksums are in [Installation](installation.md).

- [`.deb` package guide](debian-package.md) — systemd service, custom paths, ACLs.
- [Migration Guide](migration.md) — coming from rsync, Syncthing, or manual copies.

### 2. Choose a mode and run your first sync

HSYNC runs in two mutually exclusive modes. The [Getting Started](getting-started.md)
chooser picks the right path; each path is a complete, copy-paste walkthrough:

| Mode | Walkthrough | Best for |
|------|-------------|----------|
| **Server Mode** (central server) | [Getting Started — Server Mode](getting-started-server.md) | remote / internet sync, multiple sites |
| **p2p Mode** (peer-to-peer) | [Getting Started — p2p Mode](getting-started-p2p.md) | devices on the same network (mDNS) or across networks via a bootnode; no server |

For the concepts behind each mode (how they work, security, master election),
see [Server Mode](server-mode.md) and [p2p Mode](p2p-mode.md).

### 3. Configure

- [Configuration](configuration.md) — shared options: sync, compression, delta, logging, auto-tuning.
- [Configuration — Server Mode](configuration-server.md) — server URL, API key, SSL/TLS, env vars.
- [Configuration — p2p Mode](configuration-p2p.md) — discovery, master election, `HSYNC_P2P_KEY`, stability.
- [Sync Modes](sync-modes.md) — server vs p2p topologies and how to combine flags.

### 4. Choose the p2p discovery scope

mDNS discovery on the local network is **always on**; a bootnode only adds
cross-network discovery on top of it.

| Task | Guide |
|------|-------|
| Sync devices on the **same network** (mDNS only, no bootnode) | [Sync on the local network](p2p-lan.md) |
| Sync across networks through a **bootnode** (mDNS stays on) | [Sync across networks](p2p-bootnode.md) |

### 5. Operate

| Task | Guide |
|------|-------|
| Practical recipes (backup, mirror, multi-device, bandwidth) | [Examples](examples.md) → `examples-server.md` / `examples-p2p.md` |
| Schedule one-shot syncs (cron, systemd timers, alerts) | [Scheduling](scheduling.md) |
| Read the client logs | [Log Labels](log-labels.md) |
| Troubleshooting | [Troubleshooting](troubleshooting.md) — [FAQ](faq.md) (common + `faq-server.md` / `faq-p2p.md`) |

### 6. Understand

- [Authentication](authentication.md) — API key, P2P_KEY, hello handshake.
- [Sync Flow](sync-flow.md) — client-side flow, split by mode.
- [Conflict Resolution](conflict-resolution.md) — causal model and decision trees.

### 7. Reference & internals

- [Usage](usage.md) — CLI syntax and behaviour (symlinks, error handling, auto-tuning).
- [Performance](performance.md) — measured throughput by mode, workload and phase.
- [Internal architecture](internal/architecture.md) — how the client is structured.
- [p2p protocol internals](internal/p2p-internals.md) — deep P2P details.
- [Auto-tuning internals](internal/autotune.md) — environment-adaptive defaults.
- [Developer Guide](internal/developer.md) — for contributors.
- [Test Suite Guide](internal/testing.md) — test structure, fixtures, cross-project testing.
- [Stress Test Plan](internal/stress-test-plan.md) — load/replication stress harness for p2p and server mode (design + resume checklist).

## Associated Ecosystem Components

> The other HSYNC ecosystem components are listed in the
> [project README](../README.md#associated-ecosystem-components).
