# Frequently Asked Questions (Client)

Common questions about the HSYNC client. For deeper detail each answer links to
the relevant guide.

## General

### Why choose HSYNC over other file-sync tools?

- **No runtime to install.** HSYNC ships as a single self-contained binary —
  there is no Python, Java, or Node dependency to manage on the target machine.
  See [Installation](installation.md).
- **Two topologies, one tool.** Use a central **Server Mode** hub for remote /
  multi-site sync, or serverless peer-to-peer **LAN Mode** that needs no
  infrastructure at all. See [Sync Modes](sync-modes.md).
- **Secure by default.** Server Mode requires an API key transmitted only over
  HTTPS; LAN Mode runs over mandatory HTTPS/WSS with certificate pinning (TOFU).
  See [SSL/TLS](configuration.md#ssltls-httpswss).
- **Real-time *and* resilient.** Watch mode propagates changes instantly, while a
  periodic full resync acts as a safety net for any missed file-system events.
  See [Watch Mode](sync-modes.md#watch-mode--w).
- **Safe with your data.** Deletions can be ignored (backup mode), a pre-sync
  safety check guards against mass-deletion, and transfers are resumable with
  SHA-256 integrity verification.
- **Open ecosystem.** The same engine powers the CLI, a desktop GUI, an Android
  app, and ships ready for WD My Cloud OS5 NAS devices.

### Do I need a server to use HSYNC?

No. **LAN Mode** (`--lan-mode`) synchronizes devices on the same network
peer-to-peer with no server: peers find each other via mDNS and elect a master
automatically. A server is only needed for remote / cross-network sync. See
[LAN Mode](lan-mode.md).

### Which platforms are supported?

Prebuilt binaries, a Debian `.deb`, and a WD OS5 `.bin` are published on the
releases page; you can also build from source. See [Installation](installation.md).

## Security & Safety

### Is my data encrypted in transit?

Yes. Server Mode transmits the API key and data only over HTTPS/WSS, and LAN Mode
mandates HTTPS/WSS with certificate pinning. See
[SSL/TLS](configuration.md#ssltls-httpswss).

### What stops a stranger on my network from syncing my files?

In LAN Mode set a shared secret via `HSYNC_LAN_KEY`, and optionally enforce
strict pairing with `--require-pairing` to refuse first-seen master certificates
until confirmed out of band. See [LAN Mode Options](configuration.md#lan-mode-options).

### Will HSYNC delete my files if I remove them remotely?

By default deletions are propagated. To keep a destination that never deletes
(pure backup), set `HSYNC_IGNORE_DELETE=true`. A built-in
[pre-sync safety check](architecture.md#pre-sync-safety-check-deletion-risk)
also aborts when an unusually large deletion is detected.

## Sync Behavior

### What happens if two devices edit the same file?

Sync direction and mode determine the outcome; see
[Sync Direction](sync-modes.md#sync-direction) and [Sync Modes](sync-modes.md).
For one-way safety pick `upload-only` or `download-only`.

### Can I sync in only one direction?

Yes — `--sync-direction upload-only` pushes local → remote, `download-only`
mirrors remote → local, and the default is `bidirectional`. See
[Sync Direction](sync-modes.md#sync-direction).

### How do I keep changes syncing continuously?

Run with `-w` (watch mode) to maintain a live connection and propagate changes in
real time; a periodic full resync (`HSYNC_RESYNC`) recovers any missed events.
See [Watch Mode](sync-modes.md#watch-mode--w).

### Can I preview what would happen before syncing?

Yes — use `-n` (dry run) to list the operations without changing anything. See
[Dry Run](sync-modes.md#dry-run).

### How do I exclude files from syncing?

Add patterns to a `.hsyncignore` file (gitignore-style), pass extra patterns via
the CLI, or set `HSYNC_IGNORE_PATTERNS`. See
[Ignore Files](configuration.md#ignore-files-hsyncignore).

## LAN Mode

### How is the "master" chosen?

By priority: `--force-master` > bidirectional peer > earliest startup > lowest
CID; peers started with `--never-master` are excluded. See
[LAN Mode](lan-mode.md).

### What if no device can become master?

If **every** discovered peer is `--never-master`, no master is elected and a
parsable `[NO_MASTER]` warning is logged after a grace period. See
[Incompatibilities & Priority](configuration.md#incompatibilities--priority).

### Can I keep separate sync groups on the same network?

Yes — use `--lan-group` / `HSYNC_LAN_GROUP` so peers only pair within the same
group name. See [LAN Mode Options](configuration.md#lan-mode-options).

## Files & Performance

### Are large files and interrupted transfers handled safely?

Yes — uploads are resumable and verified with SHA-256, so an interrupted transfer
resumes from where it stopped rather than restarting. See
[Architecture](architecture.md).

### Can I limit how much bandwidth HSYNC uses?

Yes — cap upload and/or download throughput. See
[Bandwidth Limiting](configuration.md#bandwidth-limiting).

### Does HSYNC compress data to save bandwidth?

Yes — eligible files are compressed automatically above a size threshold; already
compressed formats are skipped. See
[File Compression](configuration.md#file-compression).

### Sync seems to miss changes on Linux — what should I check?

Large directory trees can exceed the inotify watch limit. Raise
`fs.inotify.max_user_watches`; the client warns at startup when the limit is
approached. See
[inotify watch limit](configuration.md#periodic-resync-and-the-inotify-watch-limit-linux).

## Debian Package

### How do I customise the sync path for the systemd service?

See the [Debian Package Guide](debian-package.md#customising-the-sync-path) for
the full procedure (editing the config, granting ACLs, updating systemd
sandboxing, and restarting the service).

### Why does the service report "Directory '…' does not exist" for a path under /home?

This is caused by `ProtectHome=yes` (an older package default) or missing
`x` (traverse) permissions on parent directories. See
[ProtectHome and directory visibility](debian-package.md#protecthome-and-directory-visibility)
in the Debian Package Guide.

### Why can't the service write to my chosen directory?

Two layers control write access: systemd sandboxing (`ReadWritePaths`) and
POSIX permissions / ACLs. Both must be satisfied. See
[Understanding write access](debian-package.md#understanding-write-access) in
the Debian Package Guide.

### Can I run the service as my own user instead of _hsync?

Yes. See [Running as a different user](debian-package.md#running-as-a-different-user)
in the Debian Package Guide.

## See also

- [Getting Started](getting-started.md) — first sync walkthrough.
- [Usage](usage.md) — full command-line reference.
- [Configuration](configuration.md) — every CLI option and environment variable.
- [Debian Package Guide](debian-package.md) — detailed guide for the `.deb` package.
