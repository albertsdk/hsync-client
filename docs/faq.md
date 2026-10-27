# FAQ — Common

Common questions that apply to both modes. Mode-specific questions are split
into:

- [Server Mode](faq-server.md)
- [p2p Mode](faq-p2p.md)

## Maintenance

**Q: A folder keeps re-syncing or reports phantom conflicts — how do I start from a clean sync state?**

The folder's local sync state (merge bases and deletion markers) is inconsistent.
Forget it so the next cycle reconciles the folder from scratch:

- **CLI**: stop the sync, then run `hsync state reset --folder <dir>`. The
  command refuses while another `hsync` process is syncing the folder. Add
  `--json` for scripts, or `--quarantine` to move an unreadable database aside
  instead of failing.
- **Desktop**: stop the sync, enable **Show advanced options** in General, then
  use **Settings → Sync → Advanced options → Reset sync state…**
  ([Desktop procedure](https://github.com/albertsdk/hsync-desktop/blob/main/docs/usage.md#reset-the-sync-state)).

The reset clears only that folder's merge bases and deletion markers. It never
touches the synced files, `.hsyncignore`, `.hsyncfolder`, the settings, the
saved keys, the device identity or the trusted fingerprints. On the next sync
every file is scanned and hashed again (a slower first cycle); files that are
identical on both sides are not transferred; differences are resolved normally
with a conflict copy when both sides changed; and deletions made shortly before
the reset can come back, because the deletion markers were forgotten.

Do **not** delete the files under `syncstate/` by hand: the database is
per-folder, `index.db` is shared, `HSYNC_STATE_DB` may hold every folder, and a
running sync must be stopped first. The CLI and the Desktop button perform the
reset safely and keep the folder/index rows.

Full procedure:
[Troubleshooting — A folder keeps re-syncing](troubleshooting.md#a-folder-keeps-re-syncing-or-reports-phantom-conflicts).

## Deletions

**Q: How do I keep a copy that never loses files (a backup target)?**

Run with `--deletion-policy keep` (or set `HSYNC_DELETION_POLICY=keep`). The
device then never deletes: local removals are neither applied to the other side
nor recorded as deletion markers, remote deletions are not applied here, and an
emptied folder is left in place. It is independent of `--sync-direction`: use
`upload-only` as well when you also want to stop taking remote content down.
No `--allow-deletions` is needed: with `keep` there is no deletion risk to
confirm, so a backup unit can run unattended. Deletion markers recorded before
switching also stop deleting anything: a file still present on the other side is
downloaded here instead of being removed there.
See [Configuration → Deletions](configuration.md#deletions).

**Q: A file disappeared from the folder — how can that happen?**

With the default policy the peer's deletion is applied, which is the point of
bidirectional sync. The paths that can remove a file locally are:

- a deletion from the other side, applied because a matching deletion marker
  proves the local copy is a stale ancestor ([Conflict Resolution](conflict-resolution.md#deletions-and-tombstones-adr-0003));
- the empty-directory sweep, which removes a folder the peer no longer has once
  it holds nothing (suspended under `keep` and `upload-only`);
- `--sync-direction download-only` mirroring the source, which refuses to delete
  local-only files unless `--allow-deletions` was passed explicitly;
- the mass-deletion guard only *stops* a batch (it never deletes anything).

If the removal was unintended, set `--deletion-policy keep` before the next run
and restore the content from a backup: HSYNC keeps no file content to undelete.
