# HSYNC Client Usage Guide

This document provides detailed usage instructions for the HSYNC Client.

## Quick Start

Three steps: **install** a binary (or build it), **configure** the connection,
then **run** a sync. The full walkthrough is in
[Getting Started](getting-started.md); installation options are in
[Installation](installation.md).

## Command Line Interface

### Basic Syntax

HSYNC runs in one of two mutually exclusive operating modes:

```bash
# Server Mode — connect to a central HSYNC server
hsync -s <server_url> -b <bucket_name> [options]

# LAN Mode — peer-to-peer, no server (mDNS discovery, implicit watch)
hsync --lan-mode -b <bucket_name> [options]
```

See [Sync Modes → Operating Modes](sync-modes.md#operating-modes-server-vs-lan)
for when to use each.

### Options Overview

Options are grouped into **Connection**, **Sync**, **LAN**, **Output** and
**General** panels (run `hsync --help` to see them in your terminal).

> For the **complete option reference** (descriptions, defaults and accepted
> values) see [Configuration → CLI Options](configuration.md#cli-options).
> `HSYNC_LAN_KEY` is set via environment variable only.

## Usage Scenarios

For end-user oriented examples — sync between two devices, backup, LAN
P2P, read-only mirror, scripted execution and so on — see the dedicated
[Examples](examples.md) document, split between server-based and LAN
deployments.

## Log Labels

Use `--help-symbols` to display this legend in the terminal.

### Sync Operations

| Label | Meaning | Description |
|-------|---------|-------------|
| `[IN SYNC]` | In sync | File already in sync (no action required) |
| `[SKIP]` | Skip | File skipped |
| `[DOWNLOAD]` | Download | Downloading from server / master |
| `[UPLOAD]` | Upload | Uploading to server / master |
| `[RESUME]` | Resume | Resuming partial upload or download |
| `[MOVE]` | Move | Renaming / moving file or directory |
| `[REMOVE]` | Remove | Removing file or directory |
| `[CREATE]` | Create | Creating directory |
| `[DELETE]` | Delete | Deleting local file not present on master (LAN mode) |
| `[PERMISSION]` | Permissions | Setting file / directory permissions |
| `[SYMLINK]` | Symlink | Creating symbolic link |
| `[IGNORE DEL]` | Ignore delete | Deletion ignored (backup / ignore-delete mode) |
| `[CONFLICT]` | Conflict | File conflict detected (three-way merge) |
| `[GUARD]` | Guard | Mass-deletion guard tripped (deletions paused) |
| `[TOMBSTONE]` | Tombstone | File deleted remotely while offline; local copy removed |
| `[SYNCED]` | Synced | File is in sync (preview / dry-run) |
| `[TO DOWNLOAD]` | To download | File would be downloaded (preview / dry-run) |
| `[TO UPLOAD]` | To upload | File would be uploaded (preview / dry-run) |
| `[TO DELETE]` | To delete | File would be deleted (preview / dry-run) |
| `[TO RENAME]` | To rename | File would be renamed (preview / dry-run) |

### LAN Client Operations

| Label | Meaning | Description |
|-------|---------|-------------|
| `[CLIENT UP]` | Client upload | Client uploads file to master |
| `[CLIENT DEL]` | Client delete | Client deletes file on master |
| `[CLIENT REN]` | Client rename | Client renames file on master |
| `[CLIENT DIR]` | Client dir | Client creates directory on master |
| `[CLIENT KEEP]` | Client sentinel | Client creates sentinel file (`.hsynckeep`) |
| `[CLIENT IGN]` | Client ignore | Client ignores deletion (ignore-delete mode) |
| `[CLIENT NEW]` | Client new | Client creates a new file on master |
| `[LAN MODIFY]` | LAN modify | LAN master broadcasts a file `modified` event (slaves pull the new bytes). The trigger `(closed)`/`(modified)` shows whether it fired from `IN_CLOSE_WRITE` (reliable) or the `on_modified` debounce fallback |

Log levels (ERROR, WARNING, INFO, DEBUG) are shown by the logging system.

## Error Handling

Errors are handled per item, so one failure never aborts the whole run: network
errors are retried with backoff (then marked failed), authentication errors abort
early, missing files are skipped and permission issues are logged as warnings.
The full decision flow is documented in
[Architecture → Error Handling & Retries](internal/architecture.md#error-handling--retries).

## Auto-Tuning

HSYNC automatically tunes HTTP timeout, retry backoff, and transfer
concurrency based on the runtime environment (network latency, bandwidth,
storage type). This is enabled by default and requires no configuration.

### How it works

At startup the client probes the environment:

- **Network latency** (TCP connect RTT to the server/LAN master)
- **Bandwidth** (passive EMA from real transfers; optional active probe)
- **Storage type** (HDD / SSD / NVMe, probed from the sync root)
- **Transfer-queue profile** (re-evaluated each sync cycle, zero I/O)

The derived values are used as defaults when the user has not set an explicit
override. Per-cycle concurrency is refreshed from the live queue shape.

### Resolution chain

```
CLI flag  >  HSYNC_* env var  >  auto-tuned value  >  static default
```

Set any of `HSYNC_HTTP_TIMEOUT`, `HSYNC_RECONNECT_DELAY`,
`HSYNC_MAX_CONCURRENT_TRANSFERS`, `HSYNC_TRANSFER_RETRY_MIN_WAIT`, or
`HSYNC_TRANSFER_RETRY_MAX_WAIT` to override the auto-tuned value. Use
`--no-autotune` or `HSYNC_AUTOTUNE=false` to disable auto-tuning entirely
and use the built-in static defaults.

### Tuned parameters

| Parameter | Static default | Auto-tuned range |
| --------- | -------------- | ---------------- |
| HTTP timeout | 30 s | 30 / 45 / 60 s (based on RTT) |
| Retry min wait | 1 s | 5–15 s (scales with RTT) |
| Retry max wait | 10 s | 10–30 s (scales with RTT) |
| Reconnect delay | 5 s | 5–15 s (scales with RTT) |
| Concurrency | 5 | 5–20 (based on storage + bandwidth + queue) |

See [Configuration → Environment Variables](configuration.md#environment-variables)
for the full reference.

## Environment Variables

Environment variables and the provided `dev.env` / `prod.env` templates are
documented in the single configuration reference:

- [Configuration → Environment Variables](configuration.md#environment-variables)
- [Configuration → Environment Files](configuration.md#environment-files)

## Examples

End-user oriented examples have moved to a dedicated document:

- [Examples](examples.md) — practical scenarios grouped by deployment
  model (server-based and LAN P2P), including syncing a folder between
  two devices, push-only backups, read-only mirrors, scripted /
  unattended execution, bandwidth tuning and more.

> **Note**: In LAN mode, `--force-master` cannot be combined with `--sync-direction upload-only` or `download-only` (the client will exit with an error). During automatic election, bidirectional peers are preferred as master. Use `--never-master` to keep a peer out of the election entirely (incompatible with `--force-master`); if every peer is `--never-master`, no master is elected and a `[NO MASTER]` warning is logged after 60 s. See [LAN Mode > Master Election](lan-mode.md#master-election) for the full election algorithm.
