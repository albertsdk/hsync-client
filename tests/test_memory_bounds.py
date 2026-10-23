# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box memory-bound tests.

These tests exercise public behaviour only (``enqueue``, ``qsize``, the
``dropped`` counter) so they stay valid even if the internal queue/backoff
implementation is rewritten. The RAM bound is deliberately generous: the point
is to prove that a stalled consumer cannot grow memory without limit, not to
pin an exact allocator figure.
"""

from __future__ import annotations

import gc
import tracemalloc

from hsync_client.event_queue import EVENT_QUEUE_MAXSIZE, EventQueue

_STALLED_WEBSOCKET = object()


class TestEventQueueMemory:
    async def test_queue_is_bounded_and_counts_drops(self):
        queue = EventQueue(_STALLED_WEBSOCKET)  # never consumed in this test
        overflow = 500
        for i in range(EVENT_QUEUE_MAXSIZE + overflow):
            await queue.enqueue({"name": "added", "path": f"file-{i}.bin"})

        assert queue.queue.qsize() == EVENT_QUEUE_MAXSIZE
        assert queue.dropped == overflow

    async def test_memory_stays_bounded_when_the_consumer_is_stalled(self):
        """A fire-and-forget producer must not grow RAM past the queue cap."""
        queue = EventQueue(_STALLED_WEBSOCKET)
        total = 200_000
        gc.collect()
        tracemalloc.start()
        try:
            for i in range(total):
                await queue.enqueue({"name": "added", "path": f"file-{i}.bin"})
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert queue.queue.qsize() == EVENT_QUEUE_MAXSIZE
        assert queue.dropped == total - EVENT_QUEUE_MAXSIZE
        # Unbounded growth would be ~tens of MB; the cap keeps it to a few MB.
        assert peak < 20 * 1024 * 1024

    async def test_delivered_events_keep_their_order(self):
        """Dropping only happens on overflow; the retained events stay FIFO."""
        queue = EventQueue(_STALLED_WEBSOCKET)
        names = [f"file-{i}.bin" for i in range(100)]
        for name in names:
            await queue.enqueue({"name": "added", "path": name})

        delivered = []
        while not queue.queue.empty():
            delivered.append((await queue.queue.get()).data["path"])
            queue.queue.task_done()

        assert delivered == names
