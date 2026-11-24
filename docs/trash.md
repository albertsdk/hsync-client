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

- the path already exists in the folder (nothing is overwritten);
- the folder runs `download-only`, because the mirror rule would delete the file
  again. Pass the folder's direction with `--direction download-only` to get that
  refusal instead of a silent deletion.

## Remove trashed files for good

```bash
hsync trash empty --folder SYNCED_FOLDER
```

The command asks for confirmation; `--yes` skips it, and `--older-than DAYS`
keeps the recent entries:

```bash
hsync trash empty --folder SYNCED_FOLDER --older-than 7 --yes
```

## How long files are kept

30 days by default (`--trash-retention`, `0` = forever). The store also has a
size budget: a share of the filesystem that hosts it, with a floor, or an
explicit `--trash-max-size 2GB`. The sweep runs while the client runs and when
you use these commands.

## Where the bytes live

`$XDG_STATE_HOME/hsync/trash` (or `~/.local/state/hsync/trash`), one directory
per folder. Relocate it with `--trash-dir DIR` or `HSYNC_TRASH_DIR`; it must stay
outside the synced folder, which is shared with the other devices.

## Turn the trash off

```bash
hsync server hsync.example.com -b BUCKET_NAME --no-trash
```

With the trash off, a deletion unlinks the file: the confirmation dialog and the
mass-deletion guard remain the only protections.
