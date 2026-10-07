# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Atomic text writes and the test-harness ready-file signalling."""

import atexit
import os
from pathlib import Path

_HSYNC_READY_FILE_PATH: str | None = None
_HSYNC_READY_ATEXIT_REGISTERED = False


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write *text* to *path* atomically (temp file + rename), owner-only.

    A concurrent reader either sees the previous complete content or the new
    one — never a partially written file. Generic helper shared by the
    test-harness ready file and the persistent device-identity file.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
