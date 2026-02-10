# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Process-wide bounded executor pools (lazy singletons).

The transfer executor (``sync``) and the two realtime pools (LAN slave ops,
remote uploads) are process-wide singletons so their worker threads — and
their per-thread ``requests.Session`` keep-alive connections — survive across
sync cycles and handler recreation instead of paying a fresh TCP+TLS
handshake per worker/event.

The singleton reference is stored as a **module attribute** so the test-suite
isolation fixture (``tests/conftest.py::_reset_realtime_pools``) can shut it
down and clear it between tests.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from types import ModuleType

from hsync_common.text import escape_loguru_markup
from loguru import logger as log

# Guards singleton creation only; execution never contends here.
_create_lock = threading.Lock()


def get_singleton_executor(
    module: ModuleType,
    attr: str,
    *,
    max_workers: int,
    thread_name_prefix: str,
    resizable: bool = False,
) -> ThreadPoolExecutor:
    """Return the module-level singleton pool stored at ``module.attr``.

    Created lazily on first use. With ``resizable=True`` a cached pool whose
    worker count differs from *max_workers* is replaced (old instance shut
    down without waiting, in-flight transfers keep running to completion) —
    used by the transfer executor, whose size follows the autotuned
    ``MAX_CONCURRENT_TRANSFERS``. Non-resizable pools keep their fixed size.

    Args:
        module: Module object holding the singleton attribute.
        attr: Attribute name of the cached ``ThreadPoolExecutor`` (or None).
        max_workers: Pool size.
        thread_name_prefix: Worker thread name prefix (debugging).
        resizable: Replace the cached pool when the worker count changes.
    """
    executor = getattr(module, attr, None)
    if executor is not None and (not resizable or executor._max_workers == max_workers):
        return executor
    with _create_lock:
        # Re-check under the lock: another thread may have created it.
        executor = getattr(module, attr, None)
        if executor is None or (resizable and executor._max_workers != max_workers):
            if executor is not None:
                executor.shutdown(wait=False)
            executor = ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix=thread_name_prefix
            )
            setattr(module, attr, executor)
        return executor


def log_future_error(future: Future, *, label: str) -> None:
    """Surface an unexpected exception from a pooled background operation.

    Transfer helpers handle their own transport errors (log + ``transfer_failed``
    event); this done-callback (attach via ``functools.partial``) only catches
    programming bugs that would otherwise vanish silently inside the Future.
    """
    exc = future.exception()
    if exc is not None:
        log.error(f"{label}: {escape_loguru_markup(str(exc))}")
