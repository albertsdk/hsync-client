# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Atomic text writes and the test-harness ready-file signalling."""

import atexit
import os
from pathlib import Path

from hsync_common.fs import atomic_write_text

_HSYNC_READY_FILE_PATH: str | None = None
_HSYNC_READY_ATEXIT_REGISTERED = False


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write *text* to *path* atomically (temp file + rename), owner-only.

    Thin module-local name for :func:`hsync_common.fs.atomic_write_text`, shared
    by the test-harness ready file and the persistent device-identity file.
    """
    atomic_write_text(path, text, mode=mode)


def _write_ready_file() -> None:
    """Signal that this client is ready (initial sync complete and serving).

    Only writes when ``HSYNC_READY_FILE`` is set in the environment (test
    harness). The file contains the process PID, written atomically, so the
    harness can compare it with the expected subprocess PID — never trusting
    a stale or half-written file left by a killed predecessor.
    """
    ready_path = os.environ.get("HSYNC_READY_FILE")
    if not ready_path:
        return
    global _HSYNC_READY_FILE_PATH, _HSYNC_READY_ATEXIT_REGISTERED
    _HSYNC_READY_FILE_PATH = ready_path
    _atomic_write_text(Path(ready_path), f"{os.getpid()}\n")
    if not _HSYNC_READY_ATEXIT_REGISTERED:
        atexit.register(_remove_ready_file)
        _HSYNC_READY_ATEXIT_REGISTERED = True


def _remove_ready_file() -> None:
    """Remove the ready file (atexit handler).  Idempotent."""
    if _HSYNC_READY_FILE_PATH:
        try:
            Path(_HSYNC_READY_FILE_PATH).unlink(missing_ok=True)
        except OSError:
            pass  # best-effort
