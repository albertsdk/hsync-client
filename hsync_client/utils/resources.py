# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Best-effort host-resource diagnostics (inotify watch limit, open FDs)."""

import os
from os import walk
from pathlib import Path

from loguru import logger as log

from hsync_client.constants import (
    INOTIFY_MAX_USER_WATCHES_PATH,
    INOTIFY_WATCH_WARN_RATIO,
    OPEN_FDS_WARN_RATIO,
)


def warn_if_inotify_watch_limit_low(watch_path: str) -> None:
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
    """
    try:
        limit_file = Path(INOTIFY_MAX_USER_WATCHES_PATH)
        if not limit_file.exists():
            return  # Not Linux / inotify limit not exposed.

        max_watches = int(limit_file.read_text().strip())
        if max_watches <= 0:
            return

        threshold = int(max_watches * INOTIFY_WATCH_WARN_RATIO)

        # Count directories, stopping as soon as we cross the threshold to
        # avoid walking a huge tree just to emit a warning.
        dir_count = 0
        for _root, _dirs, _files in walk(watch_path):
            dir_count += 1
            if dir_count > threshold:
                break

        if dir_count > threshold:
            log.warning(
                f"Directory tree under '{watch_path}' has at least {dir_count} "
                f"sub-directories, close to the inotify limit "
                f"(fs.inotify.max_user_watches={max_watches}). File-system events "
                f"may be dropped silently. Raise the limit, e.g.: "
                f"sudo sysctl fs.inotify.max_user_watches={max_watches * 4}. "
                f"Periodic resync (RESYNC) mitigates any events missed meanwhile."
            )
    except Exception as exc:  # pragma: no cover - diagnostics must never break startup
        log.debug(f"inotify watch-limit check skipped: {exc}")


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
    except Exception as exc:  # pragma: no cover - diagnostics must never break startup
        log.debug(f"open-file-descriptor check skipped: {exc}")
