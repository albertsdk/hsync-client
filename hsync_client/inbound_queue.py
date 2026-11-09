# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Bounded, per-path ordered execution of inbound mutation events.

A WebSocket receive loop must never wait for the filesystem work an event
implies: one multi-gigabyte download would otherwise delay every later event —
an instant delete included — for the whole transfer plus its retry ladder.
:class:`InboundEventQueue` decouples the two: the loop submits a frame and keeps
reading, while the mutations run on tasks bounded by
``MAX_CONCURRENT_TRANSFERS``.

Mutations touching the same path stay strictly ordered. The event stream *is*
the causal order for a path, and an unordered rename or directory create would
otherwise duplicate or resurrect files: a rename applied while its source
download is still in flight finds the source missing and re-downloads it, after
which the pending download re-creates the original. Ordering therefore chains
on **every** path an event carries — a rename occupies both its source and its
destination, everything else one.
"""

from __future__ import annotations

import asyncio
from functools import partial

from loguru import logger as log

from hsync_client.config import CONFIG
from hsync_client.constants import (
    INBOUND_MAX_PENDING_EVENTS_DEFAULT,
    MAX_CONCURRENT_TRANSFERS_DEFAULT,
)
from hsync_client.event_dispatch import (
    EventDispatchContext,
    apply_inbound_event,
    event_path_keys,
)


class InboundEventQueue:
    """Per-connection executor for inbound mutation events.

    Args:
        ctx: Dispatch context every task applies its event with (echo
            suppression, directory dedup, source resolver). One per connection,
            so its caches live exactly as long as the stream they describe.
        max_concurrent: Mutations allowed to run at the same time. Defaults to
            ``MAX_CONCURRENT_TRANSFERS`` so the live stream and the reconcile
            share one bandwidth budget.
        max_pending: Outstanding mutations (queued or running) tolerated before
            :meth:`submit` waits. That wait is the backpressure keeping a
            flooding peer from growing an unbounded backlog.
    """

    def __init__(
        self,
        ctx: EventDispatchContext,
        *,
        max_concurrent: int | None = None,
        max_pending: int | None = None,
    ) -> None:
        concurrency = max(
            1,
            max_concurrent
            or int(CONFIG.get("MAX_CONCURRENT_TRANSFERS", MAX_CONCURRENT_TRANSFERS_DEFAULT)),
        )
        self._ctx = ctx
        self._slots = asyncio.Semaphore(
            max(max_pending or INBOUND_MAX_PENDING_EVENTS_DEFAULT, concurrency)
        )
        self._semaphore = asyncio.Semaphore(concurrency)
        self._pending: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def outstanding(self) -> int:
        """Number of mutations queued or running."""
        return len(self._tasks)

    async def submit(self, event_data: dict) -> None:
        """Schedule one inbound event, waiting only for queue capacity.

        The receive loop calls this for every frame, so returning early is what
        keeps a large transfer from delaying the next frame. Backpressure
        applies only once ``max_pending`` mutations are already outstanding.
        """
        await self._slots.acquire()
        keys = event_path_keys(event_data)
        predecessors = tuple(
            dict.fromkeys(task for key in keys if (task := self._pending.get(key)) is not None)
        )
        task = asyncio.create_task(self._run(event_data, predecessors))
        for key in keys:
            self._pending[key] = task
        self._tasks.add(task)
        task.add_done_callback(partial(self._finish, keys=keys))

    async def drain(self) -> None:
        """Await every outstanding mutation before the link is torn down.

        Returning to the connection loop with writes still in flight would race
        the next full sync against a half-applied tree.
        """
        self._pending.clear()
        tasks = list(self._tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, event_data: dict, predecessors: tuple[asyncio.Task[None], ...]) -> None:
        try:
            # Order first, capacity second: a chained task must never hold a
            # concurrency slot while it waits for its predecessor.
            await self._await_predecessors(predecessors)
            async with self._semaphore:
                await apply_inbound_event(event_data, self._ctx)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One unapplicable event must not stop the stream; the dispatcher
            # already isolates per-event failures, this is the net for bugs in
            # the apply helpers.
            log.exception(
                f"Error applying inbound event {event_data.get('name')!r}: "
                f"{event_data.get('src')!r}"
            )
        finally:
            self._slots.release()

    @staticmethod
    async def _await_predecessors(predecessors: tuple[asyncio.Task[None], ...]) -> None:
        """Wait for the events this one is causally after.

        A predecessor that failed or was cancelled is not a reason to skip this
        event; only this task's own cancellation is.
        """
        for previous in predecessors:
            try:
                await previous
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:
                pass

    def _finish(self, task: asyncio.Task[None], keys: tuple[str, ...]) -> None:
        """Drop a completed task and its path registrations."""
        self._tasks.discard(task)
        for key in keys:
            if self._pending.get(key) is task:
                del self._pending[key]
