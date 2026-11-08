# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Small asyncio interop helpers shared by the client's sync/async seams.

Two situations need care:

* A synchronous caller (the rendezvous client, called from ``stop()``) must run
  a coroutine on a *private* loop. ``asyncio.run`` refuses to do that when a loop
  is already running in this thread, and it refuses **before** awaiting, leaking
  the coroutine as "never awaited". :func:`run_on_fresh_loop` closes it and
  raises a typed error instead.
* A stop signal (``SIGTERM`` -> ``KeyboardInterrupt``) can interrupt the loop
  while ``asyncio.Runner`` is tearing it down, which surfaces as one of the
  loop-state ``RuntimeError`` messages below. Those are not programming errors:
  they are the tail of an interrupted shutdown, and the caller must treat them
  as a clean stop rather than a crash.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

__all__ = [
    "TEARDOWN_LOOP_ERRORS",
    "is_event_loop_teardown_error",
    "run_on_fresh_loop",
]

# The ``RuntimeError`` messages asyncio raises when the loop is interrupted or
# already owned. Matched by text because the exception type is generic.
TEARDOWN_LOOP_ERRORS = (
    "This event loop is already running",
    "Cannot close a running event loop",
    "asyncio.run() cannot be called from a running event loop",
)


def is_event_loop_teardown_error(exc: BaseException) -> bool:
    """True when *exc* is the loop-state error of an interrupted shutdown."""
    if not isinstance(exc, RuntimeError):
        return False
    message = str(exc)
    return any(marker in message for marker in TEARDOWN_LOOP_ERRORS)


def run_on_fresh_loop[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive *coro* to completion on a private loop, or fail fast and clean.

    When another loop already runs in this thread the coroutine is closed (so it
    is never left un-awaited) and a :class:`RuntimeError` carrying the asyncio
    wording is raised, which the callers translate to their own error type.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("asyncio.run() cannot be called from a running event loop")
