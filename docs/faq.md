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
  app, and ships ready for WD My Cloud OS5 NAS devices. The desktop GUI and
  Android app are still under development; the CLI is the production-ready
  client today.
- **Safe with your data.** Deletions can be ignored (backup mode), a pre-sync
  safety check guards against mass-deletion, and transfers are resumable with
  SHA-256 integrity verification.
- **Open ecosystem.** The same engine powers the CLI, a desktop GUI, an Android
  app, and ships ready for WD My Cloud OS5 NAS devices. The desktop GUI and
  Android app are still under development; the CLI is the production-ready
  client today.
- **Open ecosystem.** The same engine powers the CLI, a desktop GUI, an Android
  app, and ships ready for WD My Cloud OS5 NAS devices. The desktop GUI and
  Android app are still under development; the CLI is the production-ready
  client today.

### Do I need a server to use HSYNC?

No. **LAN Mode** (`--lan-mode`) synchronizes devices on the same network
peer-to-peer with no server: peers find each other via mDNS and elect a master
automatically. A server is only needed for remote / cross-network sync. See
[LAN Mode](lan-mode.md).

### Which platforms are supported?

Prebuilt binaries, a Debian `.deb`, and a WD OS5 `.bin` are published on the
releases page; you can also build from source. See [Installation](installation.md).

### What are the basic concepts? (bucket, server, client, master, slave)

A **bucket** is the unit of synchronization: a named set of files that stays
in sync across devices. You pick the name with `-b/--bucket` and map it to a
local directory with `-p/--path`. Two devices syncing the same bucket name
keep their directories mirrored.

HSYNC runs in two topologies, each with its own roles:

- **Server Mode** (`-s/--server`): a central **server** stores the canonical
  copy; each device runs a **client** that uploads/downloads from it. The
  server is the hub; clients authenticate with an API key. Best for remote /
  multi-site sync.
- **LAN Mode** (`--lan-mode`): no server — peers discover each other via
  mDNS and elect one peer as the **master** (the source of truth); all other
  peers are **slaves** that sync with the master. Best for devices on the
  same network.

See [Sync Modes](sync-modes.md#operating-modes-server-vs-lan) for the full
comparison and [LAN Mode](lan-mode.md) for the P2P architecture and master
election.

### Can I run multiple hsync instances on the same machine?

Yes. Each invocation is independent: pick a different bucket name (`-b`) and a
different local directory (`-p`) for each instance. In LAN Mode, use a distinct
`--lan-group` per instance so the peers don't pair with each other. Each
instance gets its own `.hsyncstate` / `.hsynctombstones` side-cars (keyed by
bucket directory) and its own LAN master certificate when needed.

### How do I update HSYNC to a new version?

Re-run the [installer script](installation.md#installer-script-recommended) for
the new version — it overwrites the binary in place while preserving the
install directory. For the `.deb` package, upgrade with
`sudo dpkg -i hsync-client_<version>_*.deb`. The `.hsyncstate` and
`.hsynctombstones` side-cars are forward-compatible across versions, so no
re-sync is needed after an update. See [Installation](installation.md).

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
[pre-sync safety check](internal/architecture.md#pre-sync-safety-check-deletion-risk)
also aborts when an unusually large deletion is detected.

### Should I disable the mass-deletion guard in CI/CD pipelines?

**Yes** — in CI/CD pipelines, automation scripts and ephemeral/automated
deployments it is recommended to pass `--no-mass-delete-guard` (or set
`HSYNC_MASS_DELETE_GUARD=false`).

In those environments it is perfectly normal to wipe entire directories —
build folders, `node_modules`, cache directories — and recreate them from
scratch on every run. If the guard is left active, the first deep-clean cycle
looks exactly like a suspicious mass deletion: the guard pauses the sync, logs
a `[GUARD]` warning and waits for a manual resume. In an unattended run that
resume never comes, so the pipeline **fails constantly and requires a
pointless manual intervention**.

Disabling the guard is safe in this scenario because the synced content is
regenerated by the pipeline itself, so a full delete-and-recreate cycle
carries no risk of losing real data. Keep the guard enabled for directories
holding irreplaceable user data. See
[CI/CD pipelines, automation scripts and ephemeral deployments](examples.md#37-cicd-pipelines-automation-scripts-and-ephemeral-deployments)
and [Mass-Deletion Safety Guard](configuration.md#mass-deletion-safety-guard).

### Should I disable the mass-deletion guard for backups?

**No** — the opposite of the CI/CD advice above. For one-way backup
procedures (client → NAS/server) keep the guard **enabled**: it is good
protection against source-disk corruption, because if the local disk silently
loses files in bulk, the guard blocks the deletion from propagating to the
backup copy.

That said, for **pure backups** the industry-standard approach is to go one
step further and use `--ignore-delete` (or `HSYNC_IGNORE_DELETE=true`) instead
of relying on the guard alone. The guard only stops deletions *over the
threshold*; `--ignore-delete` disables remote deletion entirely, giving the
backup target proper append-only semantics: if a file disappears locally — by
accident or by malice — its remote safety copy stays intact. See
[Push-only backup to a remote server](examples.md#13-push-only-backup-to-a-remote-server).

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

### Why does HSYNC run a periodic resync, and why might the watcher miss events?

Watch mode uses the OS file-system watcher (inotify on Linux, FSEvents on
macOS, ReadDirectoryChangesW on Windows) to propagate changes in real time.
The watcher can miss events for several reasons:

- **Kernel queue overflow** — rapid bursts of changes can exceed the
  inotify event queue, after which the kernel silently drops events.
- **Watch limit exhaustion (Linux)** — large directory trees need one
  inotify watch per sub-directory; when `fs.inotify.max_user_watches` is
  exceeded, the kernel stops emitting events for the un-watched
  directories.
- **Rapid successive edits** — a file modified faster than the stability
  delay may be coalesced or missed.

The **periodic resync** (`HSYNC_RESYNC`, default `300` s, `0` to disable) is
the safety net: every interval the client runs a full reconciliation of the
local and remote file lists and recovers anything the watcher missed. On
Linux the client also warns at startup when the watched tree approaches the
inotify limit. See
[Periodic resync and the inotify watch limit](configuration.md#periodic-resync-and-the-inotify-watch-limit-linux).

### Can I preview what would happen before syncing?

Yes — use `-n` (dry run) to list the operations without changing anything. See
[Dry Run](sync-modes.md#dry-run).

### How do I exclude files from syncing?

Add patterns to a `.hsyncignore` file (gitignore-style), pass extra patterns via
the CLI, or set `HSYNC_IGNORE_PATTERNS`. See
[Ignore Files](configuration.md#ignore-files-hsyncignore).

### Are hidden files and directories synced?

**Yes.** Following the industry standard (Syncthing, rsync, Nextcloud,
OneDrive), HSYNC synchronises **all files by default**, including dotfiles
(`.env`, `.git/`, `.config/`, ...). Only a built-in **ignore list** of
operating-system cache files and editor/office temporary files is excluded
automatically:

- **macOS cache**: `.DS_Store`, `._*`, `.Spotlight-V100`, `.Trashes`
- **Windows cache**: `Thumbs.db`, `desktop.ini`
- **Office lock files**: `~$*`
- **Editor swap/backup**: `.*.sw?` (dotfiles only), `*~`, `4913`, `.~lock.*#`
- **Generic temp/backup**: `*.tmp`, `*.temp`, `*.bak`

The sentinel file `.hsynckeep` (used to keep empty directories) is always
included regardless of any rule.

To exclude specific dotfiles or dotfile directories, add them to
`.hsyncignore` or use `--ignore-patterns`. See
[Default behaviour](configuration.md#default-behaviour) for the full list of
built-in exclusions.

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
[Architecture](internal/architecture.md).

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

### What are the `.hsyncstate` and `.hsynctombstones` files in my sync directory?

They are internal side-cars the client creates at the bucket root to track sync
state. You normally never touch them:

- **`.hsyncstate`** — records the last-synced hash (base version) for each
  file, used by the three-way merge to decide whether a local or remote change
  is a conflict. See
  [Architecture → State persistence](internal/architecture.md#state-persistence-side-cars).
- **`.hsynctombstones`** — records deletion markers so a file deleted on the
  master while a peer was offline is not resurrected on the next
  reconciliation. See
  [Architecture → State persistence](internal/architecture.md#state-persistence-side-cars).

Both are excluded from sync and safe to leave in place; deleting them forces a
full re-reconciliation on the next cycle.

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
