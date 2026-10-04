# Getting Started

HSYNC Client keeps a local folder identical with the same folder on your other
devices, in real time: either through your own **HSYNC server** (Server Mode) or
directly device-to-device (p2p Mode). It ships as a single self-contained binary —
no Python runtime is required to run it.

Pick **one** installation method below, follow it to a running client, then
choose the operating mode you want. The methods are ordered from the simplest
and most immediate to the most involved.

| # | Method | Best for | Guide |
| - | ------ | -------- | ----- |
| 1 | **Installer script** (`install.sh` via `curl`) | Least setup on Linux or macOS: one command, verified download | [Installer script](getting-started-script.md) |
| 2 | **Standalone binary** (archive) | You manage the binary and `PATH` yourself (Linux/macOS/Windows) | [Standalone binary](getting-started-binary.md) |
| 3 | **Windows installer** (`.exe`) | A Windows x64 host: per-user install, adds `hsync` to your `PATH` | [Windows installer](getting-started-windows.md) |
| 4 | **Debian/Ubuntu package** (`.deb`) | A Debian/Ubuntu host managed by systemd | [Debian package](getting-started-deb.md) |
| 5 | **RPM package** (`.rpm`) | A RHEL/Rocky/Alma/Fedora host managed by systemd | [RPM package](getting-started-rpm.md) |
| 6 | **WD My Cloud OS5** (`.bin`) | A supported WD My Cloud OS5 NAS | [WD OS5 packaging](../packaging/wd-os5/README.md) |
| 7 | **From source** (clone and build) | Development, or a platform with no prebuilt asset | [Build from source](getting-started-source.md) |

All methods produce the same `hsync` executable; only the toolchain differs. The
asset names for every platform and the checksum file are in
[Installation](installation.md).

## Requirements (all methods)

- To **run** HSYNC you only need the compiled binary — no Python runtime.
- To **build from source** you need Python, Poetry and a sibling `hsync-common`
  clone (and Docker for the cross-arch targets) — see
  [Build from source](getting-started-source.md).

## Then choose an operating mode

HSYNC runs in two mutually exclusive modes, selected per invocation:

| Mode | Topology | How to enable | Best for | Walkthrough |
|------|----------|---------------|----------|-------------|
| **p2p Mode** | Peer-to-peer (no server) | `hsync p2p` | Devices on the same network (mDNS), or across networks through an optional bootnode | [Getting Started — p2p Mode](getting-started-p2p.md) |
| **Server Mode** | Client ↔ central server | `hsync server <hostname>` (+ `HSYNC_API_KEY`) | Remote / internet sync, multiple sites | [Getting Started — Server Mode](getting-started-server.md) |

See [Sync Modes](sync-modes.md) for a full comparison of the two topologies.

## Key terms

A few concepts appear throughout the docs; here is a one-line reference:

| Term | Meaning |
|------|---------|
| **Bucket** | The unit of synchronization: a named set of files that stays in sync across devices. Selected with `-b/--bucket`, mapped to a local directory with `-p/--path`. |
| **Server Mode** | Topology where a central **server** relays sync and keeps a copy of the content; each device runs a **client** that uploads/downloads through it. Enabled with `hsync server`. |
| **p2p Mode** | Serverless topology where peers discover each other — via mDNS on the same local network, or through a `boot/1` bootnode across networks — and elect a **master** that coordinates the sync group; all other peers are **slaves**. Enabled with `hsync p2p`. |
| **Master / Slave** | p2p Mode roles: the master serves files over HTTPS/WSS and coordinates sync; slaves connect to the master. The master is elected automatically (see [p2p Mode](p2p-mode.md#master-election)). |
| **Continuous mode** | Continuous real-time sync, the default in both modes (implicit in p2p Mode). A periodic full resync acts as a safety net for missed events. |
| **Folder marker** | Hidden `.hsyncfolder` file at the bucket root. Identifies the folder so sync state stored outside the bucket can follow a rename or move. Safe to ignore. |

## Next steps

- [Configuration](configuration.md) — all shared CLI options and environment variables
- [Server Mode](server-mode.md) — how Server Mode works (concepts)
- [p2p Mode](p2p-mode.md) — how p2p Mode works (concepts)
- [Examples](examples.md) — practical usage recipes
- [FAQ](faq.md) — common questions
