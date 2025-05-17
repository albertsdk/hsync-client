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

## Sync operations

| Label | Category | Meaning |
|-------|----------|---------|
| `[DOWNLOAD]` | transfer | Downloading a file from server / master |
| `[UPLOAD]` | upload | Uploading a file to server / master |
| `[RESUME]` | transfer | Resuming a partial upload or download |
| `[MOVE]` | rename | Renaming / moving a file or directory (watchdog detection) |
| `[REMOVE]` | delete | Removing a file or directory |
| `[DELETE]` | delete | Deleting a local file not present on master (LAN mode) |
| `[CREATE]` | create | Creating a directory |
| `[MODIFY]` | modify | File or directory content modified |
| `[RENAME]` | rename | Rename / move applied |
| `[PERMISSION]` | permission | Setting file / directory permissions |
| `[SYMLINK]` | create | Creating a symbolic link |
| `[IN SYNC]` | in_sync | File already in sync (no action required) |
| `[SKIP]` | in_sync | File skipped |
| `[IGNORE DEL]` | warning | Deletion ignored (ignore-delete / backup mode) |
| `[CONFLICT]` | warning | File conflict detected (three-way merge) |
| `[GUARD]` | warning | Mass-deletion guard tripped (deletions paused) |
| `[TOMBSTONE]` | warning | File deleted remotely while offline; local copy removed |

## Preview (dry-run)

Appear only with `--preview` / dry-run. They describe what *would* happen.

| Label | Category | Meaning |
|-------|----------|---------|
| `[SYNCED]` | in_sync | File is in sync (preview result) |
| `[TO DOWNLOAD]` | transfer | File would be downloaded |
| `[TO UPLOAD]` | upload | File would be uploaded |
| `[TO DELETE]` | delete | File would be deleted |
| `[TO RENAME]` | rename | File would be renamed |

## LAN client operations

Emitted by a LAN **client** (slave) when it pushes local changes to the master.

| Label | Category | Meaning |
|-------|----------|---------|
| `[CLIENT UP]` | upload | Client uploads a file to the master |
| `[CLIENT DEL]` | delete | Client deletes a file on the master |
| `[CLIENT REN]` | rename | Client renames a file on the master |
| `[CLIENT DIR]` | create | Client creates a directory on the master |
| `[CLIENT NEW]` | create | Client creates a new file on the master |
| `[CLIENT KEEP]` | in_sync | Client creates a sentinel file (`.hsynckeep`) |
| `[CLIENT IGN]` | warning | Client ignores a deletion (ignore-delete mode) |

## LAN master broadcasts

Emitted by the LAN **master** when it broadcasts a filesystem event to slaves.

| Label | Category | Meaning |
|-------|----------|---------|
| `[LAN MODIFY]` | modify | Master broadcasts a file modification (slaves pull new bytes). Trigger `(closed)`/`(modified)` shows `IN_CLOSE_WRITE` (reliable) vs `on_modified` debounce fallback; `(dir)` marks a directory metadata event (inotify `IN_ATTRIB` on the parent when a child is created/written/renamed/removed) — directory `MODIFY` lines are stat-deduplicated so only genuine mtime changes propagate |
| `[LAN CREATE]` | create | Master broadcasts a directory creation |
| `[LAN RENAME]` | rename | Master broadcasts a rename / move |
| `[LAN REMOVE]` | delete | Master broadcasts a removal |
| `[LAN SYMLINK]` | create | Master broadcasts a symbolic link creation |
| `[LAN DELETE]` | delete | Master broadcasts a deletion |
| `[LAN DOWNLOAD]` | transfer | Master serves a file download to a slave |
| `[LAN IGNORE DEL]` | warning | Master ignores a deletion (ignore-delete mode) |
| `[LAN SECURITY]` | security | Master rejected an unsafe inbound path |
| `[LAN ECHO]` | infrastructure | Master echoed a slave-originated event to the other slaves |

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
