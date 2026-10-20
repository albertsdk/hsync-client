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
