# Restore a deleted file

When this device applies a deletion, the bytes are not discarded: they move into
a local trash under the state directory, and you can put them back. The trash is
**device-local** — nothing in it is sent to the other devices — and a restored
file returns everywhere on the next sync, as a new version that supersedes the
deletion.

The commands work while a sync runs.

## Prerequisites

- `hsync` installed, and a folder that ran at least one sync (it carries the
  `.hsyncfolder` marker).
- `SYNCED_FOLDER` below is the local path of the share, `FILE_PATH` is the
  bucket-relative path the listing prints.

## See what can be restored

```bash
hsync trash list --folder SYNCED_FOLDER
```

One line per file, newest first: when it was deleted, its size and its path. For
a front-end or a script, ask for JSON:

```bash
hsync trash list --folder SYNCED_FOLDER --json
```

Each entry carries `path`, `size`, `deleted_at`, `origin` (`sync` when the cycle
applied the deletion, `user` when a front-end removed it), `cid`, `bucket` and
`payload_present` — a record whose stored bytes are gone is listed with
`payload_present: false` and cannot be restored (the sweep removes it).

## Delete a file into the trash

Deleting through the trash keeps the removal recoverable: the bytes leave the
folder, the deletion still propagates everywhere, and the entry can be restored
from this device.

```bash
hsync trash add --folder SYNCED_FOLDER FILE_PATH
```

Use it from a script or a desktop action instead of removing the file yourself:
an ordinary `rm` leaves nothing to restore.

## Restore one file

```bash
hsync trash restore --folder SYNCED_FOLDER FILE_PATH
```

The bytes go back with their recorded modification time, and the entry leaves
the trash. The restore refuses when:

- the path already exists in the folder (nothing is overwritten). Pass `--force`
  to replace it instead: the file that is there moves into the trash first, so
  both copies stay recoverable;
- the folder runs `download-only`, because the mirror rule would delete the file
  again. Pass the folder's direction with `--direction download-only` to get that
  refusal instead of a silent deletion.

For a front-end, `--json` prints the verdict it should branch on — `status` is
`restored`, `no-entry`, `destination-exists`, `download-only`,
`payload-missing`, `payload-corrupt` or `failed` — and the exit code stays
non-zero on a refusal:

```bash
hsync trash restore --folder SYNCED_FOLDER FILE_PATH --json
```

## Remove trashed files for good

```bash
hsync trash empty --folder SYNCED_FOLDER
```

The command asks for confirmation; `--yes` skips it, `--older-than DAYS` keeps
the recent entries, and `--json` reports `removed` and `freed` bytes:

```bash
hsync trash empty --folder SYNCED_FOLDER --older-than 7 --yes
```

The store keeps one directory per share, so `--all` empties every share of this
device in one call (with the same `--older-than`, `--yes` and `--json`):

```bash
hsync trash empty --all --yes
```

## Apply the retention window now

A running client sweeps at most hourly; a `--once` run, a stopped client or a NAS
that was off never reaches that point, so the window and the size budget can be
applied on demand:

```bash
hsync trash prune
```

The sweep covers the whole store — the size budget is shared by every share — and
`--json` reports `expired`, `evicted`, `discarded` (records whose stored bytes
were unusable), `freed` and `kept`.

## How long files are kept

30 days by default (`--trash-retention`, `0` = forever). The store also has a
size budget: a share of the filesystem that hosts it, with a floor, or an
explicit `--trash-max-size 2GB`. The sweep runs hourly while the client runs; use
`hsync trash prune` to apply it immediately.

`0` keeps entries until the size budget evicts them; turning the trash off is
`--no-trash` (`HSYNC_TRASH_ENABLED=false`). The desktop offers the same two
settings as a *Keep deleted files in the local trash* switch and a *Restore
window (days)* field, and passes `--no-trash` when the switch is off.

## Where the bytes live

`$XDG_STATE_HOME/hsync/trash` (or `~/.local/state/hsync/trash`), one directory
per folder. Relocate it with `--trash-dir DIR` or `HSYNC_TRASH_DIR`; it must stay
outside the synced folder, which is shared with the other devices, and the client
refuses to start when it does not.

## What is covered

Every file this device removes keeps its bytes — whether the deletion arrives
from the cycle (the remote listing says the file is gone), from a real-time event
(a peer deletes it while both are online), or **inside a directory** a peer
removed: the files of that tree are parked before the tree itself is deleted.

That parking is bounded per removal (1000 files / 1 GiB): deleting a huge folder
does not turn into hashing gigabytes, the files past the bound are removed
without a copy, and the log reports how many.

## Turn the trash off

```bash
hsync server hsync.example.com -b BUCKET_NAME --no-trash
```

With the trash off, a deletion unlinks the file: the confirmation dialog and the
mass-deletion guard remain the only protections.
