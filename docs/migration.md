# Migrating to HSYNC

This guide helps you move from other file synchronization tools to HSYNC.
Each section covers a specific source tool with step-by-step instructions
and caveats to watch for.

## Table of Contents

- [From rsync](#from-rsync)
- [From Syncthing](#from-syncthing)
- [From cloud storage (Dropbox, Google Drive, OneDrive)](#from-cloud-storage-dropbox-google-drive-onedrive)
- [From manual copies (scp, USB drives)](#from-manual-copies-scp-usb-drives)
- [General migration checklist](#general-migration-checklist)

---

## From rsync

### Key differences

| rsync | HSYNC |
|-------|-------|
| Push or pull on demand | Continuous bidirectional sync |
| Connects via SSH or rsync daemon | Connects via HTTP/HTTPS only |
| No conflict handling (overwrites) | Three-way merge with conflict preservation |
| Single pair of hosts | Multiple clients via central server or p2p |
| No file watching | Real-time watchdog with debouncing |

### Migration steps

1. **Set up the HSYNC Server** following the [Deployment Guide](https://github.com/albertsdk/hsync-server/blob/main/docs/deployment.md).

2. **Choose the source of truth**. Pick one device whose files represent the
   current state you want to keep.

3. **Create a bucket and upload from the source device**:
   ```bash
   hsync server server.example.com -b mybucket -p /path/to/files
   ```

4. **Connect remaining devices**. On each additional device, point HSYNC to an
   **empty directory** and let it download everything:
   ```bash
   hsync server server.example.com -b mybucket -p /path/to/empty/dir
   ```
   Using an empty directory on new peers prevents accidental overwrites.

5. **Replace your rsync cron job or script** with HSYNC in continuous mode:
   ```bash
   hsync server server.example.com -b mybucket -p /path/to/files
   ```

### Caveats

- rsync's `--delete` flag behavior is closest to HSYNC's default bidirectional
  mode. If you previously used `--delete`, tombstones will handle deletions
  correctly for offline peers.
- rsync's `--exclude` patterns translate to `.hsyncignore` rules
  (gitignore syntax).
- Bandwidth limits: use `--limit-upload` and `--limit-download`
  instead of rsync's `--bwlimit`.

---

## From Syncthing

### Key differences

| Syncthing | HSYNC |
|-----------|-------|
| P2P only (no central server) | Server mode + p2p mode |
| Custom protocol (BEP) on port 22000 | HTTP/HTTPS on port 9753 or custom port |
| Global discovery via relays | Central server or mDNS p2p discovery |
| File versioning built-in | Conflict files (.hsyncconflict-*) |
| Web GUI for management | CLI + desktop GUI (hsync-desktop) |

### Migration steps

1. **Decide on topology**:
   - **Server Mode** (recommended for a central hub across the internet): set up
     an HSYNC Server
     and connect all devices to it.
   - **p2p Mode** (equivalent to Syncthing's P2P): run `hsync p2p` on
     each device, with the same `HSYNC_P2P_KEY` on every device — the shared key
     authorizes writes (a keyless p2p is read-only, and a keyless bidirectional
     run is refused). Devices on the same network discover each other over mDNS;
     if they are spread across networks, point them at a bootstrap node (see
     [Sync across networks](p2p-bootnode.md)) — same mode, same election, same
     data plane.

2. **Export your Syncthing ignore patterns**. Syncthing's `.stignore` uses a
   similar syntax to `.gitignore`. Convert your patterns to `.hsyncignore`:
   ```bash
   # Syncthing .stignore
   (?d).stfolder
   *.tmp
   /build/

   # HSYNC .hsyncignore (same syntax!)
   .stfolder/
   *.tmp
   /build/
   ```

3. **Choose the source of truth** (the device with the most complete data).

4. **Upload from the source device** and **download to empty directories**
   on other devices (same pattern as rsync migration).

5. **Remove Syncthing**:
   ```bash
   # Stop Syncthing
   systemctl --user stop syncthing
   # Remove the .stfolder marker
   rm -rf /path/to/sync/.stfolder
   ```

### Caveats

- Syncthing's file versioning (staggered, trash can, etc.) has no direct
  equivalent. HSYNC preserves only the conflicting version as a
  `.hsyncconflict-<date>` file. For version history, rely on filesystem
  snapshots or external backup.
- Syncthing's "Receive Only" folder type maps to HSYNC's `--sync-direction
  download-only`.
- p2p Mode does not use Syncthing's global discovery or relays. It discovers
  peers over mDNS on the local network, or across networks through your own
  self-hosted `boot/1` bootnode (see
  [Sync across networks](p2p-bootnode.md)); Server Mode remains the alternative
  when you prefer a central hub.

---

## From cloud storage (Dropbox, Google Drive, OneDrive)

### Key differences

| Cloud storage | HSYNC |
|---------------|-------|
| Files stored on provider's servers | Files stored on your own server |
| Proprietary protocol | Open HTTP/HTTPS protocol |
| Monthly subscription (after free tier) | Self-hosted, no recurring cost |
| Web access + sharing | No built-in web access or sharing |
| File versioning (30-180 days) | Conflict preservation only |

### Migration steps

1. **Download all files from the cloud provider** to a local directory using
   their desktop client or web interface.

2. **Set up HSYNC** and use that directory as the source of truth.

3. **Uninstall the cloud provider's sync client** to avoid conflicts between
   two sync tools watching the same directory.

4. **Set up access from outside your network** by deploying the HSYNC Server
    behind a reverse proxy with a public domain (see
    [Deployment Guide](https://github.com/albertsdk/hsync-server/blob/main/docs/deployment.md)).

### Caveats

- Cloud storage typically provides a web interface and file sharing — HSYNC
  does not. If you need these features, consider running a separate file
  browser (e.g., FileBrowser, Nextcloud) alongside HSYNC.
- Cloud storage's "selective sync" (sync only some folders) maps to HSYNC's
  `--subdir` option:
  ```bash
  hsync server server.example.com -b mybucket -p /path --subdir Documents/
  ```

---

## From manual copies (scp, USB drives)

If you currently sync files by manually copying them (scp, rsync one-offs, USB
drives), HSYNC automates the entire process:

1. **Set up the HSYNC Server** on a machine that's always on (home server, VPS,
   Raspberry Pi).

2. **Upload your files once** from each device. HSYNC handles deduplication
   automatically — files with the same SHA-256 hash are not re-uploaded.

3. **Run in continuous mode** (the default) on each device. HSYNC will detect changes in
   real time and propagate them automatically.

4. **For serverless sync**, use the `p2p` subcommand without a server — devices
   discover each other via mDNS on the same network, or through a bootnode
   across networks, and sync directly.

---

## General migration checklist

- [ ] HSYNC Server is deployed and reachable (Server Mode) or the devices can
  reach each other (p2p Mode, via mDNS on a LAN or a bootnode across networks)
- [ ] Source-of-truth device has been identified
- [ ] Source device has uploaded all files to the bucket
- [ ] Additional devices have downloaded to **empty** directories
- [ ] `.hsyncignore` file is in place (converted from previous tool's exclude
  rules)
- [ ] Old sync tool has been stopped and uninstalled
- [ ] Sync direction is configured correctly (`--sync-direction`)
- [ ] Bandwidth limits are set if needed (`--max-bandwidth-*`)
- [ ] Continuous mode is used on devices that need real-time sync
- [ ] Backup strategy is in place (see [Operations Guide](https://github.com/albertsdk/hsync-server/blob/main/docs/operations.md))

## See also

- [Getting Started](getting-started.md) — build and run your first sync.
- [Configuration](configuration.md) — all CLI options and environment variables.
- [Examples](examples.md) — practical usage scenarios.
- [Sync Modes](sync-modes.md) — Server Mode vs p2p Mode comparison.
