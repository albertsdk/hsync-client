# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Best-effort host-resource diagnostics (inotify watch limit, open FDs)."""

import os
from dataclasses import dataclass
from os import walk
from pathlib import Path

from loguru import logger as log

from hsync_client.constants import (
    INOTIFY_MAX_USER_WATCHES_PATH,
    INOTIFY_WATCH_WARN_RATIO,
    OPEN_FDS_WARN_RATIO,
)
from hsync_client.events import emit_warning


@dataclass(frozen=True, slots=True)
class WatchBudget:
    """How many inotify watches a tree needs against the per-user maximum.

    ``dirs`` is the number of directories under the watched root (inotify needs
    one watch per directory when the watch is recursive). It is capped at
    ``max_watches + 1``: past that point the exact figure no longer changes the
    decision, and walking a huge tree just to report a precise number would
    delay the watcher. ``capped`` therefore means "at least ``dirs``".
    """

    dirs: int
    max_watches: int
    capped: bool

    @property
    def exhausted(self) -> bool:
        """True when the tree alone needs more watches than the user may hold.

        That is a static impossibility, not a transient shortage: no other
        process has to release anything for this tree to fit.
        """
        return self.dirs > self.max_watches


def inotify_watch_budget(watch_path: str) -> WatchBudget | None:
    """Return *watch_path*'s inotify budget, or ``None`` when not applicable.

    Linux-only: the cap lives in ``fs.inotify.max_user_watches`` and the path
    does not exist elsewhere, so macOS (FSEvents) and Windows
    (ReadDirectoryChangesW) have no equivalent limit and no budget to report.
    """
    try:
        limit_file = Path(INOTIFY_MAX_USER_WATCHES_PATH)
        if not limit_file.exists():
            return None  # Not Linux / inotify limit not exposed.
        max_watches = int(limit_file.read_text().strip())
        if max_watches <= 0:
            return None

        stop_at = max_watches + 1
        dir_count = 0
        for _root, _dirs, _files in walk(watch_path):
            dir_count += 1
            if dir_count >= stop_at:
                break
        return WatchBudget(
            dirs=dir_count,
            max_watches=max_watches,
            capped=dir_count >= stop_at,
        )
    except Exception as exc:  # pragma: no cover - diagnostics must never break startup
        log.debug(f"inotify watch-limit check skipped: {exc}")
        return None


def warn_if_inotify_watch_limit_low(watch_path: str) -> WatchBudget | None:
    """
    Warn when *watch_path* may exceed the inotify watch limit (Linux only).

    The recursive watcher schedules one inotify watch per sub-directory.
    When the tree approaches ``fs.inotify.max_user_watches`` the kernel
    starts dropping events silently (watchdog does not surface
    ``IN_Q_OVERFLOW``), so we proactively warn and point at the remedy.
    Periodic resync (``RESYNC``) is the safety net that recovers any
    events missed in the meantime.

    This is best-effort diagnostics: any failure is swallowed so it can
    never block the watcher from starting.

    Args:
        watch_path: Directory tree that will be watched recursively.

    Returns:
        The measured :class:`WatchBudget` when it is at or past the warn ratio,
        otherwise ``None`` (also when the check does not apply: not Linux,
        unreadable limit).
    """
    budget = inotify_watch_budget(watch_path)
    if budget is None:
        return None

    max_watches = budget.max_watches
    if budget.dirs <= int(max_watches * INOTIFY_WATCH_WARN_RATIO):
        return None

    scale = max(budget.dirs * 4, max_watches * 4)
    if budget.exhausted:
        log.error(
            f"Directory tree under '{watch_path}' has at least {budget.dirs} "
            f"directories and needs one inotify watch each, but "
            f"fs.inotify.max_user_watches={max_watches}: inotify cannot watch this "
            f"tree, so the file watcher runs in polling mode (higher CPU, up to "
            f"~1 s event latency) until the limit is raised. Make the fix permanent "
            f"with: echo fs.inotify.max_user_watches={scale} | "
            f"sudo tee /etc/sysctl.d/99-hsync-inotify.conf && sudo sysctl --system"
        )
        emit_warning(
            "The shared folder has more sub-folders than the system lets one user "
            "watch, so real-time sync runs in polling mode",
            source="watcher",
            reason="inotify_watches",
            needed=budget.dirs,
            limit=max_watches,
        )
        return budget

    log.warning(
        f"Directory tree under '{watch_path}' has at least {budget.dirs} "
        f"directories, close to the inotify limit "
        f"(fs.inotify.max_user_watches={max_watches}). File-system events "
        f"may be dropped silently. Raise the limit, e.g.: "
        f"sudo sysctl fs.inotify.max_user_watches={scale}. "
        f"Periodic resync (RESYNC) mitigates any events missed meanwhile."
    )
    emit_warning(
        "The shared folder is close to the system's file-watcher limit: real-time "
        "sync may fall back to polling",
        source="watcher",
        reason="inotify_watches",
        needed=budget.dirs,
        limit=max_watches,
    )
    return budget


def warn_if_open_fds_high() -> None:
    """Warn when the process is close to its open-file-descriptor limit.

    Best-effort diagnostics (Linux): compares the number of currently open file
    descriptors (``/proc/self/fd``) against the soft ``RLIMIT_NOFILE`` and warns
    when the process is within a fraction of it, so an impending "Too many open
    files" failure is visible before it happens. Any failure is swallowed so it
    can never block the watcher from starting.
    """
    try:
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft in (resource.RLIM_INFINITY, -1):
            return
        fd_count = len(os.listdir("/proc/self/fd"))
        if fd_count >= int(soft * OPEN_FDS_WARN_RATIO):
            log.warning(
                f"Process has {fd_count} open file descriptors, close to the soft "
                f"limit ({soft}). Raise it (e.g. `ulimit -n {soft * 4}`) or reduce "
                f"concurrent connections/transfers to avoid 'Too many open files' "
                f"errors."
            )
            emit_warning(
                "The sync process is close to its open-file limit: transfers may "
                "fail with 'Too many open files'",
                source="watcher",
                reason="fd_exhaustion",
                needed=fd_count,
                limit=int(soft),
            )
    except Exception as exc:  # pragma: no cover - diagnostics must never break startup
        log.debug(f"open-file-descriptor check skipped: {exc}")
