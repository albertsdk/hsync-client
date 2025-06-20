# Log Labels

`hsync-client` emits bracketed action labels at the start of each log line
(e.g. `[DOWNLOAD]`, `[NO MASTER]`) to tag the operation or event. This page
lists every label that can appear in the client's logs.

> The complete, cross-project catalog (descriptions, color categories, and which
> project emits each label) lives in
> [`hsync-common/docs/log-labels.md`](../../hsync-common/docs/log-labels.md).
> This page is the client-specific view: only the labels this client emits,
> grouped by where they come from.

Labels are colored by **semantic category** (transfer=cyan, upload/create=green,
modify/rename=blue, delete=red, permission=infrastructure=magenta,
in-sync=dim, warning=yellow, security=red). Run `hsync-client --legend`
(`--help-symbols`) to print the legend in the terminal.

## The two-level origin rule

- **Prefixed labels** appear during **continuous watching**: `[LOCAL ...]`
  marks an event originated by this node's own watcher, `[REMOTE ...]` an
  event caused by a peer (the master for a slave, a slave for the master, the
  central server in remote mode).
- **Unprefixed labels** belong to the **reconciliation / full-scan phase**,
  transfers, states and previews.

> **Known limitation (LAN master).** Origin attribution relies on the
> inbound-origin marker TTL (`INBOUND_ORIGIN_TTL`, 10 s): if the watchdog
> event fires after expiry, the line prints `LOCAL` even though the change
> was peer-caused. The `(CID: ...)` suffix degrades together with the prefix,
> so the line stays internally coherent.

## Watching events (prefixed)

Emitted while the client watches its bucket or applies inbound peer events.

| Label | Category | Meaning |
|-------|----------|---------|
| `[LOCAL CREATE]` | create | Local watcher: file/directory created. On a slave, a directory create logs a debug line with a `(dir)` suffix; a file arriving already complete logs `(N bytes, deferred to on_closed)` (debug) |
| `[LOCAL MODIFY]` | modify | Local watcher: file/directory content modified. Trigger `(closed)`/`(modified)` = `IN_CLOSE_WRITE` (reliable) vs `on_modified` debounce fallback; `(dir)` = directory metadata event (stat-deduplicated) |
| `[LOCAL RENAME]` | rename | Local watcher: file/directory renamed (on a slave, also the rename pushed to the master) |
| `[LOCAL DELETE]` | delete | Local watcher: file/directory deleted (on a slave, also the delete pushed to the master) |
| `[LOCAL SYMLINK]` | create | Local watcher: symbolic link created |
| `[LOCAL SECURITY]` | security | Local watcher: unsafe local path rejected (escaping symlink, traversal) |
| `[LOCAL IGNORE DEL]` | warning | Local watcher: deletion skipped (ignore-delete / backup mode) |
| `[LOCAL KEEP]` | in_sync | Local watcher: `.hsynckeep` sentinel created to preserve an empty directory |
| `[REMOTE CREATE]` | create | Peer-caused: directory created (inbound event applied, or master broadcasting a slave's create). Duplicate directory events for the same path are deduplicated (a single `mkdir` triggers a burst of `IN_ATTRIB` on the master) |
| `[REMOTE MODIFY]` | modify | Peer-caused: file/directory content modified (inbound event applied, or master broadcasting a slave's upload). Duplicate no-op directory events (same path, unchanged permissions) are deduplicated |
| `[REMOTE RENAME]` | rename | Peer-caused: file/directory renamed |
| `[REMOTE DELETE]` | delete | Peer-caused: file/directory deleted |
| `[REMOTE SYMLINK]` | create | Peer-caused: symbolic link created |
| `[REMOTE IGNORE DEL]` | warning | Peer-caused: deletion skipped (ignore-delete / backup mode) |
| `[REMOTE SECURITY]` | security | Peer-caused: unsafe inbound path rejected |
| `[REMOTE ECHO]` | infrastructure | Peer event echo suppressed (own event discarded, debug) |
| `[REMOTE DOWNLOAD]` | transfer | Master serving file bytes to a slave |

## Reconciliation, transfers & previews (unprefixed)

Emitted during the full-scan/reconcile phase and by the transfer engine.

| Label | Category | Meaning |
|-------|----------|---------|
| `[DOWNLOAD]` | transfer | Downloading a file from server / master |
| `[UPLOAD]` | upload | Uploading a file to server / master |
| `[RESUME]` | transfer | Resuming a partial upload or download |
| `[MOVE]` | rename | Internal debug trace of a watchdog move (before classification; never the propagated event) |
| `[DELETE]` | delete | Reconcile-phase deletion (e.g. local file not on master) |
| `[CREATE]` | create | Reconcile-phase directory creation |
| `[MODIFY]` | modify | Reconcile-phase modification |
| `[RENAME]` | rename | Reconcile-phase rename |
| `[PERMISSION]` | permission | Setting file / directory permissions |
| `[SYMLINK]` | create | Reconcile-phase symbolic link creation |
| `[IN SYNC]` | in_sync | File already in sync (no action required) |
| `[SKIP]` | in_sync | File skipped |
| `[CONFLICT]` | warning | File conflict detected (three-way merge) |
| `[GUARD]` | warning | Mass-deletion guard tripped (deletions paused) |
| `[TOMBSTONE]` | warning | File deleted remotely while offline; local copy removed |

### Preview (dry-run)

Appear only with `--preview` / dry-run. They describe what *would* happen.

| Label | Category | Meaning |
|-------|----------|---------|
| `[SYNCED]` | in_sync | File is in sync (preview result) |
| `[TO DOWNLOAD]` | transfer | File would be downloaded |
| `[TO UPLOAD]` | upload | File would be uploaded |
| `[TO DELETE]` | delete | File would be deleted |
| `[TO RENAME]` | rename | File would be renamed |

## Security & warnings

These usually surface a UI notification when running behind a desktop/Android
front-end.

| Label | Category | Meaning |
|-------|----------|---------|
| `[SECURITY]` | security | Rejected an unsafe remote path (traversal, absolute, internal name) |
| `[NO MASTER]` | warning | No master elected after the grace period (all peers are `--never-master`) |
| `[MASTER CONFLICT]` | warning | Two `force_master` peers conflict; one wins by earlier start time |
| `[MASTER TAKEOVER]` | warning | A remote `force_master` peer takes over a non-force_master master |
| `[INSECURE MASTER]` | warning | Master does not advertise HTTPS + a cert fingerprint; connection refused |
| `[PAIRING NEEDED]` | warning | Strict pairing: master cert is unpinned; out-of-band confirmation required |
| `[FINGERPRINT CHANGED]` | warning | Master cert no longer matches the pinned fingerprint (possible MITM); refused |
| `[AUTH REQUIRED]` | warning | Non-interactive LAN write lacks `HSYNC_LAN_KEY`; set it or run interactively |

The client does not emit `[WS]` (that is server/infrastructure-only).

## Related

- [Usage](usage.md) — CLI commands and modes
- [LAN Mode](lan-mode.md) — master election, pairing, trust model
- [Canonical label catalog](../../hsync-common/docs/log-labels.md)
