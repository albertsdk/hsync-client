# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Client log-label legend.

Single source for the ``--help-symbols`` output: it renders the lines
produced by :func:`legend_lines`.
"""

from hsync_common.labels import ActionLabel

_INTRO_LINES: tuple[str, ...] = (
    "Log legend:",
    "  While watching, [LOCAL ...] marks events originated by this node's",
    "  watcher, [REMOTE ...] events caused by a peer (master, slave or server).",
    "  Unprefixed labels belong to full-sync reconciliation and transfers.",
)

_LABEL_DESCRIPTIONS: dict[ActionLabel, str] = {
    ActionLabel.READY: "Client ready for sync (watcher active + event channel up)",
    ActionLabel.IN_SYNC: "File already in sync (no action required)",
    ActionLabel.SKIP: "File skipped",
    ActionLabel.DOWNLOAD: "Download file from server/master",
    ActionLabel.UPLOAD: "Upload file to server/master",
    ActionLabel.RESUME: "Resume partial download/upload",
    ActionLabel.CREATE: "Create directory (reconcile or server log)",
    ActionLabel.MODIFY: "Modify file or directory content (reconcile/server)",
    ActionLabel.RENAME: "Rename/move file or directory (reconcile/server)",
    ActionLabel.MOVE: "Move/rename detected (debug trace)",
    ActionLabel.DELETE: "Delete file or directory (reconcile/server)",
    ActionLabel.PERMISSION: "Set file/directory permission",
    ActionLabel.SYMLINK: "Create symbolic link (reconcile/server)",
    ActionLabel.GUARD: "Mass-deletion guard tripped",
    ActionLabel.SECURITY: "Unsafe remote path rejected",
    ActionLabel.CONFLICT: "Conflict with remote version",
    ActionLabel.TOMBSTONE: "Deletion recorded while offline",
    ActionLabel.LOCAL_CREATE: "Local watcher: file/directory created",
    ActionLabel.LOCAL_MODIFY: "Local watcher: file/directory content modified",
    ActionLabel.LOCAL_RENAME: "Local watcher: file/directory renamed",
    ActionLabel.LOCAL_DELETE: "Local watcher: file/directory deleted",
    ActionLabel.LOCAL_SYMLINK: "Local watcher: symbolic link created",
    ActionLabel.LOCAL_DEL_KEPT: "Local watcher: deletion kept, nothing removed (deletion policy: keep)",
    ActionLabel.LOCAL_KEEP: "Local watcher: .hsynckeep sentinel created",
    ActionLabel.REMOTE_CREATE: "Peer-caused: file/directory created or applied",
    ActionLabel.REMOTE_MODIFY: "Peer-caused: file/directory content modified",
    ActionLabel.REMOTE_RENAME: "Peer-caused: file/directory renamed",
    ActionLabel.REMOTE_DELETE: "Peer-caused: file/directory deleted",
    ActionLabel.REMOTE_SYMLINK: "Peer-caused: symbolic link created",
    ActionLabel.REMOTE_DEL_KEPT: "Peer-caused: deletion kept, nothing removed (deletion policy: keep)",
    ActionLabel.REMOTE_SECURITY: "Peer-caused: unsafe path rejected",
    ActionLabel.REMOTE_ECHO: "Peer event echo suppressed (debug)",
    ActionLabel.REMOTE_DOWNLOAD: "Master serving file bytes to a peer",
    ActionLabel.INITIAL_SYNC: "Initial synchronization started (first full reconcile of a run)",
    ActionLabel.WATCHER_FALLBACK: "File watcher degraded to polling (inotify/descriptor limit)",
    ActionLabel.WATCHER_RESTORE: "File watcher restored to inotify (limits freed)",
    ActionLabel.FD_EXHAUSTED: "File-descriptor limit reached (RLIMIT_NOFILE / fs.file-max)",
}


def legend_lines() -> list[str]:
    """Return the log-label legend as plain-text lines (no loguru markup)."""
    label_lines = [f"  {label} {description}" for label, description in _LABEL_DESCRIPTIONS.items()]
    return [*_INTRO_LINES, *label_lines]
