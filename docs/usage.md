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

```text
# Server Mode — connect to a central HSYNC server
hsync server HOSTNAME[:PORT] [--port PORT] -b BUCKET_NAME [OPTIONS]

# p2p Mode — peer-to-peer, no server (implicit continuous mode).
# Write-capable runs need a shared key: a keyless p2p is read-only.
HSYNC_P2P_KEY=SHARED_SECRET hsync p2p -b BUCKET_NAME [OPTIONS]

# p2p Mode across networks — discover peers through a boot/1 bootstrap node
# instead of mDNS (see "Sync across networks").
hsync p2p -b BUCKET_NAME [BOOTNODE_HOST[:PORT]] [OPTIONS]
```

With no `BOOTNODE` hostname (positional) and no `HSYNC_BOOTNODE`, `hsync p2p`
uses the mDNS plan on the local network; with a bootnode it discovers peers
across networks. See [Sync on the local network](p2p-lan.md) and
[Sync across networks](p2p-bootnode.md).

See [Sync Modes](sync-modes.md) for when to use each.

### Options Overview

Options are grouped into **Required**, **Connection**, **Sync**, **p2p Mode**
and **Output** panels (run `hsync --help` to see them in your terminal).

> For the **complete option reference** (descriptions, defaults and accepted
> values) see [Configuration](configuration.md). `HSYNC_P2P_KEY` is set via
> environment variable only, and it is what authorizes writes: a keyless p2p is
> read-only, and a write-capable run without a key is refused with
> `[AUTH REQUIRED]`.

## Usage Scenarios

For end-user oriented examples — sync between two devices, backup, p2p,
read-only mirror, scripted execution and so on — see the dedicated
[Examples](examples.md) document, split between server-based and p2p
deployments.

## Stopping a Run

A continuous run ends gracefully on `Ctrl+C` **and** on `SIGTERM`, which is what
`systemctl stop`, `docker stop`, a container runtime or the HSYNC Desktop Stop
button sends. The client stops its servers and watchers, flushes the log and
exits with code `0` — the same teardown as an interactive interruption. Only a
`SIGKILL` (`kill -9`), which no process can handle, skips that teardown.

## Log Labels

`hsync-client` tags every log line with a bracketed action label
(e.g. `[DOWNLOAD]`, `[NO MASTER]`) to show what kind of operation it is. Use
`--help-symbols` to print the legend in the terminal.

The complete list of labels, their color category, and what each means is in
the dedicated [Log Labels reference](log-labels.md). Log levels (ERROR,
WARNING, INFO, DEBUG) are shown by the logging system.

## Error Handling

Errors are handled per item, so one failure never aborts the whole run: network
errors are retried with backoff (then marked failed), authentication errors abort
early, missing files are skipped and permission issues are logged as warnings.
The full decision flow is documented in
[Developer Guide → Error Handling & Retries](internal/developer.md#error-handling--retries).

## Auto-Tuning

HSYNC automatically tunes HTTP timeout, retry backoff, and transfer
concurrency based on the runtime environment (network latency, bandwidth,
storage type). This is enabled by default and requires no configuration.

> The full explanation — probing, the resolution chain (`CLI flag > env var >
> auto-tuned value > static default`), and the tuned parameter ranges — is in
> [Auto-Tuning internals](internal/autotune.md); the user-facing knobs are in
> [Configuration → Auto-Tuning](configuration.md#auto-tuning).

## Environment Variables

Environment variables and the provided `dev.env.example` / `prod.env.example`
templates are documented in the single configuration reference:

- [Configuration](configuration.md) — shared options and `HSYNC_*` variables
- [Configuration → Environment files](configuration.md#environment-files)
- [Configuration — Server Mode](configuration-server.md#environment-variables--server-mode) / [Configuration — p2p Mode](configuration-p2p.md#environment-variables--p2p-mode) — per-mode variables

## Symlinks

Symbolic links are **replicated structurally**, both on the first sync and
during continuous operation, in server mode and p2p mode alike:

- A symlink whose target stays inside the synced folder is copied **as a
  link**: the target is not followed and its content is not transferred.
  The target path is stored relative to the link, so every peer recreates a
  valid link regardless of where the folder is mounted.
- A symlink whose target points outside the synced folder is **never
  replicated**: it is skipped and a `[SECURITY]` warning is logged.
- Deleting or renaming a synced link removes/moves the **link**, never the
  target it points to.

> Technical readers: this is the same model as `rsync --links` with munched
> symlinks, and `git`: the reference is synchronized, not the content.

## Examples

End-user oriented examples have moved to a dedicated document:

- [Examples](examples.md) — practical scenarios grouped by deployment
  model (server-based and p2p), including syncing a folder between
  two devices, push-only backups, read-only mirrors, scripted /
  unattended execution, bandwidth tuning and more.

> **Note**: In p2p mode, `--prefer-master` cannot be combined with `--sync-direction upload-only` or `download-only` (the client will exit with an error). During automatic election, bidirectional peers are preferred as master. Use `--never-master` to keep a peer out of the election entirely (incompatible with `--prefer-master`); if every peer is `--never-master`, no master is elected and a `[NO MASTER]` warning is logged after 60 s. See [p2p Mode > Master Election](p2p-mode.md#master-election) for the full election algorithm.
